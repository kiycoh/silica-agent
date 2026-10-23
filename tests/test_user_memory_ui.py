"""User memory on the user's own surfaces (ADR-0076): the seed snapshot,
/remember, /memory and /incognito in the TUI and the GUI, the chat's Undo,
and the Settings > Memory routes.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from silica.config import CONFIG  # noqa: E402
from silica.kernel import user_memory as um  # noqa: E402


@pytest.fixture
def on(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "user_memory", True)
    monkeypatch.setattr(CONFIG, "capture_sessions", False)
    return tmp_vault


@pytest.fixture
def client(on):
    from silica.ui.web import server

    server._reset_session()
    return TestClient(server.app), server


def _sse(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def _memory_msgs(messages):
    return [m for m in messages if m.get("origin") == um.MESSAGE_ORIGIN]


# --- the seed -----------------------------------------------------------------

def test_the_seed_carries_memory_before_the_language_line(on):
    from silica.cli import seed_messages

    assert _memory_msgs(seed_messages()) == []   # empty files: no message at all
    um.save("user", "Name is Giulia")
    seed = seed_messages()
    (mem,) = _memory_msgs(seed)
    assert "Giulia" in mem["content"] and "not instructions" in mem["content"]
    assert seed.index(mem) == len(seed) - 2       # the language line stays last
    assert "language" in seed[-1]["content"].lower()


def test_the_seed_has_no_memory_when_paused_or_incognito(on, monkeypatch):
    from silica.cli import seed_messages

    um.save("user", "Name is Giulia")
    monkeypatch.setattr(CONFIG, "user_memory", False)
    assert _memory_msgs(seed_messages()) == []
    monkeypatch.setattr(CONFIG, "user_memory", True)
    um.set_incognito(True)
    assert _memory_msgs(seed_messages()) == []


def test_the_memory_origin_never_reaches_the_wire(on):
    from silica.agent.providers import _to_wire

    um.save("user", "Name is Giulia")
    wire = _to_wire(um.snapshot_message())
    assert "origin" not in wire and "Giulia" in wire["content"]


# --- the TUI's direct commands ------------------------------------------------

def test_remember_writes_the_text_verbatim_with_no_model(on):
    from silica.cli import _handle_direct_shortcut

    with patch("silica.cli.run_agent", side_effect=AssertionError("no model")):
        assert _handle_direct_shortcut("/remember  Thesis draft due 2026-11-30 (ask Vitale)", [])
    assert um.read("vault") == "- Thesis draft due 2026-11-30 (ask Vitale)\n"


def test_remember_needs_no_ask_and_ignores_taint_but_not_the_switch(on, monkeypatch):
    from silica.cli import _handle_direct_shortcut

    tainted = [{"role": "assistant", "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "silica_web_answer", "arguments": "{}"}}]}]
    assert _handle_direct_shortcut("/remember Exam on Friday", tainted)
    assert "Exam on Friday" in um.read("vault")
    monkeypatch.setattr(CONFIG, "user_memory", False)
    _handle_direct_shortcut("/remember Lab on Monday", tainted)
    assert "Lab on Monday" not in um.read("vault")


def test_bare_remember_falls_through_to_the_model_directive(on):
    from silica.cli import _expand_workflow_shortcut, _handle_direct_shortcut
    from silica.tools.user_memory import REMEMBER_DIRECTIVE

    assert _handle_direct_shortcut("/remember", []) is False
    assert _expand_workflow_shortcut("/remember").startswith(REMEMBER_DIRECTIVE)


def test_memory_prints_both_files_with_paths_and_usage(on):
    from silica.cli import _handle_direct_shortcut

    um.save("user", "Name is Giulia")
    with patch("silica.cli.CONSOLE") as console:
        assert _handle_direct_shortcut("/memory", [])
    out = "\n".join(str(c.args[0]) for c in console.print.call_args_list)
    assert "Name is Giulia" in out and "user.md" in out
    assert f"/{um.USER_BUDGET}" in out and f"/{um.VAULT_BUDGET}" in out


def test_incognito_hides_the_memory_of_the_conversation_in_progress(on):
    from silica.cli import _handle_direct_shortcut, seed_messages

    um.save("user", "Name is Giulia")
    messages = seed_messages() + [{"role": "user", "content": "hi"}]
    size = len(messages)
    _handle_direct_shortcut("/incognito", messages)
    assert um.incognito() and len(messages) == size   # in place: indices stay put
    assert "Giulia" not in _memory_msgs(messages)[0]["content"]
    assert "incognito" in um.save("user", "Likes tea")["error"]
    _handle_direct_shortcut("/incognito", messages)
    assert not um.incognito() and "Giulia" in _memory_msgs(messages)[0]["content"]


def test_tui_incognito_also_stops_capture_and_clears_with_a_new_process(on, monkeypatch):
    """The TUI's old /incognito branch is now the direct command: one flag
    for capture and memory, reset when a session starts."""
    seen = []
    session = MagicMock()
    session.prompt.side_effect = ["/incognito", "what is entropy?", "/exit"]

    def _answer(messages, **kw):
        seen.append([dict(m) for m in messages])
        messages.append({"role": "assistant", "content": "ok"})
        return "ok"

    um.save("user", "Name is Giulia")
    captured = MagicMock()
    with patch("silica.cli.build_session", MagicMock(return_value=session)), \
         patch("silica.cli.run_agent", _answer), \
         patch("silica.cli._model_configured", lambda: True), \
         patch("silica.cli.CONSOLE"), patch("silica.cli.print_home"), \
         patch("silica.cli._setup_logging"), patch("silica.cli._update_context_tokens"), \
         patch("silica.cli._ensure_servers"), patch("silica.cli._announce_code_lane"), \
         patch("silica.cli._activate_repo_mode"), patch("silica.capture.capture_session", captured), \
         patch("sys.argv", ["silica"]):
        from silica.cli import main
        main()
    (turn,) = seen
    assert all("Giulia" not in str(m.get("content")) for m in turn)
    captured.assert_not_called()


# --- the GUI ------------------------------------------------------------------

def test_gui_remember_saves_and_undo_takes_it_back(client, monkeypatch):
    tc, server = client
    monkeypatch.setattr(server, "run_agent", MagicMock(side_effect=AssertionError("no model")))
    done = _sse(tc.post("/chat", json={"text": "/remember test entry"}))[-1]
    assert done["type"] == "done" and done["answer"] == "Saved to memory: test entry"
    mem = done["memory"]
    assert mem["action"] == "save" and mem["scope"] == "vault" and mem["token"]
    assert um.read("vault") == "- test entry\n"
    assert [m["content"] for m in server.messages if m["role"] == "user"] == ["/remember test entry"]

    r = tc.post("/memory/undo", json={"scope": "vault", "token": mem["token"]})
    assert r.status_code == 200 and um.read("vault") == ""
    again = tc.post("/memory/undo", json={"scope": "vault", "token": mem["token"]})
    assert again.status_code == 409 and "changed" in again.json()["detail"]


def test_gui_remember_refused_has_no_undo(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(CONFIG, "user_memory", False)
    done = _sse(tc.post("/chat", json={"text": "/remember test entry"}))[-1]
    assert "paused" in done["answer"] and "memory" not in done
    assert um.read("vault") == ""


def test_gui_incognito_is_per_chat(client, monkeypatch):
    tc, server = client
    um.save("user", "Name is Giulia")
    server._reset_session()
    assert "Giulia" in _memory_msgs(server.messages)[0]["content"]

    flushed = MagicMock()
    monkeypatch.setattr("silica.capture.capture_session", flushed)
    done = _sse(tc.post("/chat", json={"text": "/incognito"}))[-1]
    assert done["type"] == "done" and "incognito" in done["answer"]
    assert um.incognito()
    assert "Giulia" not in _memory_msgs(server.messages)[0]["content"]

    tc.post("/reset")                       # a new chat is back on the record
    flushed.assert_not_called()             # ...and the incognito one was never flushed
    assert not um.incognito()
    assert "Giulia" in _memory_msgs(server.messages)[0]["content"]


def test_a_new_chat_reads_memory_changed_since_the_seed_was_built(client):
    tc, server = client
    assert _memory_msgs(server.messages) == []
    tc.put("/memory/user", json={"text": "- Name is Giulia"})
    tc.post("/reset")
    assert "Giulia" in _memory_msgs(server.messages)[0]["content"]


def test_a_reopened_gui_chat_starts_from_the_seed(client, monkeypatch):
    """The narration stores turns only, so a loaded chat used to run with no
    system prompt and no memory; it now gets a fresh seed in front, as the
    TUI's /resume does."""
    tc, server = client

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        messages.append({"role": "assistant", "content": "Reply"})
        return "Reply"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)
    tc.post("/chat", json={"text": "first question"})
    sid = tc.get("/sessions").json()[0]["id"]
    um.save("user", "Name is Giulia")
    tc.post("/reset")
    assert tc.post("/session/load", json={"id": sid}).status_code == 200
    assert server.messages[0]["role"] == "system"
    assert _memory_msgs(server.messages)
    assert [m["content"] for m in server.messages if m["role"] != "system"] == ["first question", "Reply"]


