"""ADR-0140: a hosted account is served the views its unit lists, and a view it
does not get is gone at every door; a local install serves every view."""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from silica.config import VIEWS, served_views, withheld
from tests.webassets import WEB as STATIC_SRC

# Page (ADR-0143, ADR-0151) is a view added after ADR-0140, so paid by default
PAID = {"discover", "learn", "calendar", "public"}


@pytest.fixture
def free(tmp_vault, monkeypatch):
    """A Free tenant: hosted, with the line bin/tenant writes for the tier."""
    from silica.ui.web import server

    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "graph")
    server._reset_session()
    return TestClient(server.app), server


def test_a_laptop_serves_every_view_whatever_its_env_says(monkeypatch):
    monkeypatch.delenv("SILICA_HOSTED", raising=False)
    monkeypatch.setenv("SILICA_VIEWS", "graph")
    assert served_views() == set(VIEWS)
    assert not withheld("routes") and not withheld("tools") and not withheld("commands")


@pytest.mark.parametrize("raw, served", [
    (None, set()),
    ("", set()),
    ("graph", {"graph"}),
    (" graph , learn ", {"graph", "learn"}),
    ("all", {"graph"} | PAID),
    ("metrics,graph,bogus", {"graph"}),
])
def test_a_hosted_account_gets_what_its_unit_lists_and_never_examine(monkeypatch, raw, served):
    monkeypatch.setenv("SILICA_HOSTED", "1")
    if raw is None:
        monkeypatch.delenv("SILICA_VIEWS", raising=False)
    else:
        monkeypatch.setenv("SILICA_VIEWS", raw)
    assert served_views() == served


def test_a_free_account_gets_404_on_every_paid_route_and_the_rest_answers(free):
    tc, _ = free
    for method, path in [("get", "/discover/status"), ("get", "/learn"), ("get", "/learn/plans"),
                         ("get", "/calendar"), ("post", "/reminders"), ("get", "/metrics")]:
        assert getattr(tc, method)(path).status_code == 404, path
    assert tc.get("/commands").status_code == 200
    assert tc.get("/compare").status_code != 404


def test_a_local_install_reaches_the_same_routes(tmp_vault, monkeypatch):
    from silica.ui.web import server

    monkeypatch.delenv("SILICA_HOSTED", raising=False)
    monkeypatch.setenv("SILICA_VIEWS", "graph")
    assert TestClient(server.app).post("/reminders").status_code == 200


def test_a_free_account_has_no_paid_command_in_the_picker_and_a_typed_one_is_refused(free, monkeypatch):
    tc, server = free
    names = {c["name"] for c in tc.get("/commands").json()}
    assert not names & {"/quiz", "/learn", "/agenda"}
    assert "/fetch" in names

    def boom(*a, **kw):
        raise AssertionError("a withheld command must not reach the agent")

    monkeypatch.setattr(server, "run_agent", boom)
    resp = tc.post("/chat", json={"text": "/quiz Concepts --n=3"})
    events = [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == {"type": "error", "error": "'/quiz' is not part of this account"}


def test_a_free_chat_neither_offers_nor_permits_a_paid_views_tool(monkeypatch):
    import silica.cli  # noqa: F401  registers every tool
    from silica.agent.constraints import _eligible, chat_deferred, chat_tools

    paid = {t for v in PAID for t in VIEWS[v].get("tools", ())}
    monkeypatch.delenv("SILICA_HOSTED", raising=False)
    assert paid <= set(_eligible())
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "graph")
    assert not paid & set(_eligible())
    assert not paid & {*chat_tools(), *chat_deferred()}


