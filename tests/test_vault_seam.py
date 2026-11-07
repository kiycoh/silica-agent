# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""DRIVER is a Vault over a transport, however it got installed, and absence is
a type a dead bridge cannot pass for."""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

import silica.driver as driver_pkg
from silica.config import CONFIG
from silica.driver import NoteNotFound, get_driver, reader_for, set_driver
from silica.driver.fs_backend import ObsidianFSBackend
from silica.kernel.link import sweep
from silica.kernel.vault import Vault
from silica.kernel.write import session_changes


def test_a_fresh_process_gets_the_policy_from_the_driver_alone():
    """Nothing but `silica.driver` imported: the seam is filled by the kernel
    package Python runs on the way to kernel.base, not by an import above."""
    code = ("import sys; import silica.driver as d;"
            "assert callable(d.vault_seam.wrap);"
            "assert 'silica.kernel.vault' not in sys.modules;"
            "assert 'silica.kernel.write.preserve' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], check=True)


def test_set_driver_wraps_a_transport_and_a_vault_only_once(tmp_vault):
    transport = ObsidianFSBackend(CONFIG.vault_path)
    set_driver(transport)
    wrapped = get_driver()
    assert isinstance(wrapped, Vault) and wrapped.transport is transport
    set_driver(wrapped)
    assert get_driver() is wrapped
    set_driver(None)
    assert driver_pkg._driver is None


def test_a_foreign_reader_has_no_write_verb(tmp_path):
    reader = reader_for(str(tmp_path))
    assert not hasattr(reader, "overwrite") and not hasattr(reader, "create")


def test_a_refused_create_leaves_no_changes_row(tmp_vault):
    """The Changes row is written once the write can land: a baseline of None
    on a note that was already there would read as created, for the session."""
    tmp_vault.note("Notes/N.md", "body\n")
    with pytest.raises(FileExistsError):
        get_driver().create("Notes/N.md", "other\n")
    assert "Notes/N.md" not in session_changes.snapshot()


def test_exists_answers_as_a_read_would(tmp_vault):
    tmp_vault.note("Notes/N.md", "body\n")
    drv = get_driver()
    assert drv.exists("Notes/N.md") and drv.exists("N")
    assert not drv.exists("Notes/Nope.md")
    assert not drv.exists("/etc/hostname")   # read_note refuses it: this vault has no such note
    (Path(CONFIG.vault_path) / "Notes" / "Folder.md").mkdir()
    assert not drv.exists("Notes/Folder.md")


def _rpc_error(transport, text: str) -> Exception:
    """What `_on_frame` makes of an `rpc_error`, the one place a reply is typed."""
    async def reply():
        fut = asyncio.get_running_loop().create_future()
        transport._pending[1] = fut
        transport._on_frame({"type": "rpc_error", "id": 1, "error": text})
        return fut.exception()

    return asyncio.run(reply())


def test_the_plugins_missing_note_reply_is_typed_and_nothing_else_is():
    from silica.driver.ws_backend import ObsidianWSBackend

    transport = ObsidianWSBackend("ws://127.0.0.1:0")
    missing = _rpc_error(transport, "file not found: Notes/Ghost.md")
    assert isinstance(missing, NoteNotFound)
    assert str(missing) == "file not found: Notes/Ghost.md"   # failures.py reads the text
    other = _rpc_error(transport, "unknown method: nope")
    assert isinstance(other, RuntimeError) and not isinstance(other, NoteNotFound)


def test_exists_over_a_dead_bridge_raises_instead_of_answering_no():
    from silica.driver.ws_backend import ObsidianWSBackend

    transport = ObsidianWSBackend("ws://127.0.0.1:0")

    def dead(method, **params):
        raise RuntimeError("plugin disconnected")

    transport._rpc = dead
    with pytest.raises(RuntimeError, match="disconnected"):
        transport.exists("Notes/N.md")


def test_a_dead_bridge_does_not_strip_a_link(monkeypatch):
    """`resolution failure must never strip a link`: the path test read every
    RuntimeError as an absent note, so a dropped bridge emptied the link."""
    class Dead:
        def exists(self, ref):
            raise RuntimeError("plugin disconnected")

    monkeypatch.setattr("silica.driver.DRIVER", Dead())
    assert sweep._make_resolver()("Folder/Kept") == "Folder/Kept"
