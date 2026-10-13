# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""G1's case runner passes the built index and fails the one before ADR-0092:
a gate that cannot fail measures nothing."""
from __future__ import annotations

import pytest

from evals.probe_chat_graph_gate import Live, g1_case


@pytest.fixture
def vault(tmp_path, monkeypatch):
    import silica.cli  # noqa: F401  registers the tools and /delete
    import silica.driver
    import silica.kernel.write.checkpoints as cp
    import silica.kernel.write.undo_journal as uj
    from silica.config import CONFIG

    v = tmp_path / "v"
    (v / "Corso").mkdir(parents=True)
    (v / "vault.yaml").write_text("write_dir: ''\n", encoding="utf-8")
    notes = {"Hub": "[[Target]] [[Other]]", "Linker": "see [[Target]]", "Target": "[[Leaf]]",
             "Leaf": "leaf", "Other": "other", "Lonely": "[[Leaf2]]", "Leaf2": "x"}
    for name, body in notes.items():
        (v / "Corso" / f"{name}.md").write_text(f"# {name}\n\n{body}\n", encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(v))
    monkeypatch.setenv("SILICA_VAULT", str(v))
    monkeypatch.setattr(silica.driver, "_driver", None)
    monkeypatch.setattr(cp, "_store", cp.CheckpointStore(tmp_path / "cp.db"))
    monkeypatch.setattr(uj, "_store", uj.UndoJournalStore(tmp_path / "uj.db"))
    return v


def test_a_linked_and_an_unlinked_case_pass_on_the_built_index(vault):
    live = Live("fs")
    linked = g1_case(live, "Corso/Target.md", ["Corso/Hub.md", "Corso/Linker.md"], vault)
    assert (linked["a"], linked["b"], linked["c"]) == (True, True, True), linked
    assert (linked["dangling"], linked["orphaned"]) == (2, 1)
    lone = g1_case(live, "Corso/Lonely.md", [], vault)
    assert (lone["a"], lone["b"], lone["c"]) == (True, True, True), lone


def test_the_case_fails_with_the_repair_switched_off(vault, monkeypatch):
    monkeypatch.setenv("GATE0092_SABOTAGE", "1")
    live = Live("fs")
    linked = g1_case(live, "Corso/Target.md", ["Corso/Hub.md", "Corso/Linker.md"], vault)
    assert linked["a"] is True
    assert linked["b"] is False and linked["c"] is False
