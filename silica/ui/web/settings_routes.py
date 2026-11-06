# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Settings: the panel's rows, memory, vault instructions, connectors and local endpoints."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from silica.config import CONFIG
from silica.kernel.write import session_changes
from silica.ui.web import session
from silica.ui.web.session import SESSION

router = APIRouter()


@router.get("/health")
def health(all: bool = False):
    """The doctor's findings — non-ok by default, everything with `?all=1`.

    A server the user forgot to start degrades recall silently here: the
    embedder/reranker warnings go to the launching terminal's stderr, which the
    browser never shows. Same checks as `silica doctor`, so the two surfaces
    cannot disagree; ok rows are dropped for the sidebar notice, which is for
    what needs fixing, and kept for the settings panel's diagnostics and for a
    bug report, which needs the passing rows just as much.
    """
    from silica.config import hosted
    from silica.onboarding.checks import run_checks

    # "session capture" tells the user to edit .claude/settings.json — a
    # Claude-Code-integration concern the browser can do nothing about. It
    # stays out of the sidebar notices; ?all=1 (diagnostics) keeps it.
    # "converters" too when hosted: it reports the host's binaries, which
    # host.env chose (pdfium, no mineru) and a tenant cannot install.
    hidden = {"session capture", *(("converters",) if hosted() else ())}
    return [
        {"name": r.name, "status": r.status, "detail": r.detail, "hint": r.hint}
        for r in run_checks(CONFIG)
        if all or (r.status != "ok" and r.name not in hidden)
    ]


@router.get("/config")
def get_config():
    """Session config for the header panel: the active model (read-only — Silica
    has no runtime model-switch op, so this mirrors the TUI's display-only
    /model) plus the one live toggle the web surfaces, thinking (/thinking)."""
    from silica.model.providers import model_limits

    window = 0
    if CONFIG.model:
        window, _ = model_limits(CONFIG.provider, CONFIG.model)
    return {
        "model": CONFIG.model or "",
        # Empty means "the chat model does the worker's job too" (every call site
        # falls back to CONFIG.model), and the reader decides what to do with it.
        "worker_model": CONFIG.worker_model or "",
        "provider": CONFIG.provider or "",
        "context_window": window or 0,
        "show_thinking": CONFIG.show_thinking,
    }


# --- settings panel ----------------------------------------------------------
# The write half of /config is absorbed here: `thinking` is a persisted row like
# any other now, not a session-only flip. /config stays as the header chip's
# cheap read — GET /settings probes four endpoints, which is seconds the header
# label must not wait for.


@router.get("/settings")
def get_settings():
    """Every admitted row: value, where the value came from, whether it is
    locked, and its suggestions. Plus what About needs, so opening the panel is
    one round trip."""
    from silica import __version__
    from silica.config import hosted
    from silica.onboarding.wizard import resolve_env_path
    from silica.ui.web import settings as st
    from silica.update import behind_count

    on_server = hosted()
    return {
        "env_path": st.short_path(resolve_env_path()),
        "busy": SESSION.busy,
        # No hosted row has a pick-list fed by /models (ADR-0104 point 3), so
        # the four probes would run on every opening of the panel for nothing.
        "sections": st.read_sections(probe=not on_server),
        "version": __version__,
        # A tenant cannot update itself; the page drops the line by `hosted`.
        "behind": 0 if on_server else behind_count(),
        "issues_url": "https://github.com/kiycoh/silica-agent/issues",
        "hosted": on_server,
    }


def _reject_if_busy_or_locked(key: str) -> None:
    """One rule for every write: nothing lands while a turn is running.

    Deliberately not a per-row list of what a turn reads — that list would rot
    at the first new tool and no test would catch it. The cost is waiting for a
    response to finish.
    """
    from silica.ui.web import settings as st

    if SESSION.busy:
        raise HTTPException(status_code=409, detail="a response is running")
    if st.locked(key):
        raise HTTPException(status_code=409, detail=f"defined in the environment ({key})")


@router.post("/settings")
def set_setting(payload: dict):
    """Apply one row: live in CONFIG, persisted in the .env that wins at boot."""
    from silica.ui.web import settings as st

    key = str(payload.get("key", ""))
    _reject_if_busy_or_locked(key)
    if key == st.VAULT_KEY or key in st.EMBED_KEYS:
        raise HTTPException(status_code=400, detail="this row goes through /settings/confirm")
    result = st.apply(key, payload.get("value", ""))
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/settings/confirm")
async def confirm_setting(payload: dict):
    """The two rows that need a sequence, not an assignment: switching the vault
    and swapping the embedding model.

    Both are long enough to block, so they run off the event loop. The embedding
    swap does not repair itself: `sweep()` decides what to re-embed from mtimes,
    which a model change never touches, so stale vectors would be compared in an
    incompatible space in silence until something forces a full re-index.
    """
    from silica.config import hosted
    from silica.ui.web import settings as st

    key = str(payload.get("key", ""))
    value = str(payload.get("value", ""))
    if hosted():
        # Neither row is a hosted row (ADR-0104 point 1). Caddy refuses this
        # path too, but a vault switch skips apply's allow-list, so the refusal
        # must not rest on the front door alone.
        raise HTTPException(status_code=404, detail="not on this server")
    _reject_if_busy_or_locked(key)
    if key == st.VAULT_KEY:
        return await asyncio.to_thread(_apply_vault_switch, value)
    if key in st.EMBED_KEYS:
        return await asyncio.to_thread(_apply_embedding_swap, key, value)
    raise HTTPException(status_code=400, detail="this row does not need confirming")


