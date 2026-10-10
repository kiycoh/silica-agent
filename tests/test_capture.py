"""Capture lane: the `silica capture` hook producer and its WAL."""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def wal(tmp_path, monkeypatch):
    """A sandboxed ~/.silica and a vault directory that opts into capture."""
    import silica.kernel.recall.paths as paths
    monkeypatch.setattr(paths, "_SILICA_HOME", tmp_path / "silica-home")
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "vault.yaml").write_text("", encoding="utf-8")
    return vault


def _hook(vault, transcript, *, event="SessionEnd", session_id="abc", cwd=None):
    return json.dumps({
        "hook_event_name": event,
        "session_id": session_id,
        "transcript_path": str(transcript),
        "cwd": str(cwd or vault),
    })


def _transcript(tmp_path, name="t.jsonl", size=2048):
    p = tmp_path / name
    p.write_text("x" * size, encoding="utf-8")
    return p


class TestCapture:
    def test_session_end_writes_envelope(self, tmp_path, wal):
        from silica.capture import run_capture
        from silica.kernel.recall.paths import inbox_dir_for

        t = _transcript(tmp_path)
        assert run_capture(_hook(wal, t)) == 0

        env = inbox_dir_for(str(wal)) / "claude-code-abc-end.json"
        data = json.loads(env.read_text(encoding="utf-8"))
        assert data["version"] == 1
        assert data["source"] == "claude-code"
        assert data["event"] == "session_end"
        assert data["format"] == "claude-code-jsonl"
        assert data["session_id"] == "abc"
        assert data["cwd"] == str(wal)
        assert data["payload"] == t.read_text(encoding="utf-8")
        assert data["captured_at"]

    def test_vault_found_by_walking_up_from_subdir(self, tmp_path, wal):
        """Claude Code launched from a subdirectory still captures to the repo."""
        from silica.capture import run_capture
        from silica.kernel.recall.paths import inbox_dir_for

        sub = wal / "silica" / "kernel"
        sub.mkdir(parents=True)
        t = _transcript(tmp_path)
        assert run_capture(_hook(wal, t, cwd=sub)) == 0

        assert (inbox_dir_for(str(wal)) / "claude-code-abc-end.json").is_file()

    @pytest.mark.parametrize("case", ["garbage", "missing_transcript", "no_vault"])
    def test_fail_open_leaves_the_wal_untouched(self, tmp_path, wal, case):
        """A capture bug must never break or noise up a session."""
        from silica.capture import run_capture
        from silica.kernel.recall.paths import inbox_dir_for

        if case == "garbage":
            stdin = "not json at all {"
        elif case == "missing_transcript":
            stdin = _hook(wal, tmp_path / "gone.jsonl")
        else:
            outside = tmp_path / "outside"
            outside.mkdir()
            stdin = _hook(outside, _transcript(tmp_path), cwd=outside)

        assert run_capture(stdin) == 0
        assert not inbox_dir_for(str(wal)).exists()

    def test_trivial_transcript_is_skipped(self, tmp_path, wal):
        from silica.capture import run_capture
        from silica.kernel.recall.paths import inbox_dir_for

        t = _transcript(tmp_path, size=10)
        assert run_capture(_hook(wal, t)) == 0
        assert not inbox_dir_for(str(wal)).exists()

    def test_envelope_is_private_and_leaves_no_temp_file(self, tmp_path, wal):
        """Private conversation data: owner-only, and no half-written residue."""
        from silica.capture import run_capture
        from silica.kernel.recall.paths import inbox_dir_for

        assert run_capture(_hook(wal, _transcript(tmp_path))) == 0

        d = inbox_dir_for(str(wal))
        env = d / "claude-code-abc-end.json"
        assert env.stat().st_mode & 0o777 == 0o600
        assert d.stat().st_mode & 0o777 == 0o700
        assert list(d.glob(".tmp-*")) == []

    def test_precompact_gets_its_own_timestamped_name(self, tmp_path, wal):
        """Several compactions per session: each keeps its own envelope."""
        from silica.capture import run_capture
        from silica.kernel.recall.paths import inbox_dir_for

        assert run_capture(_hook(wal, _transcript(tmp_path), event="PreCompact")) == 0

        found = sorted(p.name for p in inbox_dir_for(str(wal)).glob("*.json"))
        assert len(found) == 1
        assert found[0].startswith("claude-code-abc-precompact-")
        data = json.loads((inbox_dir_for(str(wal)) / found[0]).read_text(encoding="utf-8"))
        assert data["event"] == "pre_compact"

    def test_same_session_end_captured_twice_stays_one_envelope(self, tmp_path, wal):
        """Deterministic names give idempotency for free."""
        from silica.capture import run_capture
        from silica.kernel.recall.paths import inbox_dir_for

        t = _transcript(tmp_path)
        run_capture(_hook(wal, t))
        t.write_text("y" * 3000, encoding="utf-8")
        run_capture(_hook(wal, t))

        envs = list(inbox_dir_for(str(wal)).glob("*.json"))
        assert len(envs) == 1
        assert json.loads(envs[0].read_text(encoding="utf-8"))["payload"] == "y" * 3000


