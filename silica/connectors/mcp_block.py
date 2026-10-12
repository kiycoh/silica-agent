# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The MCP client's block in Settings › Connectors (ADR-0091, "GUI and TUI").

One row per `mcpServers` entry: the key, `stdio` or `http`, and a state:
`connected · 12 tools`, `needs login`, `needs GITHUB_TOKEN` (with a password
field that writes the keyring), `inline secret in env.API_KEY` (with Move to
keyring), or `failed:` with the scrubbed error. `/connect` in the TUI drives
the same actions, so the two surfaces cannot disagree on what a row means.
"""
from __future__ import annotations

import logging
from typing import Any

from silica.connectors import approval, credentials, mcp_client, mcp_config

logger = logging.getLogger(__name__)

ACTIONS = ("add", "remove", "reload", "secure", "secret", "store_in_file", "login", "logout",
           "client_id", "approve")
# How long Add waits for a new server's first listing, so the catalog line it
# registers carries the tool names. A server slower than this still gets its
# line, with names from the next listing on.
ADD_LIST_TIMEOUT_S = 20.0


def _row(entry: mcp_config.Entry) -> dict[str, Any]:
    from silica.connectors import mcp_oauth, mcp_tools

    key = entry.key
    cached = mcp_client.cached_tools(key)
    notes = mcp_tools.not_offered(key)
    error = mcp_client.CLIENTS.errors.get(key, "")
    if entry.problem:
        state = f"failed: {entry.problem}"
    elif entry.inline:
        state = f"inline secret in {entry.inline[0]}"
    elif entry.needs:
        state = "needs " + ", ".join(entry.needs)
    elif mcp_client.CLIENTS.connected(key):
        state = f"connected · {len(cached)} tools"
    elif error == "needs login" or error.startswith(("needs a client ID", "needs a credentials store")):
        state = error
    elif error:
        state = f"failed: {error}"
    else:
        state = f"not connected · {len(cached)} tools listed" if cached else "not connected yet"
    return {
        "key": key,
        "kind": entry.kind or "?",
        "state": state,
        "needs": list(entry.needs),
        "inline": list(entry.inline),
        "oauth": entry.kind == "http",
        # A stored grant the server refused is not a login: the row offers
        # Log in, not only Log out (tester, 2026-10-12).
        "logged_in": entry.kind == "http" and mcp_oauth.has_tokens(key) and not error.startswith("needs"),
        # Settings polls a login on this, and redraws when it ends: a login
        # refused after the browser came back reads its reason on the row.
        "logging_in": entry.kind == "http" and mcp_oauth.login_state(key).get("done") is False,
        "client_id_field": error.startswith("needs a client ID"),
        "store": credentials.where(mcp_config.scope(key)),
        "tools": [{"name": t["name"], "note": notes.get(t["name"], "")} for t in cached],
    }


class McpBlock:
    name = "mcp"
    title = "MCP servers"

    def status(self) -> dict[str, Any]:
        base = {"name": self.name, "title": self.title, "account": "",
                "file": str(mcp_config.path()), "actions": list(ACTIONS)}
        if mcp_config.hosted():
            return {**base, "state": "local-only", "actions": [], "rows": [],
                    "detail": "connectors are local only"}
        if why := mcp_client.CLIENTS.available():
            base["detail_extra"] = why
        try:
            entries = mcp_config.load()
        except mcp_config.ConfigError as exc:
            return {**base, "state": "failed", "rows": [], "detail": str(exc)}
        rows = [_row(e) for e in entries.values()]
        connected = sum(r["state"].startswith("connected") for r in rows)
        state = "off" if not rows else "connected" if connected else "ready"
        detail = (f"{len(rows)} server{'s' if len(rows) != 1 else ''}, {connected} connected"
                  if rows else "no MCP server configured; Add takes a pasted mcpServers object")
        return {**base, "state": state, "rows": rows, "detail": detail}

    def act(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action not in ACTIONS:
            return {"ok": False, "error": f"unknown action: {action}"}
        if mcp_config.hosted():
            return {"ok": False, "error": "connectors are local only"}
        try:
            return {"ok": True, **(getattr(self, f"_{action}")(payload) or {})}
        except credentials.NoStore as exc:
            # Said apart from other refusals: the surface then offers the
            # file opt-in where the secret arrived, and retries.
            return {"ok": False, "error": str(exc), "no_store": True}
        except (mcp_config.ConfigError, mcp_client.CallFailed) as exc:
            return {"ok": False, "error": str(exc)}
        except KeyError as exc:
            return {"ok": False, "error": f"missing field: {exc.args[0]}"}

    # -- actions --
    def _add(self, p: dict[str, Any]) -> dict[str, Any]:
        from silica.connectors import mcp_tools

        if p.get("store_in_file"):
            # The owner's opt-in, given on the refusal: no keyring here, so the
            # pasted secrets go to one 0600 file per server.
            for key in mcp_config.paste_keys(str(p["json"])):
                credentials.opt_in_file(mcp_config.scope(key))
        keys, refused = mcp_config.add(str(p["json"]))
        for key in keys:
            mcp_client.CLIENTS.close(key)  # an edited entry restarts with its new spec
            try:
                mcp_client.CLIENTS.list_tools(key, timeout=ADD_LIST_TIMEOUT_S)
            except mcp_client.CallFailed as exc:
                # Added all the same: the row says why it did not connect.
                logger.info("mcp add %s: first listing failed: %s", key, exc)
        mcp_tools.reload()
        return {"added": keys, "refused": refused}

    def _remove(self, p: dict[str, Any]) -> None:
        from silica.connectors import mcp_tools

        key = str(p["key"])
        mcp_client.CLIENTS.close(key)
        mcp_config.remove(key)
        mcp_client.forget_cache(key)
        mcp_client.CLIENTS.errors.pop(key, None)
        mcp_tools.reload()

    def _reload(self, _p: dict[str, Any]) -> dict[str, Any]:
        from silica.connectors import mcp_tools

        mcp_client.CLIENTS.close_all()
        names = mcp_tools.reload()
        mcp_client.CLIENTS.warm([n[len("mcp__"):] for n in names])
        return {"tools": names}

    def _secure(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"moved": mcp_config.secure(str(p["key"]))}

    def _secret(self, p: dict[str, Any]) -> dict[str, Any]:
        key = str(p["key"])
        where = mcp_config.set_secret(key, str(p["name"]), str(p["value"]))
        mcp_client.CLIENTS.close(key)  # the child restarts with the secret in its env
        return {"stored": where}

    def _store_in_file(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"path": str(credentials.opt_in_file(mcp_config.scope(str(p["key"]))))}

    def _login(self, p: dict[str, Any]) -> dict[str, Any]:
        from silica.connectors import mcp_oauth

        return mcp_oauth.login(str(p["key"]))

    def _logout(self, p: dict[str, Any]) -> None:
        from silica.connectors import mcp_oauth

        mcp_oauth.logout(str(p["key"]))

    def _client_id(self, p: dict[str, Any]) -> None:
        key = str(p["key"])
        mcp_config.set_client_id(key, str(p["client_id"]).strip())
        mcp_client.CLIENTS.errors.pop(key, None)  # the row goes back to "log in"

    def _approve(self, p: dict[str, Any]) -> dict[str, Any]:
        if not approval.answer(str(p["id"]), bool(p.get("run"))):
            raise mcp_config.ConfigError("that card is no longer waiting")
        return {}


MCP_BLOCK = McpBlock()
