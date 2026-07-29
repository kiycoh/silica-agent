# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Snapshot of the op stream the ingest FSM emits, not of the prose it writes.

The distill -> collision -> validate -> write lanes transform ops between the
model's output and the disk: COLLISION drops vault near-dups, VALIDATE coerces
paths / synthesises the missing hub / dedupes, cohesion injects sibling refs.
Every other FSM test mocks sanitize and validate away, so all of that can
change silently. Here the model is the only thing faked — a deterministic
function of the payload it receives — and the assertion is the exact list of
ops arriving at bulk_write_atomic.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from silica.kernel.write.atomic_write import AtomicBulkResult, NoteCommitResult
from silica.router.orchestrator import InjectorFSM, InjectorState

INBOX = "Inbox/lezione.md"
TARGET = "Concepts"

# Long enough to clear the write-snippet floor (SILICA_MIN_WRITE_SNIPPET_CHARS).
BODY_A = "Il percettrone calcola una somma pesata degli ingressi e applica una soglia. " * 8
BODY_B = "La discesa del gradiente aggiorna i pesi seguendo la direzione opposta al gradiente. " * 8


def _chunk(*concepts: tuple[str, str]) -> dict:
    return {
        "schema_version": 1,
        "batches": [{
            "inbox_file": INBOX,
            "concepts": [{"name": n, "inbox_excerpt": body} for n, body in concepts],
        }],
    }


def _fake_distiller(**kwargs):
    """The model, pinned: one write op per concept still in the payload.

    Deterministic in its input, so anything the FSM drops or adds before/after
    the call shows up in the op stream and nowhere else.
    """
    payload = kwargs["payload"]
    return {"updates": [
        {
            "op": "write",
            "path": f"{TARGET}/{c['name']}.md",
            "heading": c["name"],
            "snippet": c["inbox_excerpt"],
            "source_basename": "lezione.md",
        }
        for b in payload.get("batches", [])
        for c in b.get("concepts", [])
    ]}


class _FakeDriver:
    """In-memory vault: `notes` maps vault path -> content."""

    def __init__(self, notes: dict[str, str] | None = None):
        self.notes = dict(notes or {})

    def read_note(self, ref):
        # Bare names resolve to the note with that stem, as the real driver does
        # (validate's hub existence check passes a name, not a path).
        path = getattr(ref, "path", ref)
        if path not in self.notes:
            stems = {p.rsplit("/", 1)[-1].removesuffix(".md"): p for p in self.notes}
            path = stems.get(path.removesuffix(".md"), path)
        if path not in self.notes:
            raise RuntimeError(f"File not found: {path}")
        return MagicMock(content=self.notes[path], path=path)

    def overwrite(self, ref, content):
        # Real, not a no-op: HUB_UPDATE polls read_note for 5s waiting for its
        # MOC block to settle, and a MagicMock write makes every test pay it.
        self.notes[getattr(ref, "path", ref)] = content

    def search_names(self, _query=""):
        return [MagicMock(path=p, name=p.rsplit("/", 1)[-1].removesuffix(".md"))
                for p in self.notes]

    def graph_snapshot(self, *a, **k):
        return MagicMock()

    def __getattr__(self, _name):  # every other driver call is a best-effort no-op
        return MagicMock()


@pytest.fixture
def op_stream(monkeypatch):
    """Run the FSM over one chunk and yield the ops that reached WRITE.

    Usage: `stream = op_stream(chunk, vault={...})` -> list of (op, path).
    """
    captured: list = []

    def _capture(ops, **_kw):
        captured.extend(ops)
        return AtomicBulkResult(
            committed=[NoteCommitResult(ok=True, path=o.touched_ref() or "", op=o.op.value)
                       for o in ops],
            total=len(ops),
        )

    def _run(chunk: dict, vault: dict[str, str] | None = None):
        driver = _FakeDriver(vault)
        empty_index = MagicMock()
        empty_index.__len__ = lambda _self: 0  # COLLISION -> embedder-free MinHash leg

        with (
            patch("silica.router.orchestrator.silica_recon", return_value={"success": True}),
            patch("silica.router.orchestrator.silica_payload", return_value={"chunks": [chunk]}),
            patch("silica.router.states.distill.run_distiller", side_effect=_fake_distiller),
            patch("silica.router.states.distill.orch.CONFIG.distill_concurrency", 1),
            patch("silica.kernel.recall.embed.get_store", return_value=empty_index),
            patch("silica.kernel.write.atomic_write.bulk_write_atomic", side_effect=_capture),
            patch("silica.router.orchestrator.DRIVER", driver),
            patch("silica.kernel.write.validate.DRIVER", driver),
            patch("silica.router.orchestrator.silica_lint", return_value={"success": True}),
            patch("silica.tools.wrapped.silica_snapshot",
                  return_value={"txn_id": "txn_test", "inverses": []}),
            patch("silica.tools.wrapped.silica_cleanup", return_value={"success": True}),
            patch("silica.kernel.graph_diff.check_graph_regression", return_value=(True, [])),
        ):
            fsm = InjectorFSM(INBOX, TARGET)
            res = fsm.run()
        assert fsm.state == InjectorState.DONE, res
        return [(o.op.value, o.touched_ref()) for o in captured]

    return _run


def test_two_concepts_become_two_writes_plus_the_synthesised_hub(op_stream):
    """Baseline stream: VALIDATE prepends the hub note the target dir lacks."""
    stream = op_stream(_chunk(("Percettrone", BODY_A), ("Discesa del gradiente", BODY_B)))

    assert stream == [
        ("write", "Concepts/Concepts.md"),
        ("write", "Concepts/Percettrone.md"),
        ("write", "Concepts/Discesa del gradiente.md"),
    ]


def test_existing_hub_is_not_re_synthesised(op_stream):
    stream = op_stream(
        _chunk(("Percettrone", BODY_A)),
        vault={"Concepts/Concepts.md": "# Concepts\n"},
    )

    assert stream == [("write", "Concepts/Percettrone.md")]


def test_collision_drops_a_vault_near_dup_before_the_model_sees_it(op_stream):
    """The MinHash leg (embedder-free) defers a concept whose twin is already in
    the vault: it never reaches the distiller, so no op is emitted for it."""
    stream = op_stream(
        _chunk(("Percettrone", BODY_A), ("Discesa del gradiente", BODY_B)),
        vault={"Concepts/Concepts.md": "# Concepts\n", "Concepts/Percettrone.md": BODY_A},
    )

    assert stream == [("write", "Concepts/Discesa del gradiente.md")]


def test_write_to_an_existing_note_is_coerced_to_a_patch(op_stream):
    """Same title, unrelated content (so the near-dup leg leaves it alone):
    VALIDATE must coerce the write into a patch instead of clobbering the note."""
    stream = op_stream(
        _chunk(("Percettrone", BODY_A)),
        vault={
            "Concepts/Concepts.md": "# Concepts\n",
            "Concepts/Percettrone.md": "# Percettrone\n\nAppunti dell'autore su tutt'altro.\n",
        },
    )

    assert stream == [("patch", "Concepts/Percettrone.md")]


def test_the_same_concept_twice_collapses_to_one_write(op_stream):
    """The model repeating a concept must not write the note twice — VALIDATE
    dedupes by path and keeps the richest op."""
    stream = op_stream(_chunk(("Percettrone", BODY_A), ("Percettrone", BODY_B)))

    assert stream == [
        ("write", "Concepts/Concepts.md"),
        ("write", "Concepts/Percettrone.md"),
    ]
