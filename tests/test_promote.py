# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""`/promote` — the consent bridge from episodic memory to the vault.

Machine memory enters the vault only by explicit promotion: this verb renders
one supersede chain into a stub and feeds it through the standard nucleate
gate, then stamps the chain head with the note it became.
"""
from __future__ import annotations

import pytest

from silica.config import CONFIG


def test_promote_is_advertised_where_nucleate_is():
    """`/promote <key>` runs the whole injector: it does not belong under
    "Direct, immediate, no LLM" just because bare `/promote` only lists."""
    from silica.ui.commands import COMMANDS

    cmd = next((c for c in COMMANDS if c.name == "/promote"), None)
    assert cmd is not None and not cmd.repl_only
    assert cmd.group == next(c.group for c in COMMANDS if c.name == "/nucleate")


@pytest.fixture
def store(tmp_path, monkeypatch):
    from silica.kernel.recall import episodic

    path = tmp_path / "episodic.json"
    monkeypatch.setattr(episodic, "store_path", lambda: path)
    return path


def _promote(line: str = "/promote"):
    from silica.cli import _expand_workflow_shortcut
    return _expand_workflow_shortcut(line)


def _seed(path, key="user.dog.name", texts=("Rex", "Rex", "Tom")):
    from silica.kernel.recall.episodic import EpisodicStore

    s = EpisodicStore(path=path)
    for i, text in enumerate(texts, start=1):
        s.capture([{"key": key, "text": text}], run_id=f"r{i}", seen=f"2026-06-1{i}")
    return s


class TestListing:
    def test_bare_promote_lists_the_candidates(self, store, capsys):
        _seed(store)

        assert _promote() == ""  # handled inline, no agent turn

        out = capsys.readouterr().out
        assert "user.dog" in out    # the entity, which is what /promote writes
        assert "3 runs" in out
        assert "2026-06-11" in out  # first seen of the chain
        assert "name=Tom" in out    # chain preview: attribute and current value

    def test_candidates_are_listed_per_entity_not_per_attribute(self, store, capsys):
        """A promotion writes one note per entity: three keys about one dog are
        one note, not three. Measured: per-key stubs are so thin the write gate
        rejects every one of them (155 chars against a 275 floor)."""
        _seed(store)
        _seed(store, key="user.dog.breed", texts=("pastore tedesco",) * 3)
        _seed(store, key="user.city.name", texts=("Torino",) * 3)

        assert _promote() == ""

        out = capsys.readouterr().out
        assert out.count("runs since") == 2  # user.dog and user.city, not three keys
        assert "user.dog" in out
        assert "Tom" in out and "pastore tedesco" in out  # both attributes

    def test_an_empty_queue_says_so(self, store, capsys):
        assert _promote() == ""
        assert "no episodic candidate" in capsys.readouterr().out.lower()


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A real FS-backed vault, so the stub is staged where the FSM reads."""
    import silica.driver as driver_mod
    from silica.driver import fs_backend

    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setattr(CONFIG, "vault_path", str(v))
    monkeypatch.setattr(CONFIG, "inbox_dir", "Inbox")
    backend = fs_backend.ObsidianFSBackend(str(v))
    monkeypatch.setattr(driver_mod, "DRIVER", backend)
    driver_mod.set_driver(backend)
    return v


@pytest.fixture(autouse=True)
def _no_llm_target_pick(monkeypatch):
    import silica.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_pick_target_folder", lambda files: "Concepts")


@pytest.fixture
def gate(monkeypatch):
    """Stands in for the FSM: records the call, reports what it wrote."""
    calls: list[dict] = []
    outcome: dict = {"final_status": "Success", "notes": ["Concepts/Dog.md"]}

    class _FakeCoordinator:
        def __init__(self, **kw):
            from silica.driver import DRIVER
            calls.append({**kw, "body": DRIVER.read_note(kw["inbox_files"][0]).content})

        def run(self):
            # What DISTILL does on a real run: ephemerals of the chunk land in
            # the same episodic store (router/states/distill.py).
            if outcome.get("during_run"):
                from silica.kernel.recall.episodic import capture_from_distill

                capture_from_distill({"ephemerals": outcome["during_run"]},
                                     run_id="run-x", seen="2026-06-20")
            # What CLEANUP does on a real run: record which notes came from
            # this source. The stamp has no other way to learn the note path.
            from silica.kernel.write.provenance import append_record
            from pathlib import Path

            if outcome.get("notes") is not None:
                append_record(Path(calls[-1]["inbox_files"][0]).name, "sha1",
                              "run-x", outcome["notes"])
            return {"final_status": outcome["final_status"]}

    import silica.router.coordinator as coord_mod
    monkeypatch.setattr(coord_mod, "Coordinator", _FakeCoordinator)
    return calls, outcome


