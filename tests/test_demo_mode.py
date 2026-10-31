"""ADR-0139 point 6 and its amendment of 2026-10-31: SILICA_DEMO=1 turns a
hosted process into one anonymous visit's sandbox. What it refuses answers as a
route that does not exist or as a 404 before the turn, so the door counts no
question for it; the chat gets the core minus a named set at 8 calls a question;
the page loses the controls of what it refuses."""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from silica.agent.constraints import (  # noqa: E402
    CHAT_CORE,
    DEMO_MAX_ITERATIONS,
    DEMO_WITHHELD,
    _eligible,
    chat_deferred,
    chat_tools,
    web_answer_constraints,
)
from silica.agent.events import ToolCompleteEvent  # noqa: E402
from silica.agent.recall_watch import THIN_COVERAGE_HINT  # noqa: E402
from silica.config import DEMO_OFF, demo, served_views, withheld  # noqa: E402
from tests.webassets import WEB  # noqa: E402

# The chat's tools in a demo, named here so a tool joining the core later is
# placed in or out of the demo on purpose: a new core tool could reach the web.
DEMO_TOOLS = (
    "silica_recall", "silica_search", "silica_search_context", "silica_read_note", "silica_read",
    "silica_outline", "silica_files", "silica_props", "silica_links", "silica_backlinks",
    "silica_write_note", "silica_patch_note", "silica_move", "silica_flag_note",
    "silica_vault_report", "silica_ledger_next", "silica_ledger_update", "silica_remember",
)


@pytest.fixture
def sandbox(tmp_vault, _fresh_narrator, monkeypatch):
    """A demo unit's process (ADR-0139 point 2): hosted, the demo flag, the
    line SILICA_VIEWS=graph; the chat answers without a model and keeps the
    constraints of every turn it ran."""
    from silica.ui.web import server

    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_DEMO", "1")
    monkeypatch.setenv("SILICA_VIEWS", "graph")
    server._reset_session()
    ran: list = []

    def answers(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        ran.append(kw.get("constraints"))
        messages.append({"role": "assistant", "content": "forty-two"})
        return "forty-two"

    monkeypatch.setattr(server, "run_agent", answers)
    return TestClient(server.app), server, ran


def _frames(resp) -> list[dict]:
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


def _paths(routes):
    # FastAPI keeps an included router whole: its routes sit under it.
    for r in routes:
        sub = getattr(r, "original_router", None)
        yield from (_paths(sub.routes) if sub else [getattr(r, "path", "")])


# --- the flag and the views ---------------------------------------------------

@pytest.mark.parametrize("raw, on", [
    ("1", True), (" 1 ", True), ("true", False), ("t", False), ("yes", False), ("", False), (None, False),
])
def test_the_flag_is_one_spelling_read_per_call(monkeypatch, raw, on):
    if raw is None:
        monkeypatch.delenv("SILICA_DEMO", raising=False)
    else:
        monkeypatch.setenv("SILICA_DEMO", raw)
    assert demo() is on


@pytest.mark.parametrize("hosted", [True, False])
@pytest.mark.parametrize("views", [None, "", "graph", "all", "discover,learn,calendar,public"])
def test_a_demo_serves_explore_at_most_whatever_its_unit_lists(monkeypatch, hosted, views):
    monkeypatch.setenv("SILICA_DEMO", "1")
    if hosted:
        monkeypatch.setenv("SILICA_HOSTED", "1")
    else:
        monkeypatch.delenv("SILICA_HOSTED", raising=False)
    if views is None:
        monkeypatch.delenv("SILICA_VIEWS", raising=False)
    else:
        monkeypatch.setenv("SILICA_VIEWS", views)
    assert served_views() <= {"graph"}
    # a demo has no handle to publish under and no Learn, whatever the line says
    assert {"/discover", "/learn", "/calendar", "/public"} <= withheld("routes")
    if views in ("graph", "all"):
        assert served_views() == {"graph"}


def test_a_tenant_and_a_laptop_refuse_none_of_it(monkeypatch):
    monkeypatch.delenv("SILICA_DEMO", raising=False)
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "all")
    for part in ("routes", "commands"):
        assert not withheld(part) & set(DEMO_OFF[part])
    monkeypatch.delenv("SILICA_HOSTED")
    assert not withheld("routes") and not withheld("commands")


