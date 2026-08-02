"""`/nucleate` with no argument drains the capture WAL of the current vault."""
from __future__ import annotations

import json

import pytest

from silica.config import CONFIG


@pytest.fixture(autouse=True)
def _reset_manifest_cache():
    from silica.kernel.vault_manifest import reset_manifest_cache
    reset_manifest_cache()
    yield
    reset_manifest_cache()


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A real FS-backed vault plus a sandboxed WAL for it."""
    import silica.driver as driver_mod
    import silica.kernel.recall.paths as paths
    from silica.driver import fs_backend

    monkeypatch.setattr(paths, "_SILICA_HOME", tmp_path / "silica-home")
    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setattr(CONFIG, "vault_path", str(v))
    monkeypatch.setattr(CONFIG, "inbox_dir", "Inbox")
    backend = fs_backend.ObsidianFSBackend(str(v))
    monkeypatch.setattr(driver_mod, "DRIVER", backend)
    driver_mod.set_driver(backend)
    return v


@pytest.fixture
def stub_coordinator(monkeypatch):
    calls: list[dict] = []

    class _FakeCoordinator:
        def __init__(self, **kw):
            calls.append(kw)

        def run(self):
            return {"final_status": "Success"}

    import silica.router.coordinator as coord_mod
    monkeypatch.setattr(coord_mod, "Coordinator", _FakeCoordinator)
    return calls


@pytest.fixture(autouse=True)
def _no_llm_target_pick(monkeypatch):
    import silica.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_pick_target_folder", lambda files: "Sessions")


TRANSCRIPT = "\n".join(json.dumps(row) for row in (
    {"type": "user", "message": {"role": "user", "content": "why is recall fused?"},
     "timestamp": "2026-08-01T10:00:00.000Z"},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Because one leg misses what the others catch."}]},
     "timestamp": "2026-08-01T10:00:09.000Z"},
))


def _envelope(session_id="s1", payload=TRANSCRIPT):
    return {
        "version": 1, "source": "claude-code", "event": "session_end",
        "format": "claude-code-jsonl", "captured_at": "2026-08-01T10:00:00+00:00",
        "session_id": session_id, "cwd": "/repo", "title": "", "payload": payload,
    }


SESSION = json.dumps([
    {"role": "user", "content": "remember I moved the reranker to jina v3", "ts": ""},
    {"role": "assistant", "content": "Noted. " + "context " * 60, "ts": ""},
])


def _silica_envelope(session_id="s1", payload=SESSION, notes_touched=()):
    return {
        "version": 1, "source": "silica", "event": "session_end",
        "format": "silica-session", "captured_at": "2026-08-01T10:00:00+00:00",
        "session_id": session_id, "cwd": "/repo", "title": "", "payload": payload,
        "driver": "tui", "notes_touched": list(notes_touched),
    }


def _drain():
    from silica.cli import _expand_workflow_shortcut
    return _expand_workflow_shortcut("/nucleate")


class TestDrain:
    def test_transcript_is_staged_for_the_fsm_and_the_envelope_retired(
        self, vault, stub_coordinator
    ):
        from silica.capture import write_envelope
        from silica.kernel.recall.paths import inbox_dir_for

        v = str(vault)
        env = write_envelope(v, "claude-code-s1-end.json", _envelope())

        assert _drain() == ""  # handled inline, no agent turn

        assert len(stub_coordinator) == 1
        staged = stub_coordinator[0]["inbox_files"]
        assert staged == ["Inbox/claude-code-s1-end.md"]
        assert stub_coordinator[0]["target_dir"] == "Sessions"
        # the envelope is retired, and no transcript is left behind in the vault
        assert not env.exists()
        assert (inbox_dir_for(v) / "processed" / "claude-code-s1-end.json").is_file()
        assert not (vault / "Inbox" / "claude-code-s1-end.md").exists()

    def test_the_staged_text_is_the_rendered_conversation(self, vault, monkeypatch):
        """What the FSM reads must be the transcript, at the staged path."""
        from silica.capture import write_envelope

        seen = {}

        class _Peek:
            def __init__(self, **kw):
                seen["files"] = kw["inbox_files"]

            def run(self):
                from silica.driver import DRIVER
                seen["body"] = DRIVER.read_note(seen["files"][0]).content
                return {"final_status": "Success"}

        import silica.router.coordinator as coord_mod
        monkeypatch.setattr(coord_mod, "Coordinator", _Peek)

        write_envelope(str(vault), "claude-code-s1-end.json", _envelope())
        _drain()

        assert "why is recall fused?" in seen["body"]
        assert "Because one leg misses what the others catch." in seen["body"]

    def test_no_conversation_text_survives_the_fsm_archiving_its_source(
        self, vault, monkeypatch
    ):
        """A successful run moves the source to done/ before the drain unstages it.

        The stub runs the real archive call CLEANUP makes (finalize.py), so the
        assertion is the invariant and not a guess about where the file went.
        """
        from silica.capture import write_envelope

        class _Archiving:
            def __init__(self, **kw):
                self.files = kw["inbox_files"]

            def run(self):
                from silica.tools.wrapped import silica_cleanup
                for f in self.files:
                    assert silica_cleanup(f, "done").get("success")
                return {"final_status": "Success"}

        import silica.router.coordinator as coord_mod
        monkeypatch.setattr(coord_mod, "Coordinator", _Archiving)

        write_envelope(str(vault), "claude-code-s1-end.json", _envelope())

        assert _drain() == ""

        leaked = [
            p.relative_to(vault).as_posix()
            for p in vault.rglob("*") if p.is_file()
            and "why is recall fused?" in p.read_text(encoding="utf-8", errors="ignore")
        ]
        assert leaked == []

    def test_unparseable_envelope_is_kept_for_autopsy(self, vault, stub_coordinator):
        from silica.kernel.recall.paths import inbox_dir_for

        d = inbox_dir_for(str(vault))
        d.mkdir(parents=True, exist_ok=True)
        (d / "claude-code-bad-end.json").write_text("{ truncated", encoding="utf-8")

        assert _drain() == ""

        assert (d / "failed" / "claude-code-bad-end.json").is_file()
        assert not (d / "claude-code-bad-end.json").exists()
        assert stub_coordinator == []  # nothing to distill

    def test_a_silent_session_is_retired_without_an_llm_call(
        self, vault, stub_coordinator
    ):
        """Zero conversation turns is success, not failure."""
        from silica.capture import write_envelope
        from silica.kernel.recall.paths import inbox_dir_for

        v = str(vault)
        write_envelope(v, "claude-code-s1-end.json",
                       _envelope(payload=json.dumps({"type": "mode"})))

        assert _drain() == ""

        assert stub_coordinator == []
        assert (inbox_dir_for(v) / "processed" / "claude-code-s1-end.json").is_file()

    def test_a_crashing_run_still_takes_the_transcript_out_of_the_vault(
        self, vault, monkeypatch
    ):
        """The privacy invariant does not depend on the run finishing."""
        from silica.capture import write_envelope

        class _Boom:
            def __init__(self, **kw):
                pass

            def run(self):
                raise RuntimeError("provider down")

        import silica.router.coordinator as coord_mod
        monkeypatch.setattr(coord_mod, "Coordinator", _Boom)

        v = str(vault)
        env = write_envelope(v, "claude-code-s1-end.json", _envelope())

        with pytest.raises(RuntimeError):
            _drain()

        assert not (vault / "Inbox" / "claude-code-s1-end.md").exists()
        assert env.is_file()  # retried by the next drain

    # Every terminal shape the FSM actually produces: Success/no_ops/partial/
    # failed from finalize.py and _contain_chunk_failure, already_nucleated from
    # the ledger short-circuit, and a bare error from a run that never reached a
    # verdict. `error` alone does not mean the run failed — best-effort phases
    # record theirs and carry on (orchestrator._on_step_error).
    @pytest.mark.parametrize("result,retired", [
        ({"final_status": "Success"}, True),
        ({"final_status": "no_ops"}, True),
        ({"final_status": "already_nucleated"}, True),
        ({"final_status": "Success", "error": "AUTOLINK failed"}, True),
        ({"final_status": "partial", "failed_chunks": [{"chunk": "f0_c1"}]}, False),
        ({"final_status": "failed", "failed_chunks": [{"chunk": "f0_c0"}]}, False),
        ({"error": "provider down"}, False),
    ])
    def test_the_envelope_is_retired_only_on_the_fsm_verdict(
        self, vault, monkeypatch, result, retired
    ):
        from silica.capture import write_envelope

        class _Fixed:
            def __init__(self, **kw):
                pass

            def run(self):
                return dict(result)

        import silica.router.coordinator as coord_mod
        monkeypatch.setattr(coord_mod, "Coordinator", _Fixed)

        v = str(vault)
        env = write_envelope(v, "claude-code-s1-end.json", _envelope())

        assert _drain() == ""

        assert env.exists() is not retired  # pending means the next drain retries
        assert not (vault / "Inbox" / "claude-code-s1-end.md").exists()


class TestEpisodicRouting:
    """Silica's own sessions never take the note path (spec §11)."""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        from silica.kernel.recall import episodic
        path = tmp_path / "episodic.json"
        monkeypatch.setattr(episodic, "store_path", lambda: path)
        return path

    @pytest.fixture
    def distiller(self, monkeypatch):
        """Stands in for the LLM; records what it was asked to distill."""
        seen: dict = {}

        def _run(**kwargs):
            seen["kwargs"] = kwargs
            return {
                "updates": [{"action": "create", "path": "Notes/Reranker.md",
                             "content": "# Reranker"}],
                "ephemerals": [{"key": "silica.reranker", "text": "reranker is jina v3"}],
            }

        import silica.kernel.prep_delegation as prep
        monkeypatch.setattr(prep, "run_distiller", _run)
        return seen

    def test_a_session_becomes_facts_and_never_a_note(
        self, vault, store, distiller, monkeypatch
    ):
        from silica.capture import write_envelope
        from silica.kernel.recall.episodic import EpisodicStore
        from silica.kernel.recall.paths import inbox_dir_for

        class _Never:
            def __init__(self, **kw):
                raise AssertionError("machine memory must not reach the FSM")

        import silica.router.coordinator as coord_mod
        monkeypatch.setattr(coord_mod, "Coordinator", _Never)

        v = str(vault)
        env = write_envelope(v, "silica-s1-end.json", _silica_envelope())

        assert _drain() == ""

        (fact,) = EpisodicStore(path=store).live_facts()
        assert fact.key == "silica.reranker"
        assert not env.exists()
        assert (inbox_dir_for(v) / "processed" / "silica-s1-end.json").is_file()
        assert list(vault.rglob("*.md")) == []  # the distilled body is dropped

    def test_facts_are_stamped_with_their_vault_and_the_notes_of_the_session(
        self, vault, store, distiller, monkeypatch
    ):
        """Provenance for the /graph overlay: which vault, which notes (spec §11)."""
        from silica.capture import write_envelope
        from silica.kernel.recall.episodic import EpisodicStore
        from silica.kernel.recall.paths import vault_digest

        import silica.router.coordinator as coord_mod
        monkeypatch.setattr(coord_mod, "Coordinator", None)

        v = str(vault)
        write_envelope(v, "silica-s1-end.json",
                       _silica_envelope(notes_touched=["Concepts/Rerank.md"]))

        assert _drain() == ""

        (fact,) = EpisodicStore(path=store).live_facts()
        assert fact.vault == vault_digest(v)
        assert fact.notes == ["Concepts/Rerank.md"]

    def test_the_distiller_is_told_which_keys_already_exist(
        self, vault, store, distiller, monkeypatch
    ):
        """ADR-0021, measured on a real 3-session run: without the established
        keys in its prompt the distiller coins a synonym every session
        (user.pet.name, user.dog.name, user.pet.rex.status for one dog), no
        chain ever reaches min_runs and the promotion queue is empty by
        construction. The note path gets this section through build_substrate;
        this lane calls the distiller directly and has to pass it itself."""
        from silica.capture import write_envelope
        from silica.kernel.recall.episodic import EpisodicStore

        import silica.router.coordinator as coord_mod
        monkeypatch.setattr(coord_mod, "Coordinator", None)

        seeded = EpisodicStore(path=store)
        seeded.capture([{"key": "user.dog.name", "text": "Rex"}],
                       run_id="r0", seen="2026-07-01")
        write_envelope(str(vault), "silica-s1-end.json", _silica_envelope())

        assert _drain() == ""

        assert "user.dog.name" in (distiller["kwargs"].get("substrate") or "")

    def test_the_drain_never_pays_for_note_bodies(
        self, vault, store, distiller, monkeypatch
    ):
        """This lane keeps only ephemerals (the distilled bodies are dropped,
        see test above) — structure_only tells run_distiller to use the
        body-less schema and skip the body pass, so those tokens are never
        generated in the first place."""
        from silica.capture import write_envelope

        import silica.router.coordinator as coord_mod
        monkeypatch.setattr(coord_mod, "Coordinator", None)

        write_envelope(str(vault), "silica-s1-end.json", _silica_envelope())

        assert _drain() == ""

        assert distiller["kwargs"].get("structure_only") is True

    def test_a_session_worth_no_facts_is_still_done(self, vault, store, monkeypatch):
        """Nothing worth remembering is success, not failure."""
        from silica.capture import write_envelope
        from silica.kernel.recall.paths import inbox_dir_for

        import silica.kernel.prep_delegation as prep
        monkeypatch.setattr(prep, "run_distiller",
                            lambda **kw: {"updates": [], "ephemerals": []})

        v = str(vault)
        write_envelope(v, "silica-s1-end.json", _silica_envelope())

        assert _drain() == ""

        assert (inbox_dir_for(v) / "processed" / "silica-s1-end.json").is_file()

    def test_a_distiller_failure_leaves_the_session_pending(
        self, vault, store, monkeypatch
    ):
        from silica.capture import write_envelope

        import silica.kernel.prep_delegation as prep
        monkeypatch.setattr(prep, "run_distiller",
                            lambda **kw: {"error": "provider down"})

        v = str(vault)
        env = write_envelope(v, "silica-s1-end.json", _silica_envelope())

        assert _drain() == ""

        assert env.is_file()  # the next drain repeats the call
        assert list(vault.rglob("*.md")) == []