def _apply_vault_switch(path: str) -> dict:
    from silica.cli import switch_vault
    from silica.ui.web import settings as st

    switched = switch_vault(path)
    if switched.error:
        return {"ok": False, "error": switched.error}
    # The Changes list describes paths in the vault we just left.
    session_changes.clear()
    # Live only, never persisted: a SILICA_VAULT line in the .env is ignored at
    # boot (config.load_user_env), the next launch curates the folder it starts
    # in. The resolved absolute path is what the caches were just rebuilt for.
    result = {"ok": True, "values": {st.VAULT_KEY: switched.vault}}
    # The fresh-session seed carries the old vault's map until it is rebuilt.
    session.prewarm_seed()
    notes = []
    if switched.write_dir:
        notes.append(f"writes confined to {switched.write_dir}/")
    if switched.invalid_write_dir:
        notes.append("vault.yaml declares an invalid write_dir — every write will be rejected")
    if switched.repo_warning:
        notes.append(switched.repo_warning)
    if switched.language_drift:
        notes.append(
            f"language {switched.language}, co-occurrence store frozen "
            f"{switched.store_language} — rebuild it with /cooccur --force"
        )
    return {**result, "vault": switched.vault, "notes": notes}


def _apply_embedding_swap(key: str, value: str) -> dict:
    from silica.tools import TOOLS
    from silica.ui.web import settings as st

    result = st.apply(key, value)
    if not result["ok"]:
        return result
    raw = TOOLS["silica_embed_refresh"].run(folder="", force=True)
    try:
        report = json.loads(raw)
    except (TypeError, ValueError):
        report = {"result": str(raw)[:200]}
    return {**result, "reindex": report}


@router.get("/bug_report")
def get_bug_report():
    """The diagnostic block a bug report attaches. Built server-side on purpose:
    in the browser the API keys sit in the panel's own fields, and a public issue
    is exactly the wrong place for one."""
    from silica.ui.web import settings as st

    return st.bug_report()


# --- User memory (ADR-0076) ----------------------------------------------------
# The Settings > Memory page and the chat's Undo. None of these checks the
# memory switch or incognito: they are the user's own hand on the files, and
# erasing must work while memory is paused.

def _memory_scope(scope: str) -> str:
    from silica.kernel import user_memory

    if scope not in user_memory.SCOPES:
        raise HTTPException(status_code=404, detail=f"no memory scope {scope!r}")
    return scope


@router.get("/memory")
def memory_view():
    from silica.kernel import user_memory
    from silica.ui.web import settings as st

    view = user_memory.view()
    for s in view["scopes"].values():
        s["path"] = st.short_path(Path(s["path"])) if s["path"] else ""
    return view


@router.put("/memory/{scope}")
def memory_write(scope: str, payload: dict):
    from silica.kernel import user_memory

    out = user_memory.write_file(_memory_scope(scope), str(payload.get("text", "")))
    if not out["ok"]:
        raise HTTPException(status_code=400, detail=out["error"])
    return out


@router.post("/memory/undo")
def memory_undo(payload: dict):
    from silica.kernel import user_memory

    out = user_memory.undo(_memory_scope(str(payload.get("scope", ""))),
                           str(payload.get("token", "")))
    if not out["ok"]:
        raise HTTPException(status_code=409, detail=out["error"])
    return out


@router.post("/memory/reset")
def memory_reset():
    from silica.kernel import user_memory

    return user_memory.reset()


# --- vault instructions (ADR-0127 point 4) --------------------------------------
# A file and not a settings row, as memory is; on hosted the vault is the one
# root an account can write and there is no file manager, so this is the way in.


@router.get("/instructions")
def instructions_view():
    from silica.kernel import vault_instructions as vi

    vault = CONFIG.vault_path or None
    return {"available": bool(vault), "text": vi.read(vault), "cap": vi.CAP, "path": vi.REL}


@router.put("/instructions")
def instructions_write(payload: dict):
    from silica.kernel import vault_instructions as vi

    out = vi.write(CONFIG.vault_path or None, str(payload.get("text", "")))
    if not out["ok"]:
        raise HTTPException(status_code=400, detail=out["error"])
    SESSION.seed = None   # built again by the next chat, with the new text (a prewarm could lose the race)
    return out


# Settings › Connectors (ADR-0089 point 8): every block's status, and one
# action of one block. Sync GET on purpose: FastAPI runs it in the threadpool,
# and a status reads the keyring, which may block on a D-Bus round trip.
@router.get("/connectors")
def connectors_status():
    from silica.connectors import connectors
    return [c.status() for c in connectors()]


@router.post("/connectors/{name}/{action}")
async def connectors_act(name: str, action: str, request: Request):
    from silica.connectors import find

    block = find(name)
    if block is None:
        raise HTTPException(404, f"no connector named {name}")
    try:
        payload = await request.json()
    except ValueError:
        payload = {}  # an action without a body (reload) posts nothing
    # Off the loop: Add lists a new server (up to 20 s) and a refusal is an
    # `ok: false` body, never a 5xx the panel would read as the server dying.
    return await asyncio.to_thread(block.act, action, payload if isinstance(payload, dict) else {})


@router.get("/endpoints")
def get_endpoints():
    from silica.ui.web import settings as st

    return st.endpoint_status()


@router.post("/endpoints/start")
async def start_endpoint(payload: dict):
    """Start one local endpoint from the command its own .env key names. Loading
    a model takes tens of seconds, so this waits on a worker thread."""
    from silica.ui.web import settings as st

    return await asyncio.to_thread(st.start_endpoint, str(payload.get("label", "")))