def test_every_name_the_demo_refuses_exists():
    """A misspelt route or command would be a refusal that refuses nothing."""
    import silica.cli
    from silica.ui.commands import commands
    from silica.ui.web import server

    paths = set(_paths(server.app.routes))
    for r in DEMO_OFF["routes"]:
        assert any(p == r or p.startswith(r + "/") for p in paths), r
    assert set(DEMO_OFF["commands"]) <= {c.name for c in commands()} | set(silica.cli._SHORTCUTS)


# --- the routes ---------------------------------------------------------------

@pytest.mark.parametrize("path", [
    *DEMO_OFF["routes"],
    # what each prefix covers
    "/settings/confirm", "/vault/save/preview", "/vault/export/added", "/vault/export/summary",
    "/phone/turn", "/phone/new", "/connectors/mcp/add", "/skills/proposed/x/install", "/zotero/status",
])
def test_each_refused_route_answers_as_one_that_does_not_exist(sandbox, path):
    tc, server, ran = sandbox
    for method in ("get", "post"):
        r = getattr(tc, method)(path)
        assert (r.status_code, r.json()) == (404, {"detail": "Not Found"}), (method, path)
    assert not ran and not server._busy


def test_a_refused_route_runs_nothing_behind_it(sandbox, monkeypatch):
    from pathlib import Path

    from silica.config import CONFIG
    from silica.kernel.recall.paths import _SILICA_HOME
    from silica.ui.web import settings as st

    tc, server, ran = sandbox

    def boom(*a, **kw):
        raise AssertionError("a refused route must not reach its handler")

    monkeypatch.setattr(st, "apply", boom)
    assert tc.post("/settings", json={"key": "SILICA_THEME", "value": "dark"}).status_code == 404
    assert tc.post("/flag", json={"session": "abc", "seq": 1, "note": "wrong"}).status_code == 404
    assert not (_SILICA_HOME / "flags.jsonl").exists()
    assert tc.post("/chat/files/add", files={"files": ("upload.md", b"# mine")}).status_code == 404
    assert tc.post("/vault/import", files={"files": ("upload.md", b"# mine")}).status_code == 404
    assert not list(Path(CONFIG.vault_path).rglob("upload.md"))
    # a question the door would not count: again, edit, the phone's turn
    assert tc.post("/turn/redo", json={"text": "and again"}).status_code == 404
    assert tc.post("/phone/turn", json={"text": "hi"}).status_code == 404
    assert not ran and not server._busy


def test_a_route_the_demo_keeps_still_answers(sandbox):
    tc, _, _ = sandbox
    for path in ("/vault_info", "/sessions", "/messages", "/changes", "/vault_version", "/commands"):
        assert tc.get(path).status_code == 200, path
    assert tc.get("/compare").status_code != 404  # Explore's, the demo's one view


# --- POST /chat ---------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "/web what is entropy?", "/WEB what is entropy?", "/fetch https://example.com",
    "/nucleate Inbox", "/explain Entropy", "/flashcards Entropy", "/vault ~/elsewhere", "/quiz Entropy",
])
def test_a_refused_command_is_a_404_before_the_turn_so_the_door_counts_nothing(sandbox, text):
    tc, server, ran = sandbox
    r = tc.post("/chat", json={"text": text})
    assert r.status_code == 404
    assert r.json()["detail"] == f"'{text.split()[0].lower()}' is not part of the demo"
    assert not ran and not server._busy