def _envelope(session_id="s1", event="session_end", payload="x",
              source="claude-code"):
    return {
        "version": 1, "source": source, "event": event,
        "format": "claude-code-jsonl", "captured_at": "2026-08-01T10:00:00+00:00",
        "session_id": session_id, "cwd": "/repo", "title": "", "payload": payload,
    }


class TestDrainSelection:
    def test_session_end_supersedes_the_precompacts_of_its_session(self, wal):
        """The end-of-session transcript is cumulative: it contains them."""
        from silica.capture import collect, write_envelope
        from silica.kernel.recall.paths import inbox_dir_for

        v = str(wal)
        write_envelope(v, "claude-code-s1-precompact-20260801T090000Z.json",
                       _envelope(event="pre_compact"))
        write_envelope(v, "claude-code-s1-end.json", _envelope())

        envelopes, remaining = collect(v)

        assert [p.name for p in envelopes] == ["claude-code-s1-end.json"]
        assert remaining == 0
        processed = inbox_dir_for(v) / "processed"
        assert (processed / "claude-code-s1-precompact-20260801T090000Z.json").is_file()

    def test_latest_precompact_wins_when_the_session_has_no_end(self, wal):
        from silica.capture import collect, write_envelope
        from silica.kernel.recall.paths import inbox_dir_for

        v = str(wal)
        for stamp in ("20260801T090000Z", "20260801T110000Z", "20260801T100000Z"):
            write_envelope(v, f"claude-code-s1-precompact-{stamp}.json",
                           _envelope(event="pre_compact"))

        envelopes, _ = collect(v)

        assert [p.name for p in envelopes] == [
            "claude-code-s1-precompact-20260801T110000Z.json"]
        assert len(list((inbox_dir_for(v) / "processed").glob("*.json"))) == 2

    def test_other_sessions_are_untouched_by_the_supersede_rule(self, wal):
        from silica.capture import collect, write_envelope

        v = str(wal)
        write_envelope(v, "claude-code-s1-end.json", _envelope("s1"))
        write_envelope(v, "claude-code-s2-precompact-20260801T090000Z.json",
                       _envelope("s2", event="pre_compact"))

        envelopes, _ = collect(v)

        assert {p.name for p in envelopes} == {
            "claude-code-s1-end.json",
            "claude-code-s2-precompact-20260801T090000Z.json",
        }

    def test_a_session_clear_is_not_superseded_by_that_session_end(self, wal):
        """`/clear` destroys the conversation: the end transcript lacks it."""
        from silica.capture import collect, write_envelope

        v = str(wal)
        write_envelope(v, "silica-s1-clear-20260801T090000Z.json",
                       _envelope(event="session_clear", source="silica"))
        write_envelope(v, "silica-s1-end.json", _envelope(source="silica"))

        envelopes, _ = collect(v)

        assert {p.name for p in envelopes} == {
            "silica-s1-clear-20260801T090000Z.json",
            "silica-s1-end.json",
        }

    def test_each_clear_of_a_session_is_its_own_conversation(self, wal):
        """Two `/clear`s are two conversations, so neither subsumes the other."""
        from silica.capture import collect, write_envelope

        v = str(wal)
        for stamp in ("20260801T090000Z", "20260801T100000Z"):
            write_envelope(v, f"silica-s1-clear-{stamp}.json",
                           _envelope(event="session_clear", source="silica"))

        envelopes, _ = collect(v)

        assert len(envelopes) == 2

    def test_drain_is_capped_and_reports_what_is_left(self, wal):
        """A 500-conversation backlog drains in resumable batches, not one bill."""
        from silica.capture import collect, write_envelope

        v = str(wal)
        for i in range(13):
            write_envelope(v, f"chatgpt-{i:03d}.json", _envelope(session_id=f"c{i}"))

        envelopes, remaining = collect(v, cap=10)

        assert len(envelopes) == 10
        assert remaining == 3


def _history(n=4):
    """A session long enough to clear the trivia floor."""
    turns = [
        {"role": "system", "content": "you are silica"},
        {"role": "user", "content": "how does the write gate work? " + "x" * 400},
    ]
    for i in range(n):
        turns.append({"role": "assistant", "content": f"turn {i}: " + "y" * 400})
        turns.append({"role": "user", "content": f"and then? {i} " + "z" * 400})
    return turns


