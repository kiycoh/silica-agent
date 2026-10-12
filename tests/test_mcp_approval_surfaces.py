"""Where the approval card is drawn and answered (ADR-0091 point 6, "GUI and
TUI"): the GUI gets an `approval` frame over the SSE bridge; the TUI renderer
stops its Live, asks `run? [y]es / [N]o` (default no) and resumes."""
from __future__ import annotations

import pytest

from silica.agent.events import ApprovalRequestEvent
from tests.webassets import WEB


def _event(**kw):
    base = dict(id="c1", server="gmail", tool="send_message",
                args={"to": "ann@example.org", "body": "hi"}, timeout_s=120.0)
    return ApprovalRequestEvent(**{**base, **kw})


def test_the_gui_frame_carries_server_tool_full_arguments_and_the_wait():
    from silica.ui.web.callback import event_to_json

    assert event_to_json(_event()) == {
        "type": "approval", "id": "c1", "server": "gmail", "tool": "send_message",
        "args": {"to": "ann@example.org", "body": "hi"}, "timeout_s": 120.0,
    }


def test_the_page_loads_the_card_and_the_chat_draws_it():
    index = (WEB / "index.html").read_text(encoding="utf-8")
    assert '<script src="/static/app-connectors.js"></script>' in index
    js = (WEB / "app-connectors.js").read_text(encoding="utf-8")
    assert "function approvalCard(" in js and 'connAct("mcp", "approve"' in js
    assert "not run: no answer" in js
    chat = (WEB / "app-chat.js").read_text(encoding="utf-8")
    assert 'ev.type === "approval"' in chat and "approvalCard(ev)" in chat


def test_no_button_of_the_card_takes_the_initial_focus():
    """A stray Enter must not approve: the card never calls focus()."""
    js = (WEB / "app-connectors.js").read_text(encoding="utf-8")
    card = js[js.index("function approvalCard("):]
    card = card[: card.index("\n}\n")]
    assert ".focus(" not in card and "autofocus" not in card


@pytest.fixture
def renderer(monkeypatch):
    from silica.ui import renderer as mod

    r = mod.make_progress_callback()
    stops = []
    monkeypatch.setattr(r, "_stop_spinner", lambda: stops.append(1))
    r.stops = stops
    return r


@pytest.mark.parametrize("typed,approved", [("y", True), ("yes", True), ("", False), ("n", False),
                                             ("whatever", False)])
def test_the_tui_asks_in_place_and_defaults_to_no(renderer, monkeypatch, typed, approved):
    from silica.ui import renderer as mod

    asked = []
    monkeypatch.setattr(mod, "_ask_yes_no", lambda msg, timeout: asked.append((msg, timeout)) or typed)
    ev = _event()
    renderer(ev)
    assert renderer.stops, "the Live stops before the question"
    assert asked == [("  run? [y]es / [N]o ", 120.0)]
    assert ev.decided.is_set() and ev.approved is approved


def test_the_tui_reads_no_answer_when_the_prompt_times_out(renderer, monkeypatch):
    from silica.ui import renderer as mod

    monkeypatch.setattr(mod, "_ask_yes_no", lambda msg, timeout: None)
    ev = _event()
    renderer(ev)
    assert ev.decided.is_set() and ev.approved is None


def test_without_a_terminal_there_is_no_one_to_ask(monkeypatch):
    from silica.ui import renderer as mod

    monkeypatch.setattr(mod.sys.stdin, "isatty", lambda: False)
    assert mod._ask_yes_no("run? ", 1.0) is None


def test_a_connector_row_is_named_by_its_server_and_its_request():
    from silica.ui.renderer import _tool_target, _tool_verb

    assert _tool_verb("mcp__gmail") == "gmail"
    assert _tool_target("mcp__gmail", {"request": "find the thesis thread"}) == "find the thesis thread"


def test_a_call_that_did_not_run_reads_as_itself_not_as_a_failure():
    """The row of a declined connector call says so; the generic "something
    went wrong" would read as a fault."""
    from silica.ui.web.failures import sentence

    for raw in ("not run: declined", "not run: no answer", "not run: out of time"):
        assert sentence(raw) == raw