def test_an_image_or_a_story_is_refused_before_anything_is_saved(sandbox):
    from silica.kernel.recall.paths import attachments_dir

    tc, server, ran = sandbox
    png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 16).decode()
    r = tc.post("/chat", json={"text": "what is this?", "images": [{"name": "a.png", "data": png}]})
    assert (r.status_code, r.json()["detail"]) == (404, "images are not part of the demo")
    assert not list(attachments_dir().glob("*"))
    # a Discover story with the web as its scope would be a /web turn
    r = tc.post("/chat", json={"text": "and this?", "story": "s1", "scope": "web"})
    assert (r.status_code, r.json()["detail"]) == (404, "Discover is not part of the demo")
    assert not ran and not server._busy


def test_the_picker_offers_no_command_the_demo_refuses(sandbox):
    tc, _, _ = sandbox
    names = {c["name"] for c in tc.get("/commands").json()}
    assert not names & set(DEMO_OFF["commands"])
    assert {"/summarize", "/find", "/undo"} <= names


def test_a_question_runs_on_the_demo_set_at_8_calls(sandbox, monkeypatch):
    tc, _, ran = sandbox
    assert _frames(tc.post("/chat", json={"text": "what is entropy?"}))[-1]["type"] == "done"
    (c,) = ran
    assert c.max_iterations == DEMO_MAX_ITERATIONS
    assert c.tools == DEMO_TOOLS and c.deferred == ()
    monkeypatch.delenv("SILICA_DEMO")
    tc.post("/chat", json={"text": "and a tenant's?"})
    assert ran[-1].max_iterations is None  # the loop's own default


def test_the_cap_is_the_one_the_web_door_already_uses():
    """ADR-0139 point 5: 8 calls a question, three questions 24 at most."""
    assert DEMO_MAX_ITERATIONS == web_answer_constraints().max_iterations == 8


def test_no_answer_is_flaggable_in_a_demo(sandbox):
    tc, _, _ = sandbox
    done = _frames(tc.post("/chat", json={"text": "hi"}))[-1]
    assert done["type"] == "done" and "turn" not in done
    assert all("turn" not in m for m in tc.get("/messages").json())


def test_a_thin_answer_does_not_offer_the_web_the_demo_refuses(sandbox, monkeypatch):
    tc, server, _ = sandbox

    def thin(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        tool_progress_callback(ToolCompleteEvent(
            name="silica_recall", args={}, call_id="c1",
            result=json.dumps({"notes": [], "facts": 0}), duration_s=0.0, iteration=1,
        ))
        messages.append({"role": "assistant", "content": "I have nothing on that."})
        return "I have nothing on that."

    monkeypatch.setattr(server, "run_agent", thin)
    done = _frames(tc.post("/chat", json={"text": "what is graph rewiring?"}))[-1]
    assert THIN_COVERAGE_HINT not in [n["text"] for n in done.get("notes") or []]
    monkeypatch.delenv("SILICA_DEMO")
    done = _frames(tc.post("/chat", json={"text": "what is graph rewiring?"}))[-1]
    assert {"kind": "retrieval", "text": THIN_COVERAGE_HINT} in done["notes"]


# --- the tools ------------------------------------------------------------------

def test_the_chat_gets_the_core_minus_a_named_set_and_nothing_deferred(monkeypatch):
    import silica.cli  # noqa: F401  registers every tool
    from silica.tools import TOOLS

    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "graph")
    monkeypatch.setenv("SILICA_DEMO", "1")
    # every withheld name is a core tool, or the exclusion is a silent no-op
    assert DEMO_WITHHELD <= set(CHAT_CORE) <= set(TOOLS)
    assert DEMO_TOOLS == tuple(n for n in CHAT_CORE if n not in DEMO_WITHHELD)
    # a directive naming tools past the core summons none of them here
    summons = [{"role": "user", "origin": "cli",
                "content": "call silica_generate_taxonomy, then silica_run_organizer"}]
    assert chat_tools() == chat_tools(summons) == DEMO_TOOLS
    assert chat_deferred() == chat_deferred(summons) == ()
    assert not DEMO_WITHHELD & set(_eligible())
    # the writes stay: they land in the visitor's copy and go with it (point 6)
    assert {"silica_write_note", "silica_patch_note", "silica_move", "silica_flag_note",
            "silica_remember"} <= set(DEMO_TOOLS)
    monkeypatch.delenv("SILICA_DEMO")
    assert DEMO_WITHHELD <= set(chat_tools()) and chat_deferred()


