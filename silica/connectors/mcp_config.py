# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""`~/.silica/mcp.json`: the MCP servers Silica connects to (ADR-0091 point 2).

Claude Code's shape, so a pasted `.mcp.json` works as is and a pasted Claude
Desktop file brings its stdio servers: one key, `mcpServers`, mapping a name
to a stdio entry (`command`, `args`, `env`, optional `"type": "stdio"`) or an
HTTP one (`"type": "http"`, `url`, `headers`, optional `oauth`). `${VAR}` and
`${VAR:-default}` expand in `command`, `args`, `env`, `url` and `headers`, as
Claude Code expands them, with one difference (point 3): a reference whose
name reads as a secret, or any reference inside a header, resolves from the
keyring entry of that name and never from `os.environ`, so a Class B secret
never passes through the `.env` (ADR-0089 point 7).

One global file and not one per vault: an account is the user's, and a vault
switch must not log the user out of everything.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from silica.config import hosted  # noqa: F401  callers read it as mcp_config.hosted()
from silica.connectors import credentials

# The words scrub_credentials already treats as secret (silica/kernel/scrub.py),
# so one vocabulary decides both what is redacted and what is kept out of the
# environment.
_SECRET_WORDS = ("token", "key", "secret", "password", "bearer", "cookie",
                 "session", "credential", "signature")
_WORD = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
_REF = re.compile(r"\$\{([^}]*)\}")
_KEYRING = "keyring:"
# Fields the OAuth provider keeps under the server's scope (mcp_oauth.py);
# named here so Remove drops them with the rest.
OAUTH_FIELDS = ("oauth_tokens", "oauth_client")


class ConfigError(ValueError):
    """The file, or what was pasted, cannot be used; the message says why."""


class Unresolved(RuntimeError):
    """An entry that cannot start yet; the message is the row's state."""


@dataclass
class Entry:
    key: str
    kind: str                       # "stdio" | "http" | "" when refused
    raw: dict[str, Any]
    problem: str = ""               # why it cannot start, before any secret
    needs: list[str] = field(default_factory=list)   # secrets missing from the store
    inline: list[str] = field(default_factory=list)  # "env.API_KEY", "headers.Authorization"


def path() -> Path:
    from silica.kernel.recall import paths

    return paths._SILICA_HOME / "mcp.json"


def scope(key: str) -> str:
    return f"mcp/{key}"


def tool_name(key: str) -> str:
    """`mcp__<key>`, inside the `[A-Za-z0-9_-]{1,64}` every provider accepts."""
    return ("mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", key))[:64]


def is_secret_name(name: str) -> bool:
    for word in _WORD.findall(name):
        w = word.lower()
        w = w[:-1] if w.endswith("s") and len(w) > 3 else w
        if any(w == s or w.endswith(s) for s in _SECRET_WORDS):
            return True
    return False


def _kind_problem(key: str, entry: Any) -> tuple[str, str]:
    """(kind, problem) of one entry, from its shape alone."""
    if not isinstance(entry, dict):
        return "", f"entry '{key}' is not an object"
    kind = str(entry.get("type") or "").lower()
    if kind in ("sse", "ws"):
        return "", (f"entry '{key}': the {kind} transport is not supported; "
                    "use \"type\": \"http\" (Streamable HTTP) or a stdio command")
    if kind in ("", "stdio") and "url" in entry and "command" not in entry:
        return "", f"entry '{key}': a url needs \"type\": \"http\""
    if kind == "http":
        if not str(entry.get("url") or "").strip():
            return "", f"entry '{key}': an http entry needs a url"
        return "http", ""
    if kind not in ("", "stdio"):
        return "", f"entry '{key}': unknown type '{kind}'"
    if not str(entry.get("command") or "").strip():
        return "", f"entry '{key}': a stdio entry needs a command"
    return "stdio", ""


def _strings(entry: dict) -> list[tuple[str, str, bool]]:
    """(field label, value, every reference in it is secret) for the five
    expanding fields."""
    out: list[tuple[str, str, bool]] = []
    for name in ("command", "url"):
        if isinstance(entry.get(name), str):
            out.append((name, entry[name], False))
    for i, arg in enumerate(entry.get("args") or ()):
        if isinstance(arg, str):
            out.append((f"args[{i}]", arg, False))
    for name, value in (entry.get("env") or {}).items():
        if isinstance(value, str):
            out.append((f"env.{name}", value, False))
    for name, value in (entry.get("headers") or {}).items():
        if isinstance(value, str):
            out.append((f"headers.{name}", value, True))
    return out


def _inline(entry: dict) -> list[str]:
    """Literal secrets: every header value, and every secret-named env value,
    that carries no reference at all."""
    found = []
    for name, value in (entry.get("env") or {}).items():
        if isinstance(value, str) and value and not _REF.search(value) and is_secret_name(name):
            found.append(f"env.{name}")
    for name, value in (entry.get("headers") or {}).items():
        if isinstance(value, str) and value and not _REF.search(value):
            found.append(f"headers.{name}")
    return found