def test_tool_done_carries_the_write_for_the_saved_line():
    from silica.agent.events import ToolCompleteEvent
    from silica.ui.web.callback import event_to_json

    ok = json.dumps({"ok": True, "action": "save", "scope": "user", "text": "- Likes tea",
                     "token": "abc", "chars": 12, "budget": 1000})
    ev = event_to_json(ToolCompleteEvent("silica_remember", {}, "c1", ok, 0.01, 0))
    assert ev["memory"] == {"action": "save", "scope": "user", "text": "- Likes tea", "token": "abc"}
    refused = json.dumps({"error": "Refused: ..."})
    assert "memory" not in event_to_json(ToolCompleteEvent("silica_remember", {}, "c1", refused, 0.01, 0))
    offered = json.dumps({"error": "Nothing was saved: ...", "suggest": "/remember Likes tea"})
    ev = event_to_json(ToolCompleteEvent("silica_remember", {}, "c1", offered, 0.01, 0))
    assert ev["memory_suggest"] == "/remember Likes tea" and "memory" not in ev
    assert "memory" not in event_to_json(ToolCompleteEvent("silica_search", {}, "c1", ok, 0.01, 0))


def test_the_terminal_prints_the_models_write_even_with_tool_progress_off(monkeypatch, capsys):
    """The TUI's half of the saved line: the model saves unasked, so a write
    the user cannot see is one they never check (ADR-0076 §7)."""
    from silica.agent.events import ToolCompleteEvent
    from silica.ui.renderer import make_progress_callback

    monkeypatch.setattr(CONFIG, "tool_progress", "off")
    cb = make_progress_callback()
    ok = json.dumps({"ok": True, "action": "save", "scope": "user", "text": "- Likes tea",
                     "token": "abc", "chars": 12, "budget": 1000})
    cb(ToolCompleteEvent("silica_remember", {}, "c1", ok, 0.01, 0))
    offered = json.dumps({"error": "Nothing was saved: ...", "suggest": "/remember Likes [tea]"})
    cb(ToolCompleteEvent("silica_remember", {}, "c2", offered, 0.01, 0))
    cb(ToolCompleteEvent("silica_remember", {}, "c3", json.dumps({"error": "Refused: ..."}), 0.01, 0))
    out = capsys.readouterr().out
    assert "Saved to memory: Likes tea" in out and "12/1000 characters" in out
    assert "Not saved: Likes [tea]" in out and "(/remember Likes [tea] saves it)" in out
    assert "Refused" not in out