# --- the page -------------------------------------------------------------------

def test_the_page_of_a_demo_has_no_control_for_what_it_refuses(sandbox, monkeypatch):
    tc, _, _ = sandbox
    html = tc.get("/").text
    assert '<html lang="en" data-hosted data-demo' in html
    assert 'placeholder="Ask about the sample vault"' in html and "Ask about your notes" not in html
    # the field goes back to it after each turn: read off the page, not a copy
    assert "Ask about your notes" not in (WEB / "app-chat.js").read_text(encoding="utf-8")
    assert 'data-tab="graph" data-served' in html
    css = (WEB / "app-shell.css").read_text(encoding="utf-8")
    rule = css[css.index("html[data-demo] :is("):]
    hidden = rule[:rule.index(")")]
    for control in ("#attach", ".mic", "#vault-add", "#vault-switch", "#settings-btn", ".redo-btn", ".edit-btn"):
        assert control in hidden, control
    # each id the rule names is in the page, or the rule hides nothing
    for name in ("attach", "vault-add", "vault-switch", "settings-btn"):
        assert f'id="{name}"' in html, name
    assert 'class="mic"' in html


def test_the_page_without_the_demo_is_unchanged(sandbox, monkeypatch):
    tc, _, _ = sandbox
    monkeypatch.delenv("SILICA_DEMO")
    html = tc.get("/").text
    assert "data-demo" not in html and 'placeholder="Ask about your notes"' in html
    assert '<html lang="en" data-hosted' in html


# --- the help, and the chat's picture of the app it reads (ADR-0115) ------------

# The help rows about a control the demo refuses, by the route each is keyed on
REFUSED_ROWS = {"ctrl ,": "/settings", "drop a file": "/chat/files/add", "download": "/vault/export",
                "the mic": "/stt"}


def _terms(sections) -> list[str]:
    return [term for _, rows in sections for term, _ in rows]


def test_a_help_row_goes_with_the_route_it_names(sandbox, monkeypatch):
    tc, _, _ = sandbox
    html = tc.get("/").text
    # each key is a route the demo refuses as written, or the row hides nowhere
    assert set(re.findall(r'data-route="([^"]+)"', html)) == set(REFUSED_ROWS.values()) <= set(DEMO_OFF["routes"])
    for route in REFUSED_ROWS.values():
        assert f'data-route="{route}" data-served' not in html, route
    monkeypatch.delenv("SILICA_DEMO")
    html = tc.get("/").text
    for route in REFUSED_ROWS.values():
        assert f'data-route="{route}" data-served' in html, route


def test_the_chat_hears_of_no_control_the_demo_refuses(monkeypatch):
    """The panel's rows are the chat's picture of the app, so a row the demo
    drops is one the chat never describes, and the settings block goes with
    the row that opens it."""
    from silica.ui.web.app_help import app_message, panel

    monkeypatch.delenv("SILICA_HOSTED", raising=False)
    monkeypatch.delenv("SILICA_DEMO", raising=False)
    laptop, laptop_told = _terms(panel(hosted=False)), app_message()
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "graph")
    tenant, told = _terms(panel(hosted=True)), app_message()
    monkeypatch.setenv("SILICA_DEMO", "1")
    shown, demo_told = _terms(panel(hosted=True)), app_message()
    # a laptop has no downloads (ADR-0142); a tenant keeps all four
    assert set(REFUSED_ROWS) - {"download"} <= set(laptop) and set(REFUSED_ROWS) <= set(tenant)
    assert "### settings (ctrl ,)" in laptop_told and "### settings (ctrl ,)" in told
    assert shown == [t for t in tenant if t not in REFUSED_ROWS]
    for term in REFUSED_ROWS:
        assert f"\n- {term}: " in told and f"\n- {term}: " not in demo_told, term
    rows = demo_told[:demo_told.index("These rows")]
    assert "settings" not in rows.lower() and "download" not in rows.lower()