def _secret_ref(ref: str, in_header: bool) -> str | None:
    """The keyring field a reference reads, or None when it reads the
    environment.

    A header reference is a secret by default (a header is where a bearer
    token goes) unless it carries a default and a plain name: `${TRACE_ID:-none}`
    is a value with a fallback, as Claude Code expands it, and asking the
    keyring for it left the row reading "needs TRACE_ID" (tester, 2026-10-12).
    The name rule of ADR-0091 point 3 wins over a default."""
    if ref.startswith(_KEYRING):
        return ref[len(_KEYRING):]
    var, defaulted, _ = ref.partition(":-")
    if is_secret_name(var):
        return var
    return var if in_header and not defaulted else None


def _expand(key: str, value: str, in_header: bool, missing: list[str], unset: list[str]) -> str:
    def sub(m: re.Match) -> str:
        ref = m.group(1)
        secret = _secret_ref(ref, in_header)
        if secret is not None:
            got = credentials.get(scope(key), secret)
            if got is None:
                if secret not in missing:
                    missing.append(secret)
                return ""
            return got
        var, _, default = ref.partition(":-")
        got = os.environ.get(var)
        if got is not None:
            return got
        if ":-" in ref:
            return default
        if var not in unset:
            unset.append(var)
        return ""

    return _REF.sub(sub, value)


def secret_fields(key: str, entry: dict) -> list[str]:
    """Every keyring field this entry reads, for Remove."""
    names: list[str] = []
    for _label, value, in_header in _strings(entry):
        for ref in _REF.findall(value):
            secret = _secret_ref(ref, in_header)
            if secret is not None and secret not in names:
                names.append(secret)
    return names


def read_raw() -> dict[str, Any]:
    """The `mcpServers` mapping as written; {} when there is no file."""
    p = path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{p} is unreadable: {exc}") from exc
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise ConfigError(f"{p} has no \"mcpServers\" object")
    return servers


def _write_raw(servers: dict[str, Any]) -> None:
    from silica.kernel.recall.paths import atomic_write_bytes

    path().parent.mkdir(parents=True, exist_ok=True)
    # 0600 although the file holds no secret by design: a hand edit can put a
    # literal back, and the row that then refuses to start it is no reason for
    # the file to have been readable meanwhile.
    atomic_write_bytes(path(), (json.dumps({"mcpServers": servers}, indent=2) + "\n").encode("utf-8"),
                       mode=0o600)


def load() -> dict[str, Entry]:
    """Every configured entry with what keeps it from starting. Reads no
    network; reads the keyring only to tell present secrets from missing."""
    if hosted():
        return {}
    out: dict[str, Entry] = {}
    for key, raw in read_raw().items():
        kind, problem = _kind_problem(key, raw)
        entry = Entry(key=str(key), kind=kind, raw=raw if isinstance(raw, dict) else {}, problem=problem)
        if not problem:
            entry.inline = _inline(raw)
            missing: list[str] = []
            unset: list[str] = []
            for _label, value, in_header in _strings(raw):
                _expand(entry.key, value, in_header, missing, unset)
            entry.needs = missing
            if unset:
                entry.problem = f"${{{unset[0]}}} is not set and has no default"
        out[entry.key] = entry
    return out


def resolve(entry: Entry) -> dict[str, Any]:
    """The launch spec of a startable entry: {command, args, env} for stdio,
    {url, headers, oauth} for HTTP. Secrets are read from the store here and
    handed to the child's env or the request headers only."""
    if entry.problem:
        raise Unresolved(entry.problem)
    if entry.inline:
        raise Unresolved(f"inline secret in {entry.inline[0]}")
    missing: list[str] = []
    unset: list[str] = []
    raw = entry.raw

    def x(value: str, in_header: bool = False) -> str:
        return _expand(entry.key, value, in_header, missing, unset)

    if entry.kind == "stdio":
        spec: dict[str, Any] = {
            "command": x(raw["command"]),
            "args": [x(a) for a in raw.get("args") or () if isinstance(a, str)],
            "env": {str(k): x(v) for k, v in (raw.get("env") or {}).items() if isinstance(v, str)},
        }
    else:
        spec = {
            "url": x(raw["url"]),
            "headers": {str(k): x(v, True) for k, v in (raw.get("headers") or {}).items()
                        if isinstance(v, str)},
            "oauth": raw.get("oauth") if isinstance(raw.get("oauth"), dict) else None,
        }
    if missing:
        raise Unresolved("needs " + ", ".join(missing))
    if unset:
        raise Unresolved(f"${{{unset[0]}}} is not set and has no default")
    return spec


def _move_literals(key: str, entry: dict) -> tuple[dict, list[str]]:
    """A copy of `entry` with each inline secret saved to the keyring and
    replaced by `${keyring:<name>}`. Raises credentials.NoStore before
    anything is rewritten when a secret has nowhere to go."""
    moved = []
    out = json.loads(json.dumps(entry))
    for label in _inline(entry):
        section, name = label.split(".", 1)
        credentials.put(scope(key), name, out[section][name])
        out[section][name] = f"${{{_KEYRING}{name}}}"
        moved.append(label)
    return out, moved