class TestOwnSessions:
    """Silica's own conversations: the third provenance class (spec §10)."""

    @pytest.fixture(autouse=True)
    def _opt_in(self, wal, monkeypatch):
        from silica.config import CONFIG
        monkeypatch.setattr(CONFIG, "vault_path", str(wal))
        monkeypatch.setattr(CONFIG, "capture_sessions", True)

    def test_the_end_of_a_session_writes_a_silica_envelope(self, wal):
        from silica.capture import capture_session
        from silica.kernel.recall.paths import inbox_dir_for

        capture_session(_history(), session_id="s1", driver="tui")

        data = json.loads(
            (inbox_dir_for(str(wal)) / "silica-s1-end.json").read_text(encoding="utf-8")
        )
        assert data["version"] == 1
        assert data["source"] == "silica"
        assert data["event"] == "session_end"
        assert data["format"] == "silica-session"
        assert data["driver"] == "tui"
        assert data["session_id"] == "s1"

    def test_only_the_conversation_is_captured(self, wal):
        """Tool noise and system scaffolding are dropped at the source."""
        from silica.capture import capture_session
        from silica.kernel.recall.paths import inbox_dir_for

        history = _history() + [
            {"role": "tool", "content": "{'hits': []}"},
            {"role": "assistant", "content": ""},
        ]
        capture_session(history, session_id="s1", driver="tui")

        payload = json.loads(json.loads(
            (inbox_dir_for(str(wal)) / "silica-s1-end.json").read_text(encoding="utf-8")
        )["payload"])
        assert {t["role"] for t in payload} == {"user", "assistant"}
        assert all(t["content"] for t in payload)


    def test_a_clear_gets_its_own_timestamped_envelope(self, wal):
        """A session ends once but can be cleared many times."""
        from silica.capture import capture_session

        path = capture_session(_history(), session_id="s1", driver="tui",
                               event="session_clear")

        assert path.name.startswith("silica-s1-clear-")
        assert json.loads(path.read_text(encoding="utf-8"))["event"] == "session_clear"


    def test_a_session_that_said_nothing_is_not_captured(self, wal):
        """A greeting is not a conversation: the drain would pay to learn that."""
        from silica.capture import capture_session

        assert capture_session(
            [{"role": "user", "content": "hi"},
             {"role": "assistant", "content": "hello, what are we doing?"}],
            session_id="s1", driver="tui",
        ) is None

    def test_nothing_is_captured_while_the_knob_is_off(self, wal, monkeypatch):
        from silica.capture import capture_session
        from silica.config import CONFIG
        from silica.kernel.recall.paths import inbox_dir_for

        monkeypatch.setattr(CONFIG, "capture_sessions", False)

        assert capture_session(_history(), session_id="s1", driver="tui") is None
        assert list(inbox_dir_for(str(wal)).glob("*.json")) == []


class TestHousekeeping:
    def test_old_processed_envelopes_are_truncated_not_deleted(self, wal):
        """The name is what carries import idempotency: reclaim the disk, keep
        the tombstone."""
        import os
        import time

        from silica.capture import housekeep, mark_processed, write_envelope
        from silica.kernel.recall.paths import inbox_dir_for

        v = str(wal)
        old = mark_processed(write_envelope(v, "chatgpt-old.json", _envelope(payload="a" * 500)))
        fresh = mark_processed(write_envelope(v, "chatgpt-new.json", _envelope(payload="b" * 500)))
        long_ago = time.time() - 31 * 86400
        os.utime(old, (long_ago, long_ago))

        housekeep(v)

        assert old.is_file()
        assert old.stat().st_size == 0
        assert fresh.stat().st_size > 0
        assert (inbox_dir_for(v) / "processed" / "chatgpt-old.json").is_file()


class TestCaptureSubcommand:
    def test_capture_reads_the_hook_json_from_stdin(self, tmp_path, wal, monkeypatch):
        import io
        import sys

        from silica.cli import _dispatch_subcommand
        from silica.kernel.recall.paths import inbox_dir_for

        monkeypatch.setattr(sys, "stdin", io.StringIO(_hook(wal, _transcript(tmp_path))))
        assert _dispatch_subcommand(["capture"]) == 0
        assert (inbox_dir_for(str(wal)) / "claude-code-abc-end.json").is_file()

    def test_capture_exits_zero_even_when_the_producer_explodes(self, monkeypatch):
        """The hook's exit code is the session's problem, so it is always 0."""
        import silica.capture as capture_mod
        from silica.cli import _dispatch_subcommand

        def boom(_):
            raise RuntimeError("producer bug")

        monkeypatch.setattr(capture_mod, "run_capture", boom)
        assert _dispatch_subcommand(["capture"]) == 0