class TestPromoteOneChain:
    def test_the_chain_reaches_the_gate_as_a_staged_stub(self, vault, store, gate):
        _seed(store)
        calls, _ = gate

        assert _promote("/promote user.dog.name") == ""

        assert len(calls) == 1
        assert calls[0]["inbox_files"] == ["Inbox/user.dog.md"]  # the entity
        assert calls[0]["target_dir"] == "Concepts"
        assert "episodic_key: user.dog" in calls[0]["body"]
        assert "Tom" in calls[0]["body"]  # the chain, not just the key

    def test_the_stub_carries_every_attribute_of_the_entity(
        self, vault, store, gate
    ):
        """One note per entity: the dog's name and breed travel together, the
        city does not."""
        _seed(store)
        _seed(store, key="user.dog.breed", texts=("pastore tedesco",) * 3)
        _seed(store, key="user.city.name", texts=("Torino",) * 3)
        calls, _ = gate

        assert _promote("/promote user.dog.name") == ""

        assert calls[0]["inbox_files"] == ["Inbox/user.dog.md"]
        body = calls[0]["body"]
        assert "user.dog.name" in body and "user.dog.breed" in body
        assert "Tom" in body and "pastore tedesco" in body
        assert "Torino" not in body

    def test_the_run_is_told_not_to_feed_the_store_its_own_render(
        self, vault, store, gate
    ):
        """The stub IS the chain: distilling it back into the store nests the
        history inside itself, one level deeper per promotion (measured)."""
        _seed(store)
        calls, _ = gate

        assert _promote("/promote user.dog.name") == ""

        assert calls[0]["episodic_capture"] is False

    def test_the_run_distills_with_the_promotion_lens(self, vault, store, gate):
        """The stub is finished verbatim content: the default authoring lens +
        275-char floor rejected every real promotion (55/155/34 chars, all
        no_ops), and the extractive lens skipped every fact as 'time-bound
        personal' — the ingest-direction diversion, measured live. The
        promotion lens selects verbatim and never diverts to ephemerals."""
        _seed(store)
        calls, _ = gate

        assert _promote("/promote user.dog.name") == ""

        assert calls[0]["distill_profile"] == "promotion"

    def test_a_written_note_stamps_the_head_and_empties_the_queue(
        self, vault, store, gate
    ):
        from silica.kernel.recall.episodic import EpisodicStore

        _seed(store)

        assert _promote("/promote user.dog.name") == ""

        reloaded = EpisodicStore(path=store)
        (head,) = reloaded.live_facts()
        assert head.promoted == "Concepts/Dog.md"
        assert reloaded.nucleation_candidates(min_runs=3) == []

    def test_every_chain_of_the_entity_leaves_the_queue(self, vault, store, gate):
        """The note covers all the attributes, so all of them are spent — one
        unstamped sibling would keep suggesting a note that already exists."""
        from silica.kernel.recall.episodic import EpisodicStore

        _seed(store)
        _seed(store, key="user.dog.breed", texts=("pastore tedesco",) * 3)
        _seed(store, key="user.city.name", texts=("Torino",) * 3)

        assert _promote("/promote user.dog.name") == ""

        reloaded = EpisodicStore(path=store)
        by_key = {f.key: f for f in reloaded.live_facts()}
        assert by_key["user.dog.name"].promoted == "Concepts/Dog.md"
        assert by_key["user.dog.breed"].promoted == "Concepts/Dog.md"
        assert by_key["user.city.name"].promoted is None  # another entity, untouched
        assert [c.key for c in reloaded.nucleation_candidates(min_runs=3)] == [
            "user.city.name"
        ]

    def test_the_stamp_does_not_clobber_facts_captured_during_the_run(
        self, vault, store, gate, monkeypatch
    ):
        """The promotion run distills, and distilling captures into the SAME
        store — stamping a pre-run snapshot would erase what the run learned."""
        from silica.kernel.recall.episodic import EpisodicStore

        _seed(store)
        _, outcome = gate
        outcome["during_run"] = [{"key": "user.city", "text": "Torino"}]

        assert _promote("/promote user.dog.name") == ""

        reloaded = EpisodicStore(path=store)
        keys = {f.key: f for f in reloaded.live_facts()}
        assert keys["user.dog.name"].promoted == "Concepts/Dog.md"
        assert "user.city" in keys  # captured mid-run, must survive the stamp

    def test_a_chain_superseded_mid_run_is_stamped_on_its_new_head(
        self, vault, store, gate
    ):
        """The run can distill a newer value for the very key being promoted:
        the stamp belongs to the chain, so it follows the head forward."""
        from silica.kernel.recall.episodic import EpisodicStore

        _seed(store)
        _, outcome = gate
        outcome["during_run"] = [{"key": "user.dog.name", "text": "Fido"}]

        assert _promote("/promote user.dog.name") == ""

        reloaded = EpisodicStore(path=store)
        (head,) = reloaded.live_facts()
        assert head.text == "Fido"
        assert head.promoted == "Concepts/Dog.md"
        assert reloaded.nucleation_candidates(min_runs=3) == []  # out of the queue

    def test_the_stamp_points_at_the_entity_note_not_the_hub(
        self, vault, store, gate
    ):
        """Measured live: CLEANUP's provenance record lists the hub first
        (["Life/Life", "Life/Rex"]), and notes[0] stamped every chain with the
        HUB. The stamp is the overlay's solid edge and the 'already promoted
        to X' pointer — it must name the note that holds the facts."""
        from silica.kernel.recall.episodic import EpisodicStore

        _seed(store)
        _, outcome = gate
        outcome["notes"] = ["Concepts/Concepts", "Concepts/Dog.md"]  # hub first

        assert _promote("/promote user.dog.name") == ""

        reloaded = EpisodicStore(path=store)
        (head,) = reloaded.live_facts()
        assert head.promoted == "Concepts/Dog.md"

    def test_a_run_that_wrote_nothing_leaves_the_chain_in_the_queue(
        self, vault, store, gate
    ):
        from silica.kernel.recall.episodic import EpisodicStore

        _seed(store)
        _, outcome = gate
        outcome.update(final_status="failed", notes=None)  # gate refused the stub

        assert _promote("/promote user.dog.name") == ""

        reloaded = EpisodicStore(path=store)
        assert reloaded.live_facts()[0].promoted is None
        assert reloaded.nucleation_candidates(min_runs=3)  # still suggested


