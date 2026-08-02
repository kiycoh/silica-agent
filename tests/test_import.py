"""`silica import` — migration exploder: conversation exports into the WAL."""
from __future__ import annotations

import hashlib
import json

import pytest


@pytest.fixture
def wal(tmp_path, monkeypatch):
    import silica.kernel.recall.paths as paths
    monkeypatch.setattr(paths, "_SILICA_HOME", tmp_path / "silica-home")
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def _chatgpt_conversation(conv_id, text="tell me about vector search"):
    return {
        "title": f"Chat {conv_id}",
        "conversation_id": conv_id,
        "current_node": "b",
        "mapping": {
            "a": {"id": "a", "parent": None, "children": ["b"], "message": {
                "author": {"role": "user"}, "create_time": 1000.0,
                "content": {"content_type": "text", "parts": [text]}}},
            "b": {"id": "b", "parent": "a", "children": [], "message": {
                "author": {"role": "assistant"}, "create_time": 1001.0,
                "content": {"content_type": "text", "parts": ["y" * 300]}}},
        },
    }


def _claude_ai_conversation(uuid, text="how does the write gate work?"):
    return {
        "uuid": uuid, "name": f"Chat {uuid}",
        "chat_messages": [
            {"sender": "human", "text": text, "created_at": "2026-07-01T09:00:00Z"},
            {"sender": "assistant", "text": "z" * 300,
             "created_at": "2026-07-01T09:00:04Z"},
        ],
    }


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class TestChatgptExport:
    def test_bare_conversations_json_becomes_one_envelope_per_chat(self, tmp_path, wal):
        from silica.capture import run_import
        from silica.kernel.recall.paths import inbox_dir_for

        export = tmp_path / "conversations.json"
        export.write_text(json.dumps(
            [_chatgpt_conversation("c1"), _chatgpt_conversation("c2")]), encoding="utf-8")

        created, skipped = run_import(str(export), str(wal))

        assert (created, skipped) == (2, 0)
        d = inbox_dir_for(str(wal))
        env = d / f"chatgpt-{_digest('c1')}.json"
        assert env.is_file()
        data = json.loads(env.read_text(encoding="utf-8"))
        assert data["source"] == "chatgpt"
        assert data["event"] == "import"
        assert data["format"] == "chatgpt-mapping"
        assert data["title"] == "Chat c1"
        assert data["cwd"] == ""
        assert json.loads(data["payload"])["conversation_id"] == "c1"

    def test_tiny_conversations_are_skipped(self, tmp_path, wal):
        """A one-line "hi" costs an LLM call to learn it said nothing."""
        from silica.capture import run_import
        from silica.kernel.recall.paths import inbox_dir_for

        one_message = _chatgpt_conversation("short")
        del one_message["mapping"]["b"]
        thin = _chatgpt_conversation("thin", text="hi")
        thin["mapping"]["b"]["message"]["content"]["parts"] = ["hey"]

        export = tmp_path / "conversations.json"
        export.write_text(json.dumps([one_message, thin, _chatgpt_conversation("real")]),
                          encoding="utf-8")

        created, skipped = run_import(str(export), str(wal))

        assert (created, skipped) == (1, 2)
        assert [p.name for p in inbox_dir_for(str(wal)).glob("*.json")] == [
            f"chatgpt-{_digest('real')}.json"]


CC_TRANSCRIPT = "\n".join(json.dumps(row) for row in (
    {"type": "user", "message": {"role": "user", "content": "x" * 200},
     "timestamp": "2026-07-01T09:00:00Z"},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "y" * 200}]}, "timestamp": "2026-07-01T09:00:03Z"},
))


class TestExportArchive:
    def test_a_zipped_export_is_read_without_unpacking_it_by_hand(self, tmp_path, wal):
        import zipfile

        from silica.capture import run_import
        from silica.kernel.recall.paths import inbox_dir_for

        archive = tmp_path / "export.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("chatgpt-export/conversations.json",
                       json.dumps([_chatgpt_conversation("c1")]))
            z.writestr("chatgpt-export/user.json", "{}")

        assert run_import(str(archive), str(wal)) == (1, 0)
        assert (inbox_dir_for(str(wal)) / f"chatgpt-{_digest('c1')}.json").is_file()