def test_the_page_marks_only_the_served_views_and_the_css_hides_the_rest(free, monkeypatch):
    tc, _ = free
    html = tc.get("/").text
    assert 'data-tab="graph" data-served' in html
    for view in PAID | {"metrics"}:
        assert f'data-tab="{view}"' in html and f'data-tab="{view}" data-served' not in html
    assert 'data-view="learn" data-served' not in html
    monkeypatch.delenv("SILICA_HOSTED", raising=False)
    assert "data-served" not in tc.get("/").text
    css = (STATIC_SRC / "app-shell.css").read_text(encoding="utf-8")
    assert "html[data-hosted] :is([data-tab], [data-view], [data-route]):not([data-served])" in css


def test_the_chat_hears_of_no_view_the_account_does_not_get(monkeypatch):
    from silica.ui.web.app_help import panel

    monkeypatch.setenv("SILICA_VIEWS", "graph")
    free = [t for t, _ in dict(panel(hosted=True))["the views"]]
    assert free == ["a chat", "Explore", "a new note"]
    local = [t for t, _ in dict(panel(hosted=False))["the views"]]
    assert {"Memorize", "Calendar", "Examine"} <= set(local)


# Point 5: a route, tool or command added later is placed on purpose, so a new
# view's cannot ship open to Free by omission. These are the chat's, shared by
# every account; a new one goes here or into its view's row in config.VIEWS.
SHARED_ROUTES = {
    "/link",
    "/", "/asset", "/attachment/{name}", "/bug_report", "/changes", "/changes/diff", "/changes/keep",
    "/chat", "/chat/files/add", "/commands", "/concept", "/concepts", "/config", "/connectors",
    "/connectors/{name}/{action}", "/context", "/docs", "/docs/oauth2-redirect", "/endpoints",
    "/endpoints/start", "/find", "/flag", "/health", "/instructions", "/jobs", "/jobs/running",
    "/jobs/{job_id}/dismiss", "/jobs/{job_id}/retry", "/jobs/{job_id}/turn", "/limits", "/mail/upload",
    "/memory", "/memory/reset", "/memory/undo", "/memory/{scope}", "/messages", "/narration",
    "/narration/sse", "/note", "/note/asset", "/note/check", "/note/check/keep", "/note/history",
    "/note/history/diff", "/note/history/restore", "/note/let-go", "/note/new", "/note/raw",
    "/note/render", "/note/save", "/nucleate",
    "/openapi.json", "/path", "/phone/new", "/phone/turn", "/raw", "/redoc", "/reset", "/session/delete",
    "/session/load", "/session/rename", "/sessions", "/settings", "/settings/confirm", "/skills",
    "/skills/proposed/{name}/dismiss", "/skills/proposed/{name}/install", "/skills/{name}", "/static",
    "/stop", "/stt", "/supported_types", "/trash", "/trash/erase", "/trash/restore", "/tree/delete",
    "/tree/move", "/tree/undo", "/turn/redo", "/vault/export", "/vault/export/added",
    "/vault/export/summary", "/vault/fetch", "/vault/import", "/vault/invitations",
    "/vault/invitations/decline", "/vault/invitations/preview", "/vault/invitations/save",
    "/vault/save", "/vault/save/preview",
    "/vault_brief",
    "/vault_info", "/vault_version", "/workspaces", "/workspaces/line", "/workspaces/membership",
    "/zotero/collection", "/zotero/covers", "/zotero/file", "/zotero/grant", "/zotero/groups",
    "/zotero/item", "/zotero/labels", "/zotero/library", "/zotero/link", "/zotero/peek",
    "/zotero/setting", "/zotero/status", "/zotero/unlink",
}
SHARED_TOOLS = {
    "find_in_page", "plan", "remember", "silica_aliases", "silica_anneal", "silica_autolink",
    "silica_backlink", "silica_backlinks", "silica_changes",
    "silica_concepts", "silica_cooccurrence_refresh", "silica_curate", "silica_dedup",
    "silica_dedup_pairs", "silica_deferred_flush", "silica_deferred_list", "silica_deferred_retry",
    "silica_delegate", "silica_delete", "silica_doctor", "silica_document", "silica_embed_refresh",
    "silica_enrich_batch", "silica_exists", "silica_file_links", "silica_files", "silica_flag_note", "silica_require",
    "silica_generate_taxonomy", "silica_graph_explain", "silica_graph_export", "silica_graph_path",
    "silica_health", "silica_impact", "silica_inbox_ls", "silica_ledger_digest", "silica_ledger_next",
    "silica_ledger_update", "silica_lexical_refresh", "silica_links", "silica_load_tools",
    "silica_look", "silica_mindmap", "silica_move", "silica_orphans", "silica_outline",
    "silica_patch_note", "silica_props", "silica_query_table", "silica_read",
    "silica_read_note", "silica_recall", "silica_related", "silica_remember",
    "silica_restore", "silica_rework", "silica_run_injector", "silica_run_organizer",
    "silica_search", "silica_search_context", "silica_semantic_search", "silica_session_search",
    "silica_snapshot", "silica_tables", "silica_timeline", "silica_unresolved",
    "silica_vault_report", "silica_vaults", "silica_web_answer", "silica_write_note",
    "submit_repaired_ops", "web_fetch", "web_search",
}
SHARED_COMMANDS = {
    "/aliases", "/anneal", "/changes", "/check", "/clear", "/connect", "/contested", "/convert",
    "/cooccur", "/curate", "/dedup", "/delete", "/embed", "/enrich", "/episodes", "/exit", "/fetch",
    "/find", "/flashcards", "/graph", "/help", "/impact", "/incognito", "/keep", "/lexical", "/mail",
    "/map", "/memory", "/migrate", "/model", "/new", "/nucleate", "/organize", "/path", "/persona",
    "/plans", "/promote", "/remember", "/report", "/resume", "/revert", "/review", "/sessions",
    "/settings", "/slides", "/stale", "/status", "/thinking", "/tools", "/trash", "/undo", "/vault",
    "/verbose", "/verify", "/web", "/web-search", "/wiki", "/zotero",
}