class TestRefusals:
    def test_a_key_that_walks_out_of_the_inbox_cannot(self, vault, store, gate):
        """Keys are model-authored: they name a file here, so they are input."""
        _seed(store, key="user../../../escaped")
        calls, _ = gate

        _promote("/promote user../../../escaped")

        if calls:  # if it ran at all, it ran on a file inside the inbox
            staged = (vault / calls[0]["inbox_files"][0]).resolve()
            assert staged.parent == (vault / "Inbox").resolve()


    def test_an_unknown_key_is_refused_without_touching_the_gate(
        self, vault, store, gate, capsys
    ):
        _seed(store)
        calls, _ = gate

        assert _promote("/promote user.cat.name") == ""

        assert calls == []
        assert "user.cat.name" in capsys.readouterr().out
        assert list(vault.rglob("*.md")) == []  # nothing staged either

    def test_an_already_promoted_key_is_refused(self, vault, store, gate, capsys):
        from silica.kernel.recall.episodic import EpisodicStore

        _seed(store)
        s = EpisodicStore(path=store)
        s.live_facts()[0].promoted = "Concepts/Dog.md"
        s.save()
        calls, _ = gate

        assert _promote("/promote user.dog.name") == ""

        assert calls == []
        out = capsys.readouterr().out
        assert "Concepts/Dog.md" in out  # says where it already lives