class TestLocalClaudeCodeTranscripts:
    def test_a_directory_of_transcripts_is_imported_by_session_id(self, tmp_path, wal):
        from silica.capture import run_import
        from silica.kernel.recall.paths import inbox_dir_for

        projects = tmp_path / "projects"
        (projects / "-home-u-repo").mkdir(parents=True)
        (projects / "-home-u-repo" / "sess-1.jsonl").write_text(CC_TRANSCRIPT,
                                                                encoding="utf-8")
        (projects / "-home-u-other" / "nested").mkdir(parents=True)
        (projects / "-home-u-other" / "nested" / "sess-2.jsonl").write_text(
            CC_TRANSCRIPT, encoding="utf-8")

        assert run_import(str(projects), str(wal)) == (2, 0)

        env = inbox_dir_for(str(wal)) / "claude-code-sess-1-end.json"
        data = json.loads(env.read_text(encoding="utf-8"))
        assert data["source"] == "claude-code"
        assert data["format"] == "claude-code-jsonl"
        assert data["event"] == "import"
        assert data["payload"] == CC_TRANSCRIPT
        assert (inbox_dir_for(str(wal)) / "claude-code-sess-2-end.json").is_file()

    def test_a_live_captured_session_is_not_imported_again(self, tmp_path, wal):
        """Retroactive import reuses the live envelope name, so the hook wins."""
        from silica.capture import run_import, write_envelope
        from silica.kernel.recall.paths import inbox_dir_for

        write_envelope(str(wal), "claude-code-sess-1-end.json",
                       {"version": 1, "source": "claude-code", "event": "session_end",
                        "format": "claude-code-jsonl", "captured_at": "", "title": "",
                        "session_id": "sess-1", "cwd": "/repo", "payload": "live"})
        transcript = tmp_path / "sess-1.jsonl"
        transcript.write_text(CC_TRANSCRIPT, encoding="utf-8")

        assert run_import(str(transcript), str(wal)) == (0, 1)

        env = inbox_dir_for(str(wal)) / "claude-code-sess-1-end.json"
        assert json.loads(env.read_text(encoding="utf-8"))["payload"] == "live"


class TestImportSubcommand:
    def test_import_reports_counts_and_how_to_drain(self, tmp_path, wal, monkeypatch, capsys):
        from silica.cli import _dispatch_subcommand
        from silica.config import CONFIG
        from silica.kernel.recall.paths import inbox_dir_for

        monkeypatch.setattr(CONFIG, "vault_path", str(wal))
        export = tmp_path / "conversations.json"
        export.write_text(json.dumps(
            [_chatgpt_conversation("c1"), _chatgpt_conversation("c2", text="hi")]),
            encoding="utf-8")

        assert _dispatch_subcommand(["import", str(export)]) == 0

        out = capsys.readouterr().out
        assert "1" in out and "/nucleate" in out
        assert (inbox_dir_for(str(wal)) / f"chatgpt-{_digest('c1')}.json").is_file()

    def test_import_without_a_path_is_a_clean_refusal(self, wal, monkeypatch, capsys):
        from silica.cli import _dispatch_subcommand
        from silica.config import CONFIG

        monkeypatch.setattr(CONFIG, "vault_path", str(wal))
        assert _dispatch_subcommand(["import"]) == 1
        assert "silica import" in capsys.readouterr().out


class TestIdempotency:
    def test_importing_the_same_export_twice_creates_nothing_new(self, tmp_path, wal):
        from silica.capture import run_import
        from silica.kernel.recall.paths import inbox_dir_for

        export = tmp_path / "conversations.json"
        export.write_text(json.dumps([_chatgpt_conversation("c1")]), encoding="utf-8")

        assert run_import(str(export), str(wal)) == (1, 0)
        assert run_import(str(export), str(wal)) == (0, 1)
        assert len(list(inbox_dir_for(str(wal)).glob("*.json"))) == 1

    def test_an_already_drained_conversation_is_not_re_imported(self, tmp_path, wal):
        """Existence blocks re-import, not content: a housekeeping-truncated
        tombstone still means 'this one already went through'."""
        import os

        from silica.capture import mark_processed, run_import
        from silica.kernel.recall.paths import inbox_dir_for

        export = tmp_path / "conversations.json"
        export.write_text(json.dumps([_chatgpt_conversation("c1")]), encoding="utf-8")
        run_import(str(export), str(wal))
        drained = mark_processed(inbox_dir_for(str(wal)) / f"chatgpt-{_digest('c1')}.json")
        os.truncate(drained, 0)

        assert run_import(str(export), str(wal)) == (0, 1)
        assert list(inbox_dir_for(str(wal)).glob("*.json")) == []


class TestClaudeAiExport:
    def test_the_same_file_name_is_disambiguated_by_schema(self, tmp_path, wal):
        from silica.capture import run_import
        from silica.kernel.recall.paths import inbox_dir_for

        export = tmp_path / "conversations.json"
        export.write_text(json.dumps([_claude_ai_conversation("u1")]), encoding="utf-8")

        assert run_import(str(export), str(wal)) == (1, 0)

        env = inbox_dir_for(str(wal)) / f"claude-ai-{_digest('u1')}.json"
        data = json.loads(env.read_text(encoding="utf-8"))
        assert data["source"] == "claude-ai"
        assert data["format"] == "claude-ai-messages"
        assert data["title"] == "Chat u1"
