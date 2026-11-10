# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What Settings does that is not one of its rows: the local endpoints, the safe-mode switch
and the diagnostics a bug report carries. The panel's table sits above this and names them."""
from __future__ import annotations

from pathlib import Path

from silica.config import CONFIG, live_env, silica_home


# The one row that is not an env var. Safe mode is a *view* over `write_dir` in
# vault.yaml — reading it as `bool(active_write_dir())` and writing it there is
# what keeps the manifest the single source of truth instead of adding a second
# boundary the enforcement seam would have to learn about.
SAFE_MODE_KEY = "safe_mode"


def base_urls() -> dict[str, str]:
    """Where each endpoint is expected to answer, right now."""
    from silica.onboarding import serve

    return serve.base_urls(CONFIG)


def apply_safe_mode(on: bool) -> dict:
    """Flip the write boundary in the active vault's `vault.yaml`.

    Turning it back on re-derives the boundary from the vault's content rather
    than restoring a remembered value: a source tree keeps its declared
    `docs/silica`, a prose vault gets the mirror. No "previous write_dir" is
    stored anywhere, so there is none to go stale against a vault that changed.
    """
    from silica.kernel.base.manifest import MANIFEST_REL, set_write_dir
    from silica.onboarding.adopt import write_dir_for

    vault = (CONFIG.vault_path or "").strip()
    if not vault or not Path(vault).is_dir():
        return {"ok": False, "error": "no vault to confine writes in"}
    declared = write_dir_for(vault) if on else ""
    try:
        path = set_write_dir(vault, declared)
    except OSError as exc:
        return {"ok": False, "error": f"could not write {MANIFEST_REL} ({exc})"}
    return {"ok": True, "path": short_path(path), "values": {
        SAFE_MODE_KEY: "true" if declared else "false",
        "write_dir": declared or "the vault root",
    }}


def short_path(path: Path) -> str:
    """A path as the panel says it: `~` for home, `./` for the working directory."""
    text = str(path)
    try:
        return f"./{path.relative_to(Path.cwd())}"
    except ValueError:
        pass
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return text


_STATUS_MARK = {"ok": "ok", "warn": "warn", "fail": "FAIL", "unknown": "?"}


def tilde(text: str) -> str:
    """Shorten the home directory to `~`. A vault path is a real name on a real
    disk and this is headed for a public issue tracker."""
    home = str(Path.home())
    return text.replace(home, "~") if home else text


def bug_report(rows: list[tuple[str, str]]) -> dict:
    """The diagnostic block a bug report attaches, and where to file it. `rows`
    is the label and value of each setting it may name (settings.reportable).

    Built here rather than in the browser for one reason: in the browser the API
    keys are in the DOM, one field away from whatever reads the panel. Nothing
    that can see a key ever builds this.
    """
    from silica import __version__
    from silica.onboarding.checks import run_checks

    lines = [f"silica {__version__}", f"vault: {tilde(CONFIG.vault_path or '(none)')}"]
    lines += [f"{label}: {tilde(value)}" for label, value in rows]
    lines.append("")
    lines.append("checks:")
    for r in run_checks(CONFIG):
        lines.append(f"  [{_STATUS_MARK.get(r.status, r.status)}] {r.name}: {tilde(r.detail)}")
    return {
        "payload": "\n".join(lines),
        "issues_url": "https://github.com/kiycoh/silica-agent/issues/new",
    }


def endpoint_status() -> list[dict]:
    """The four endpoints, probed concurrently: url, reachability, how many
    models each advertises, and whether silica has a command to start it.

    Reachability is `serve.ready` — a real request, because llama.cpp answers
    503 while it loads and an open port therefore lies.
    """
    from concurrent.futures import ThreadPoolExecutor

    from silica.onboarding.serve import ENDPOINTS, endpoint_model_ids, is_local, ready

    urls = base_urls()

    def probe(item: tuple[str, str]) -> dict:
        label, cmd_key = item
        url = urls.get(label, "")
        up = bool(url) and ready(url)
        return {
            "label": label,
            "url": url,
            "up": up,
            "models": len(endpoint_model_ids(url)) if up else 0,
            "local": bool(url) and is_local(url),
            "command": live_env(cmd_key),
            "command_key": cmd_key,
        }

    with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as pool:
        return list(pool.map(probe, ENDPOINTS))


def start_endpoint(label: str) -> dict:
    """Start one local endpoint from the command its env key names.

    The command is never editable from the browser: `serve.ensure` runs it
    through a shell on the justification that it came from a file the user wrote
    by hand, and a web field that writes the .env would make that false.
    """
    from silica.onboarding.serve import ENDPOINTS, ensure

    match = next((c for lbl, c in ENDPOINTS if lbl == label), None)
    if match is None:
        return {"ok": False, "error": f"unknown endpoint: {label}"}
    command = live_env(match)
    if not command:
        return {"ok": False, "error": f"no start command set ({match})"}
    url = base_urls().get(label, "")
    if not url:
        return {"ok": False, "error": f"{label} has no base url"}
    ok = ensure(label, url, command)
    log = silica_home() / "logs" / f"{label}-server.log"
    return {"ok": ok, "log": short_path(log)}
