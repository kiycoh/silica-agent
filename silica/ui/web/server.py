# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""FastAPI backend for the localhost GUI: the app, put together.

Every router and every module they share sits below this one and none reads it.
What is left here is what only the assembled app can answer: its startup, the
rules on every request, the page itself and the server that runs it.
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from silica.config import CONFIG, demo
from silica.kernel import vault_files
from silica.sources.zotero import ZoteroError
from silica.ui.web import (
    calendar_routes,
    chat_routes,
    discover_routes,
    explore_routes,
    files_routes,
    jobs_routes,
    learn_routes,
    metrics_routes,
    notes_routes,
    public_routes,
    security,
    session,
    settings_routes,
    skills_routes,
    stt_routes,
    tree_routes,
    workspaces_routes,
    zotero_web,
)
from silica.ui.web.session import SESSION

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def _prewarm_learn() -> None:
    """Learn's vault-wide reads before the first click: the notes, their
    parsed frontmatter and the prerequisite map were 8 s of a plan page's
    first open after a restart (2,166 notes, 2026-10-23). A failure costs only
    that wait, which the click then pays."""
    try:
        from silica.kernel.base import note_index
        from silica.kernel.learn import plans, store
        from silica.kernel.learn import learner

        note_index.meta(store.vault())
        plans.list_plans()
        learner.prerequisites_map()
    except Exception:
        logger.exception("learn prewarm failed")


