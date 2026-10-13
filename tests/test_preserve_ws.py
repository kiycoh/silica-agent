# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The preservation check on the ws backend (ADR-0093 decisions 4 and 8):
the prior is read from disk, the rule runs before the Changes list and the
RPC, and a CRLF note keeps CRLF while every read hands the composers LF."""
from __future__ import annotations

import logging

import pytest

pytest.importorskip("websockets")

from silica.config import CONFIG  # noqa: E402
from silica.kernel.write import preserve, session_changes  # noqa: E402
from silica.kernel.write.preserve import Loss, PreservationError  # noqa: E402
from tests.test_ws_backend import StubPluginServer  # noqa: E402

NOTE = "---\ntitle: N\nstatus: active\n---\n\n# N\n\nFirst line.\n\nSecond line.\n"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    import silica.kernel.write.undo_journal as uj
    from silica.driver.ws_backend import ObsidianWSBackend

    v = tmp_path / "v"
    (v / "Notes").mkdir(parents=True)
    (v / "Notes" / "N.md").write_text(NOTE, encoding="utf-8")
    (v / "Notes" / "W.md").write_bytes(NOTE.replace("\n", "\r\n").encode("utf-8"))
    monkeypatch.setattr(CONFIG, "vault_path", str(v))
    monkeypatch.setattr(uj, "_store", uj.UndoJournalStore(tmp_path / "uj.db"))
    monkeypatch.setattr(preserve, "_TURN_STARTED", preserve.ContextVar("t", default=None))
    srv = StubPluginServer(v)
    be = ObsidianWSBackend(url=srv.url, token=srv.token)
    yield be, v
    be.close()
    srv.stop()


def _warnings(caplog):
    return [r for r in caplog.records
            if r.name == "silica.kernel.write.preserve" and r.levelno == logging.WARNING]


def test_read_note_hands_the_composers_lf(ws):
    be, _v = ws
    assert "\r" not in be.read_note("Notes/W.md").content


def test_a_crlf_note_stays_crlf(ws):
    be, v = ws
    be.overwrite("Notes/W.md", be.read_note("Notes/W.md").content + "\nThird line.\n")
    raw = (v / "Notes" / "W.md").read_bytes()
    assert b"Third line.\r\n" in raw and raw.count(b"\r\n") == raw.count(b"\n")


def test_a_lossy_overwrite_logs_once_naming_the_site(ws, caplog):
    be, v = ws
    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    be.overwrite("Notes/N.md", NOTE.replace("First line.\n", ""))
    (rec,) = _warnings(caplog)
    assert rec.args[1:3] == (__name__, "test_a_lossy_overwrite_logs_once_naming_the_site")
    assert "First line." not in (v / "Notes" / "N.md").read_text(encoding="utf-8")


def test_refusing_stops_before_the_changes_list_and_the_rpc(ws, monkeypatch):
    be, v = ws
    monkeypatch.setattr(preserve, "REFUSE", True)
    with pytest.raises(PreservationError):
        be.overwrite("Notes/N.md", NOTE.replace("First line.\n", ""))
    assert (v / "Notes" / "N.md").read_text(encoding="utf-8") == NOTE
    assert "Notes/N.md" not in session_changes.snapshot()


def test_a_restore_is_declared(ws, caplog):
    from silica.driver.base import Txn
    from silica.kernel.write.ops import InverseOp, InverseOpKind

    be, v = ws
    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    inv = InverseOp(kind=InverseOpKind.restore_version, path="Notes/N.md", prior_content="old\n")
    be.restore(Txn(id="t", refs=[], inverses=[inv]))
    assert not _warnings(caplog)
    assert (v / "Notes" / "N.md").read_text(encoding="utf-8") == "old\n"


def test_set_prop_is_checked_with_its_key_free(ws, caplog):
    be, _v = ws
    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    be.set_prop("Notes/N.md", "status", "done")
    assert not _warnings(caplog)


def test_a_create_after_a_chat_delete_in_the_same_turn_is_judged(ws, caplog):
    from silica.kernel.write.ops import InverseOp, InverseOpKind
    from silica.kernel.write.undo_journal import CHAT_DELETE_SOURCE, get_undo_journal

    be, _v = ws
    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    preserve.begin_turn()
    journal = get_undo_journal()
    run = journal.start_run(source=CHAT_DELETE_SOURCE, vault=CONFIG.vault_path)
    journal.record(run, InverseOp(kind=InverseOpKind.recreate_deleted, path="Notes/N.md",
                                  prior_content=NOTE), post_hash="absent")
    be.delete("Notes/N.md")
    be.create("Notes/N.md", "---\ntitle: N\n---\n\nShorter.\n")
    assert len(_warnings(caplog)) == 1


def test_a_declared_create_passes(ws, caplog):
    be, _v = ws
    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    be.create("Notes/New.md", "new\n", loss=Loss.STAGED)
    assert not _warnings(caplog)