def _parse_paste(text: str) -> dict[str, Any]:
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"not valid JSON: {exc}") from exc
    if not isinstance(obj, dict) or not obj:
        raise ConfigError("paste a JSON object: {\"mcpServers\": {...}} or {\"<name>\": {...}}")
    if "mcpServers" in obj:
        servers = obj["mcpServers"]
        if not isinstance(servers, dict):
            raise ConfigError("\"mcpServers\" must be an object")
        return servers
    if any(k in obj for k in ("command", "url", "type", "args")):
        raise ConfigError("name the entry: {\"<name>\": {\"command\": ...}}")
    return obj


def _split_paste(text: str) -> tuple[dict[str, Any], list[str]]:
    """The pasted entries Add can take, and one reason per entry it cannot,
    each naming its entry."""
    usable: dict[str, Any] = {}
    refused: list[str] = []
    for key, entry in _parse_paste(text).items():
        _kind, problem = _kind_problem(str(key), entry)
        if problem:
            refused.append(problem)
        else:
            usable[str(key)] = entry
    return usable, refused


def paste_keys(text: str) -> list[str]:
    """The server names a paste would add, for an opt-in asked before Add."""
    return list(_split_paste(text)[0])


def _stale_fields(key: str, old: Any, new: dict) -> list[str]:
    """What the store keeps for `old` that `new` will not read: the secrets
    only the old entry named, and the OAuth grant when the server it was
    issued by is not the one the new entry reaches."""
    if not isinstance(old, dict):
        return []
    keep = set(secret_fields(key, new))
    stale = [f for f in secret_fields(key, old) if f not in keep]
    same_server = (_kind_problem(key, old)[0] == "http" and old.get("url") == new.get("url"))
    return stale if same_server else [*stale, *OAUTH_FIELDS]


def add(text: str) -> tuple[list[str], list[str]]:
    """Merge a pasted `{"mcpServers": {...}}` or `{"<name>": {...}}` into the
    file, moving every inline secret to the keyring first. Returns the keys
    added and one reason per entry refused: an `sse` entry beside two good
    ones costs only itself. Raises when nothing in the paste can be added."""
    if hosted():
        raise ConfigError("connectors are local only")
    servers, refused = _split_paste(text)
    if not servers:
        raise ConfigError("; ".join(refused))
    rewritten = {key: _move_literals(key, entry)[0] for key, entry in servers.items()}
    merged = read_raw() if path().exists() else {}
    # A re-added key replaces its entry: what only the old one named would
    # stay in the keyring with nothing left to name it, and Remove finds a
    # secret by the names in the file (a keyring cannot be listed).
    stale = {key: _stale_fields(key, merged[key], entry) for key, entry in rewritten.items()
             if key in merged}
    merged.update(rewritten)
    _write_raw(merged)
    for key, fields in stale.items():
        for name in fields:
            # delete, not forget: an emptied credentials file is still the
            # owner's opt-in for this server's next secret.
            credentials.delete(scope(key), name)
    return list(rewritten), refused


def secure(key: str) -> list[str]:
    """Move to keyring: the inline secrets of a hand-edited entry."""
    if hosted():
        raise ConfigError("connectors are local only")
    servers = read_raw()
    if key not in servers:
        raise ConfigError(f"no server named '{key}'")
    servers[key], moved = _move_literals(key, servers[key])
    if moved:
        _write_raw(servers)
    return moved


def set_secret(key: str, name: str, value: str) -> str:
    """The value a row's password field asked for; returns where it went."""
    if hosted():
        raise ConfigError("connectors are local only")
    return credentials.put(scope(key), name, value)


def set_client_id(key: str, client_id: str) -> None:
    """The spec's last registration step: a client ID the user registered by
    hand, for a server that offers neither CIMD nor dynamic registration."""
    servers = read_raw()
    if key not in servers:
        raise ConfigError(f"no server named '{key}'")
    oauth = servers[key].get("oauth") if isinstance(servers[key].get("oauth"), dict) else {}
    servers[key]["oauth"] = {**oauth, "clientId": client_id}
    _write_raw(servers)
    # A new ID starts a new login: the stored registration may be the same ID
    # pinned to an issuer it no longer answers for, and a grant belongs to the
    # client it was issued to. The file opt-in stays (delete, not forget).
    for name in OAUTH_FIELDS:
        credentials.delete(scope(key), name)


def remove(key: str) -> None:
    """Delete the entry and every secret it kept in the store."""
    servers = read_raw()
    entry = servers.pop(key, None)
    if entry is None:
        raise ConfigError(f"no server named '{key}'")
    fields = secret_fields(key, entry) if isinstance(entry, dict) else []
    credentials.forget(scope(key), [*fields, *OAUTH_FIELDS])
    _write_raw(servers)
