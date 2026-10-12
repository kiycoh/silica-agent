# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The store for Class B secrets (ADR-0089 point 7).

A Class B secret opens an account of the owner beyond Silica: an MCP server's
token, its OAuth grant and client registration (ADR-0091 point 3). It lives in
the operating system's keyring, through `keyring` in the `[connectors]`
extra, and is read here and handed straight to the code that needs it: never
a `.env` line, never `os.environ`. What that removes is inheritance through
the environment, a read through a vault opened at `$HOME`, and every backup or
sync of `~/.silica`; a keyring does not stop another process of the same user
on Linux, and nothing here claims it does.

With no keyring backend (headless Linux, WSL) a save refuses until the owner
opts into `~/.silica/credentials/<scope>.json`, folder 0700, file 0600 from
its first byte.

A scope names one connector account ("mcp/github"); a field names one secret
in it ("GITHUB_TOKEN"). In the keyring that is service `silica`, user
`<scope>/<field>`.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

SERVICE = "silica"


class NoStore(RuntimeError):
    """No keyring backend and no opted-in file: the secret has nowhere to go."""


def _keyring():
    """The keyring module when a usable backend exists, else None."""
    try:
        import keyring
        import keyring.backends.fail
    except ImportError:
        return None  # no [connectors] extra: the file is the only store
    backend = keyring.get_keyring()
    if isinstance(backend, keyring.backends.fail.Keyring):
        return None
    # A chainer with nothing to chain reports priority 0; it would raise on use.
    if (getattr(backend, "priority", 1) or 0) <= 0:
        return None
    return keyring


def _dir() -> Path:
    # Read at call time, not import: the test sandbox and a vault switch move it.
    from silica.kernel.recall import paths

    return paths._SILICA_HOME / "credentials"


def folder() -> Path:
    """Where opted-in credential files live."""
    return _dir()


def file_path(scope: str) -> Path:
    return _dir() / (scope.replace("/", "-") + ".json")


def where(scope: str) -> str:
    """Where a secret of `scope` is read from and saved to: "keyring", "file"
    (the owner opted in), or "" (nowhere, so a save refuses)."""
    if _keyring() is not None:
        return "keyring"
    return "file" if file_path(scope).exists() else ""


def opt_in_file(scope: str) -> Path:
    """The owner's opt-in to a file store for `scope`: create it empty, 0600
    from the first byte, inside a 0700 folder."""
    folder = _dir()
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)  # mkdir's mode is masked by the umask; the chmod is not
    path = file_path(scope)
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("{}")
    path.chmod(0o600)  # an older file may carry another mode
    return path


def _read_file(scope: str) -> dict[str, str]:
    path = file_path(scope)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # A torn or hand-edited file reads as empty: the row then asks for the
        # secret again, which is recoverable; raising would kill every turn.
        logger.warning("credentials file %s unreadable: %s", path, exc)
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _write_file(scope: str, data: dict[str, str]) -> None:
    from silica.kernel.recall.paths import atomic_write_bytes

    # mode=0o600 rides on the temp file, which mkstemp already opened 0600, so
    # the replace never exposes the token under the umask's mode.
    atomic_write_bytes(file_path(scope), json.dumps(data, indent=2).encode("utf-8"), mode=0o600)


def get(scope: str, field: str) -> str | None:
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(SERVICE, f"{scope}/{field}")
        except Exception as exc:
            # A Secret Service with no session bus is viable on paper and
            # raises on use. Absent is the honest reading: the row asks again,
            # and a turn never dies on a locked keyring.
            logger.warning("keyring read failed for %s/%s: %s", scope, field, exc)
            value = None
        if value is not None:
            return value
    # The file is read even beside a keyring: a machine that gained a backend
    # after the owner opted in still finds what was saved there.
    return _read_file(scope).get(field)


def put(scope: str, field: str, value: str) -> str:
    """Save one secret; returns where it went. Raises NoStore when there is
    nowhere: the caller says so instead of falling back to the environment."""
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, f"{scope}/{field}", value)
            return "keyring"
        except Exception as exc:
            if not file_path(scope).exists():
                raise NoStore(f"the keyring refused the secret ({exc}); "
                              "opt into a credentials file instead") from exc
    if not file_path(scope).exists():
        raise NoStore("no keyring backend on this machine; opt into a credentials file "
                      f"({file_path(scope)}) to save it there")
    data = _read_file(scope)
    data[field] = value
    _write_file(scope, data)
    return "file"


def delete(scope: str, field: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE, f"{scope}/{field}")
        except Exception as exc:
            # Deleting what was never saved raises PasswordDeleteError; the
            # postcondition (no such secret) already holds.
            logger.debug("keyring delete %s/%s: %s", scope, field, exc)
    data = _read_file(scope)
    if field in data:
        del data[field]
        _write_file(scope, data)


def forget(scope: str, fields: list[str]) -> None:
    """Drop every named secret of `scope`, and its file when one exists: a
    keyring cannot be listed, so the caller names what it saved."""
    for field in fields:
        delete(scope, field)
    path = file_path(scope)
    if path.exists() and not _read_file(scope):
        path.unlink()