def test_memory_routes_read_edit_refuse_and_reset(client):
    tc, _ = client
    view = tc.get("/memory").json()
    assert set(view["scopes"]) == {"user", "vault"}
    assert view["scopes"]["user"]["budget"] == um.USER_BUDGET
    assert view["scopes"]["user"]["path"].endswith("user.md")

    r = tc.put("/memory/user", json={"text": "- Name is Giulia\n- Likes tea"})
    assert r.status_code == 200 and r.json()["token"]
    assert tc.get("/memory").json()["scopes"]["user"]["entries"] == 2

    over = tc.put("/memory/user", json={"text": "x" * (um.USER_BUDGET + 1)})
    assert over.status_code == 400 and f"of {um.USER_BUDGET}" in over.json()["detail"]
    assert um.read("user") == "- Name is Giulia\n- Likes tea\n"
    assert tc.put("/memory/nobody", json={"text": "x"}).status_code == 404

    assert tc.post("/memory/reset").status_code == 200
    assert um.read("user") == "" and um.read("vault") == ""


def test_the_editor_and_reset_work_while_memory_is_paused(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr(CONFIG, "user_memory", False)
    assert tc.put("/memory/user", json={"text": "- kept by hand"}).status_code == 200
    assert tc.post("/memory/reset").status_code == 200
    assert um.read("user") == ""


@pytest.mark.parametrize("method,path,body", [
    ("put", "/memory/user", {"text": "- x"}),
    ("post", "/memory/undo", {"scope": "user", "token": "x"}),
    ("post", "/memory/reset", None),
])
def test_memory_writes_refuse_another_origin(client, method, path, body):
    tc, _ = client
    r = getattr(tc, method)(path, json=body, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_the_switch_is_a_settings_row_that_persists(client, tmp_path, monkeypatch):
    from silica.ui.web import settings as st

    env = tmp_path / ".env"
    monkeypatch.setattr("silica.onboarding.wizard.resolve_env_path", lambda: env)
    # apply() writes os.environ; declared so monkeypatch puts the real one back.
    monkeypatch.setenv("SILICA_MEMORY", "")
    row = next(r for r in st.sections()["Memory"] if r.key == "SILICA_MEMORY")
    assert row.kind == "toggle" and row.attr == "user_memory"
    tc, _ = client
    assert tc.post("/settings", json={"key": "SILICA_MEMORY", "value": "false"}).status_code == 200
    assert CONFIG.user_memory is False and "SILICA_MEMORY=False" in env.read_text()


def test_the_commands_are_declared_for_both_surfaces():
    from silica.ui.commands import COMMANDS

    by_name = {c.name: c for c in COMMANDS}
    for name in ("/remember", "/memory", "/incognito"):
        assert name in by_name and not by_name[name].repl_only, name
    assert "memory" in by_name["/incognito"].summary