def _paths(routes):
    # FastAPI 0.139 keeps an included router whole: its routes sit under it.
    for r in routes:
        sub = getattr(r, "original_router", None)
        yield from (_paths(sub.routes) if sub else [getattr(r, "path", "")])


def _owned(part: str) -> set[str]:
    return {x for v in VIEWS.values() for x in v.get(part, ())}


def test_every_route_tool_command_and_tab_is_a_views_or_named_shared():
    import silica.cli  # noqa: F401  registers every tool
    from silica.agent.constraints import _eligible
    from silica.tools import TOOLS
    from silica.ui.commands import COMMANDS
    from silica.ui.web import server

    _eligible()  # registers the loader and the connectors' tools
    owned = tuple(_owned("routes"))
    loose = {p for p in _paths(server.app.routes)
             if p not in SHARED_ROUTES and not any(p == r or p.startswith(r + "/") for r in owned)}
    assert not loose, f"place these routes in a view's row or SHARED_ROUTES: {sorted(loose)}"
    tools = {n for n in TOOLS if not n.startswith("mcp__")}
    assert _owned("tools") <= tools
    assert not tools - _owned("tools") - SHARED_TOOLS, sorted(tools - _owned("tools") - SHARED_TOOLS)
    commands = {c.name for c in COMMANDS}
    # an old name the dispatcher still runs is owned too, or a typed one gets past the view
    assert _owned("commands") <= commands | set(silica.cli._SHORTCUTS)
    runs = {silica.cli._SHORTCUTS[c] for c in _owned("commands") if c in silica.cli._SHORTCUTS}
    aliases = {k for k, f in silica.cli._SHORTCUTS.items() if f in runs}
    assert aliases <= _owned("commands"), sorted(aliases - _owned("commands"))
    assert not commands - _owned("commands") - SHARED_COMMANDS, sorted(commands - _owned("commands") - SHARED_COMMANDS)
    tabs = set(re.findall(r'data-tab="([a-z]+)"', (STATIC_SRC / "index.html").read_text(encoding="utf-8")))
    assert tabs == set(VIEWS)