def _zotero_detect() -> None:
    try:
        from silica.sources.zotero import detect
        detect(CONFIG.vault_path)
    except Exception:
        logger.debug("zotero detection skipped", exc_info=True)  # detection is an aid


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Host the Obsidian bridge for the GUI session — the plugin dials in and
    the driver hot-swaps to ws (falls back on drop). No-op without the
    [connect] extra or when the vault has no .obsidian/."""
    from silica.ui.connect import maybe_start_bridge
    from silica.ui.web import jobs

    # Before the first request (ADR-0099 point 4): a job this process did not
    # queue and that never ended was cut by the restart, and the student is
    # shown that, never a file that silently stopped.
    if n := jobs.recover():
        logger.warning("%d conversion(s) interrupted by the restart", n)
    bridge = None
    try:
        bridge = await maybe_start_bridge()
    except Exception:
        logger.exception("bridge auto-start failed")  # the GUI must not die for it
    # Session open for the web (ADR-0055 decision 9): what changed in Zotero
    # since the last session, for the Library tab's count. Off the loop, because
    # it is HTTP to Zotero; silent on failure, like the hook's line.
    asyncio.get_running_loop().run_in_executor(None, _zotero_detect)
    # MCP servers open in the background (ADR-0091), off the loop: reading
    # mcp.json and the keyring is file and D-Bus I/O.
    from silica.connectors.mcp_tools import start as _start_connectors
    asyncio.get_running_loop().run_in_executor(None, _start_connectors)
    asyncio.get_running_loop().run_in_executor(None, _prewarm_learn)
    yield
    if bridge is not None:
        await bridge.stop()


app = FastAPI(lifespan=_lifespan, dependencies=[Depends(security.same_origin_writes)])
# Added in this order because the last added runs first: a refused host is
# answered before anything else looks at the request.
app.middleware("http")(security.hosted_image_policy)
app.middleware("http")(security.withheld_views)
app.middleware("http")(security.require_loopback_host)


def _refused(status: int):
    async def answer(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": exc.args[0]}, status_code=status)

    return answer


# Sentences written for the person by code below this layer, which names no status.
app.add_exception_handler(vault_files.NotAChatFile, _refused(400))
app.add_exception_handler(vault_files.InboxClash, _refused(409))
app.add_exception_handler(ZoteroError, zotero_web.refused)

for _routes in (chat_routes, files_routes, workspaces_routes, metrics_routes, calendar_routes, explore_routes,
                notes_routes, tree_routes, stt_routes, settings_routes, zotero_web, learn_routes, skills_routes,
                jobs_routes, discover_routes, public_routes):
    app.include_router(_routes.router)


@app.get("/")
def index(request: Request):
    # Cache-bust the churning assets by content hash: StaticFiles sets no
    # Cache-Control, so browsers serve them stale from heuristic freshness
    # (edited JS never reaches the page). A content-keyed URL can't be stale.
    # The big vendored bundles keep their long-lived cache — only these churn.
    import hashlib

    from silica.config import CONFIG, hosted, served_views, withheld

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # work.js belongs here as much as the others: it owns the whole node
    # panel and churns with it, and without a version an edit to it reaches
    # nobody who has already loaded the page once.
    # Read back out of the document rather than listed here: app.js is now eight
    # cuts whose load order lives in index.html and nowhere else, and a second
    # copy of that list is a copy that drifts. The pattern is deliberately not
    # every /static/ href -- the vendored bundles keep their long-lived cache,
    # and hashing 3.5 MB of mermaid on each page load is the reason why.
    for asset in dict.fromkeys(
        re.findall(r"/static/(app[\w-]*\.(?:js|css)|work\.js)", html)
    ):
        ver = hashlib.sha256((STATIC_DIR / asset).read_bytes()).hexdigest()[:8]
        html = html.replace(f"/static/{asset}", f"/static/{asset}?v={ver}")
    # The preference, not the resolution: "auto" is a question only the browser
    # can answer, and the inline script in <head> answers it before first paint.
    # Stamping it server-side is what keeps a light session from flashing dark.
    pref = CONFIG.theme if CONFIG.theme in ("auto", "dark", "light") else "auto"
    html = html.replace('data-theme-pref="auto"', f'data-theme-pref="{pref}"')
    # The new chat's greeting says it (ADR-0048, amendment 2026-10-22), and
    # stamping it is what lets the first word written be the name.
    if CONFIG.user_name:
        html = html.replace('<html lang="en"', '<html lang="en" data-name="'
                            f'{_html.escape(CONFIG.user_name, quote=True)}"', 1)
    # A demo's controls for what it refuses (ADR-0139 point 6) go before the
    # first paint too, by the same kind of stamp, and the field names whose
    # notes these are. Ahead of the hosted stamp, which the account's line reads.
    if demo():
        html = html.replace('<html lang="en"', '<html lang="en" data-demo', 1)
        html = html.replace('placeholder="Ask about your notes"', 'placeholder="Ask about the sample vault"', 1)
    # Stamped, not probed, for the same reason: the views a hosted account
    # does not get (ADR-0111 point 3) must be gone before the first paint. A
    # demo is stamped as served_views() clamps it, even on a unit that lost
    # SILICA_HOSTED, or the page offers tabs the server answers 404 for.
    if hosted() or demo():
        html = html.replace('<html lang="en"', '<html lang="en" data-hosted', 1)
        # The views this account gets (ADR-0140 point 4): every element of one
        # is marked, and the CSS hides each unmarked one, so a view added later
        # stays hidden until the tenant's list serves it.
        for view in served_views():
            html = re.sub(rf'data-(?:tab|view)="{view}"', r"\g<0> data-served", html)
        # A help row keyed by a route is marked too, unless this process refuses
        # the route, as a demo refuses Settings and uploads (ADR-0139 point 6).
        gone = withheld("routes")
        html = re.sub(r'data-route="([^"]*)"', lambda m: m[0] if m[1] in gone else f"{m[0]} data-served", html)
        # The account the window is signed in as, for the menu under SILICA
        # (ADR-0122, amendment 2026-10-22). The gateway sets X-Silica-Email
        # (ADR-0154), Cloudflare Access its own header until the switch, and the
        # host's Caddy passes a tenant only what one of them set (silica-hosted
        # deploy/Caddyfile). It is shown back to whoever sent it and decides
        # nothing, so a forged one forges only its own page. Sign out is offered
        # behind the gateway alone: under Access /sign-out would reach this
        # process and answer 404.
        gateway = request.headers.get("x-silica-email", "")
        email = gateway or request.headers.get("cf-access-authenticated-user-email", "")
        if email:
            signout = " data-signout" if gateway else ""
            html = html.replace(
                '<html lang="en" data-hosted',
                f'<html lang="en" data-hosted data-user="{_html.escape(email, quote=True)}"{signout}', 1)
    return HTMLResponse(html)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def serve(port: int = 8765) -> None:
    """Apply config, print the address, then block on uvicorn. No browser is
    opened: the person opens the address the banner prints."""
    import uvicorn

    from silica.ui.banner import print_banner
    from silica.ui.console import CONSOLE

    SESSION.reset()

    # A hosted tenant listens on a Unix socket in its own directory, never on a
    # TCP port (ADR-0085 point 1): loopback is open to every tenant, because the
    # shared embedder lives there, so a port would let one tenant's process
    # reach another's GUI. uvicorn makes the socket 0666; the directory's
    # permissions are what keep it the proxy's alone.
    sock = os.environ.get("SILICA_GUI_SOCKET", "").strip() or None

    print_banner()
    where = f"unix:{sock}" if sock else f"http://127.0.0.1:{port}"
    CONSOLE.print(f"  [dim]GUI live at[/] [cyan]{where}[/]\n")

    # Built here rather than through uvicorn.run(), which keeps the Server to
    # itself: /narration/sse polls should_exit to end itself on the way out.
    # timeout_graceful_shutdown is the backstop under it, for the one stream
    # that does not poll - an in-flight /chat turn, which carries its own cancel
    # path and would otherwise hold Ctrl+C for the length of the turn.
    # log_config=None: uvicorn installs no logging of its own, so its records
    # reach the handler `_setup_logging` already put on root and obey its level
    # (quiet by default, the full access log under --verbose). Two reasons, and
    # the second is a defect. The banner it prints duplicates the line above.
    # And its colourized formatter re-renders each record from the
    # `color_message` extra, which still carries the raw `%d`/`%s`, while
    # litellm >=1.98 attaches a redaction filter to `uvicorn.error` that
    # interpolates the message and then sets `record.args = None` — so on a TTY
    # every uvicorn line with arguments printed its format string verbatim
    # ("Started server process [%d]", "Uvicorn running on %s://%s:%d"). Piped
    # output escaped it, which is why it never showed in a captured log.
    SESSION.server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, uds=sock, timeout_graceful_shutdown=1,
        log_config=None))
    try:
        try:
            SESSION.server.run()
        except KeyboardInterrupt:
            # uvicorn re-raises the signal it captured, AFTER it has shut down
            # cleanly, and `silica` is installed as cli:main - so the module's
            # own __main__ guard never runs and nothing above here catches it.
            # Without this a tidy Ctrl+C ends in a traceback that reads as a
            # crash. The shutdown already happened; there is nothing to salvage.
            pass
    finally:
        session.capture_own_session()  # last chance: this conversation ends with the server
