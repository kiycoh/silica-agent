"""Transcript adapter: the single owner of conversation-format parsing."""
from __future__ import annotations

import json


def _envelope(payload, **over):
    env = {
        "version": 1,
        "source": "claude-code",
        "event": "session_end",
        "format": "claude-code-jsonl",
        "captured_at": "2026-08-01T10:00:00+00:00",
        "session_id": "abc",
        "cwd": "/home/u/repo",
        "title": "",
        "payload": payload,
    }
    env.update(over)
    return env


def _jsonl(*lines):
    return "\n".join(json.dumps(line) for line in lines)


CLAUDE_CODE = _jsonl(
    {"type": "mode", "mode": "normal"},
    {"type": "user", "message": {"role": "user", "content": "how does recall work?"},
     "timestamp": "2026-08-01T10:00:00.000Z"},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "let me grep"},
        {"type": "text", "text": "Fusion over three legs."},
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}},
    ]}, "timestamp": "2026-08-01T10:00:05.000Z"},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": "42 matches"},
    ]}, "timestamp": "2026-08-01T10:00:06.000Z"},
    {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<system hint>"},
     "timestamp": "2026-08-01T10:00:07.000Z"},
)


class TestClaudeCodeJsonl:
    def test_keeps_conversation_turns_and_drops_tool_noise(self):
        from silica.sources.transcript import parse

        assert parse(_envelope(CLAUDE_CODE)) == [
            ("user", "how does recall work?", "2026-08-01T10:00:00.000Z"),
            ("assistant", "Fusion over three legs.", "2026-08-01T10:00:05.000Z"),
        ]


def _node(nid, role, text, parent, children=(), create_time=0.0):
    return {nid: {
        "id": nid,
        "parent": parent,
        "children": list(children),
        "message": None if role is None else {
            "author": {"role": role},
            "create_time": create_time,
            "content": {"content_type": "text", "parts": [text]},
        },
    }}


CHATGPT = {
    "title": "Embeddings", "conversation_id": "conv-1", "current_node": "c",
    "mapping": {
        **_node("root", None, "", None, ["a"]),
        **_node("a", "user", "what is cosine?", "root", ["b"], 1000.0),
        **_node("b", "assistant", "a normalized dot product", "a", ["c"], 1001.0),
        **_node("c", "user", "and euclidean?", "b", [], 1002.0),
        # an abandoned regeneration branch: not on the current thread
        **_node("z", "assistant", "WRONG BRANCH", "a", [], 1003.0),
    },
}


class TestChatgptMapping:
    def test_follows_the_current_thread_and_ignores_abandoned_branches(self):
        from silica.sources.transcript import parse

        turns = parse(_envelope(json.dumps(CHATGPT), format="chatgpt-mapping",
                                source="chatgpt"))

        assert [(r, t) for r, t, _ in turns] == [
            ("user", "what is cosine?"),
            ("assistant", "a normalized dot product"),
            ("user", "and euclidean?"),
        ]

    def test_broken_parent_chain_falls_back_to_chronological_order(self):
        from silica.sources.transcript import parse

        broken = {**CHATGPT, "current_node": "does-not-exist"}
        turns = parse(_envelope(json.dumps(broken), format="chatgpt-mapping",
                                source="chatgpt"))

        assert [t for _, t, _ in turns] == [
            "what is cosine?", "a normalized dot product", "and euclidean?",
            "WRONG BRANCH",
        ]


CLAUDE_AI = {
    "uuid": "conv-2", "name": "Retrieval",
    "chat_messages": [
        {"sender": "human", "text": "does rerank help?",
         "created_at": "2026-07-01T09:00:00Z"},
        {"sender": "assistant", "text": "on the tail, yes",
         "created_at": "2026-07-01T09:00:04Z"},
    ],
}


class TestClaudeAiMessages:
    def test_maps_sender_to_role(self):
        from silica.sources.transcript import parse

        turns = parse(_envelope(json.dumps(CLAUDE_AI), format="claude-ai-messages",
                                source="claude-ai"))

        assert turns == [
            ("user", "does rerank help?", "2026-07-01T09:00:00Z"),
            ("assistant", "on the tail, yes", "2026-07-01T09:00:04Z"),
        ]


class TestSilicaSession:
    def test_reads_back_what_the_own_session_producer_wrote(self):
        """Own sessions arrive pre-filtered: the producer dropped tool noise."""
        from silica.sources.transcript import parse

        payload = json.dumps([
            {"role": "user", "content": "why episodic and not notes?", "ts": ""},
            {"role": "assistant", "content": "Because of the echo channel.", "ts": ""},
        ])

        assert parse(_envelope(payload, source="silica",
                               format="silica-session")) == [
            ("user", "why episodic and not notes?", ""),
            ("assistant", "Because of the echo channel.", ""),
        ]


class TestRender:
    def test_speaker_tagged_prose_under_a_provenance_header(self):
        from silica.sources.transcript import render

        out = render(_envelope(CLAUDE_CODE, title="Recall design"))

        assert "claude-code" in out
        assert "Recall design" in out
        assert "/home/u/repo" in out
        assert "2026-08-01T10:00:00.000Z" in out
        assert "2026-08-01T10:00:05.000Z" in out
        assert "**user**: how does recall work?" in out
        assert "**assistant**: Fusion over three legs." in out
        assert "tool_use" not in out

    def test_empty_conversation_renders_nothing(self):
        from silica.sources.transcript import render

        assert render(_envelope(_jsonl({"type": "mode"}))) == ""