# --- the visit's count and cards (renderDemo), from the door's headers ----------

node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _view(tmp_path, expr: str):
    src = (WEB / "app-chat.js").read_text(encoding="utf-8")
    pure = src[src.index("// demo-pure:start"):src.index("// demo-pure:end")]
    script = tmp_path / "demo.js"
    script.write_text(pure + f"\nconsole.log(JSON.stringify({expr}));\n", encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@node
def test_without_the_door_s_header_nothing_is_drawn(tmp_path):
    assert _view(tmp_path, "[demoView(null, '', 0), demoView(undefined, '', 0)]") == [None, None]


@node
def test_the_count_line_counts_down_to_the_last_question(tmp_path):
    views = _view(tmp_path, "[3, 2, 1].map((n) => demoView(n, '', 0))")
    assert [v["count"] for v in views] == ["3 questions left", "2 questions left", "Last question"]
    assert {v["tail"] for v in views} == {"cleared within 30 min"}
    assert not any("card" in v for v in views)


@node
def test_each_state_the_door_sends_is_its_card(tmp_path):
    v = _view(tmp_path, "({zero: demoView(0, '', 0), spent: demoView(0, 'spent', 0), "
                        "closed: demoView(2, 'closed', Date.UTC(2026, 9, 31, 18, 48)), "
                        "ended: demoView(1, 'ended', 0), late: demoUntilMidnight(Date.UTC(2026, 9, 31, 23, 59, 30))})")
    assert v["zero"] == v["spent"] == {
        "card": "end", "line": "That was your third question.", "more": "Run Silica on your own notes:",
        "install": True, "under": "This chat stays here until the sample vault is cleared, within 30 min."}
    assert v["closed"] == {
        "card": "closed", "line": "Today's demo budget is spent.", "install": True, "under": "",
        "more": "It opens again at midnight UTC, in 5 h 12 min."}
    assert v["ended"] == {
        "card": "ended", "line": "This sample vault has been cleared", "more": "", "install": False,
        "under": "A sample vault lasts 30 minutes, or 15 without a question."}
    assert v["late"] == "1 min"


@node
def test_the_card_teaches_the_install_the_landing_shows(tmp_path):
    site = (WEB.parents[3] / "site" / "index.html").read_text(encoding="utf-8")
    block = site[site.index('<pre id="c-local">'):]
    shown = [re.sub(r"<[^>]+>", "", line).removeprefix("$ ") for line in block[:block.index("</pre>")].splitlines()]
    assert _view(tmp_path, "DEMO_INSTALL") == shown


def test_the_page_reads_the_door_where_it_already_asks():
    """The headers ride the chat's own responses, and the vault's version poll
    reads them at load and every 15 s, which is how an idle tab learns its
    sandbox ended; the age line's links are the door's legal pages."""
    chat = (WEB / "app-chat.js").read_text(encoding="utf-8")
    turn = chat[chat.index("async function runTurn("):]
    assert turn.index("readDemoHeaders(resp)") < turn.index("if (!resp.ok)")
    poll = (WEB / "app-calendar.js").read_text(encoding="utf-8")
    poll = poll[poll.index("async function pollVaultVersion()"):]
    assert "readDemoHeaders(r)" in poll[:poll.index("\n}\n")]
    assert '"/terms"' in chat and '"/privacy"' in chat
