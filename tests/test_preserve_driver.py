# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The preservation check where the bytes are written (ADR-0093 decisions 4,
5, 8 and 9), on the fs backend.

Until 2026-12-15 a write that drops a line logs one WARNING naming the
calling site and lands as before; with `preserve.REFUSE` on it is refused
before a byte, and before the Changes list records it.
"""
from __future__ import annotations

import hashlib
import logging

import pytest

from silica.config import CONFIG
from silica.kernel.write import preserve, session_changes
from silica.kernel.write.preserve import Loss, PreservationError

NOTE = "---\ntitle: N\nstatus: active\n---\n\n# N\n\nFirst line.\n\nSecond line.\n"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    import silica.driver
    import silica.kernel.write.checkpoints as cp
    import silica.kernel.write.undo_journal as uj

    v = tmp_path / "v"
    (v / "Notes").mkdir(parents=True)
    (v / "vault.yaml").write_text("write_dir: ''\n", encoding="utf-8")
    (v / "Notes" / "N.md").write_text(NOTE, encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(v))
    monkeypatch.setattr(silica.driver, "_driver", None)
    monkeypatch.setattr(cp, "_store", cp.CheckpointStore(tmp_path / "cp.db"))
    monkeypatch.setattr(uj, "_store", uj.UndoJournalStore(tmp_path / "uj.db"))
    monkeypatch.setattr(preserve, "_TURN_STARTED", preserve.ContextVar("t", default=None))
    return v


def _driver():
    from silica.driver import DRIVER
    return DRIVER


def _warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records
            if r.name == "silica.kernel.write.preserve" and r.levelno == logging.WARNING]


def _site_that_drops_a_line():
    _driver().overwrite("Notes/N.md", NOTE.replace("First line.\n", ""))


def test_a_lossy_overwrite_logs_the_site_and_still_lands(vault, caplog):
    caplog.set_level(logging.DEBUG, logger="silica.kernel.write.preserve")
    _site_that_drops_a_line()
    (rec,) = _warnings(caplog)
    assert rec.msg == "preservation: would refuse %s from %s.%s: %s"
    assert rec.args[0] == "Notes/N.md"
    assert rec.args[1:3] == (__name__, "_site_that_drops_a_line")
    assert "First line." in rec.args[3]
    assert "First line." not in (vault / "Notes" / "N.md").read_text(encoding="utf-8")


def test_a_keeping_overwrite_logs_nothing(vault, caplog):
    caplog.set_level(logging.DEBUG, logger="silica.kernel.write.preserve")
    _driver().overwrite("Notes/N.md", NOTE + "\nThird line.\n")
    assert not _warnings(caplog)


def test_refusing_raises_before_the_bytes_and_before_the_changes_list(vault, monkeypatch):
    monkeypatch.setattr(preserve, "REFUSE", True)
    with pytest.raises(PreservationError) as exc:
        _site_that_drops_a_line()
    assert exc.value.path == "Notes/N.md"
    assert (vault / "Notes" / "N.md").read_text(encoding="utf-8") == NOTE
    assert "Notes/N.md" not in session_changes.snapshot()


def test_a_declared_loss_logs_at_debug_with_its_name(vault, caplog):
    caplog.set_level(logging.DEBUG, logger="silica.kernel.write.preserve")
    _driver().overwrite("Notes/N.md", "gone\n", loss=Loss.PERSON)
    assert not _warnings(caplog)
    assert any("PERSON" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)


def test_staged_holds_only_under_the_inbox_or_sources(vault, caplog):
    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    (vault / "Inbox").mkdir()
    (vault / "Inbox" / "S.md").write_text("a\nb\n", encoding="utf-8")
    _driver().upsert("Inbox/S.md", "c\n", loss=Loss.STAGED)
    assert not _warnings(caplog)
    _driver().upsert("Notes/N.md", "c\n", loss=Loss.STAGED)  # a refined note outside
    assert len(_warnings(caplog)) == 1


def test_free_keys_pass_for_that_write(vault, caplog):
    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    _driver().overwrite("Notes/N.md", NOTE.replace("status: active", "status: done"),
                        free=("status",))
    assert not _warnings(caplog)


def test_set_prop_is_checked_with_its_own_key_free(vault, caplog):
    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    _driver().set_prop("Notes/N.md", "status", "done")
    assert not _warnings(caplog)
    assert "status: done" in (vault / "Notes" / "N.md").read_text(encoding="utf-8")


def test_a_move_rewrites_referrers_under_rename(vault, caplog):
    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    (vault / "Notes" / "R.md").write_text("# R\n\nSee [[N]] and [[N|the note]].\n", encoding="utf-8")
    _driver().move("Notes/N.md", "Notes/M.md")
    assert "[[M" in (vault / "Notes" / "R.md").read_text(encoding="utf-8")
    assert not _warnings(caplog)


# ---------------------------------------------------------------- re-create in the same turn

def _chat_delete(path: str) -> None:
    from silica.tools.wrapped import silica_delete
    # ADR-0092 removed `confirm`; Notes/N.md has no referrer, so the chat deletes it.
    assert silica_delete(path).get("success")


def test_a_create_after_a_chat_delete_in_the_same_turn_is_judged(vault, caplog):
    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    preserve.begin_turn()
    _chat_delete("Notes/N.md")
    _driver().create("Notes/N.md", NOTE.replace("Second line.\n", ""))
    (rec,) = _warnings(caplog)
    assert "Second line." in rec.args[3]


def test_a_faithful_recreate_passes(vault, caplog):
    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    preserve.begin_turn()
    _chat_delete("Notes/N.md")
    _driver().create("Notes/N.md", NOTE + "\nMore.\n")
    assert not _warnings(caplog)


def test_a_create_outside_any_turn_is_out_of_scope(vault, caplog):
    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    _chat_delete("Notes/N.md")
    _driver().create("Notes/N.md", "new\n")  # no turn: an MCP client's create
    assert not _warnings(caplog)


def test_a_delete_from_an_earlier_turn_is_out_of_scope(vault, caplog):
    import time

    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    preserve.begin_turn()
    _chat_delete("Notes/N.md")
    preserve._TURN_STARTED.set(time.time() + 1)  # the next turn began after it
    _driver().create("Notes/N.md", "new\n")
    assert not _warnings(caplog)


def test_a_user_delete_is_never_a_baseline(vault, caplog):
    from silica.kernel.write.ops import InverseOp, InverseOpKind
    from silica.kernel.write.undo_journal import get_undo_journal

    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    preserve.begin_turn()
    journal = get_undo_journal()
    run = journal.start_run(source="user:/delete", vault=CONFIG.vault_path)
    journal.record(run, InverseOp(kind=InverseOpKind.recreate_deleted, path="Notes/N.md",
                                  prior_content=NOTE), post_hash="absent")
    _driver().delete("Notes/N.md")
    _driver().create("Notes/N.md", "new\n")
    assert not _warnings(caplog)


# ---------------------------------------------------------------- an untouched draft

def _land_draft(vault) -> str:
    from silica.kernel.write.ops import InverseOp, InverseOpKind
    from silica.kernel.write.undo_journal import get_undo_journal

    draft = NOTE.replace("status: active", "status: active\nreview: soft-gate")
    (vault / "Notes" / "N.md").write_text(draft, encoding="utf-8")
    journal = get_undo_journal()
    run = journal.start_run(source="nucleate", vault=CONFIG.vault_path)
    journal.record(run, InverseOp(kind=InverseOpKind.delete_created, path="Notes/N.md"),
                   hashlib.sha256(draft.encode("utf-8")).hexdigest())
    return draft


def test_an_untouched_draft_is_re_authored_freely(vault, caplog):
    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    _land_draft(vault)
    _driver().overwrite("Notes/N.md", "---\ntitle: N\n---\n\nRewritten.\n")
    assert not _warnings(caplog)


def test_a_draft_the_person_touched_is_judged(vault, caplog):
    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    draft = _land_draft(vault)
    (vault / "Notes" / "N.md").write_text(draft + "\nMy own line.\n", encoding="utf-8")
    _driver().overwrite("Notes/N.md", "---\ntitle: N\n---\n\nRewritten.\n")
    assert len(_warnings(caplog)) == 1


def test_review_alone_never_grants_the_pass(vault, caplog):
    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    draft = NOTE.replace("status: active", "status: active\nreview: soft-gate")
    (vault / "Notes" / "N.md").write_text(draft, encoding="utf-8")
    _driver().overwrite("Notes/N.md", "---\ntitle: N\n---\n\nRewritten.\n")
    assert len(_warnings(caplog)) == 1


# ---------------------------------------------------------------- line endings

def test_a_crlf_note_keeps_crlf_and_composers_read_lf(vault):
    p = vault / "Notes" / "W.md"
    p.write_bytes(NOTE.replace("\n", "\r\n").encode("utf-8"))
    text = _driver().read_note("Notes/W.md").content
    assert "\r" not in text
    _driver().overwrite("Notes/W.md", text + "\nThird line.\n")
    raw = p.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")
    assert b"Third line.\r\n" in raw


def test_an_lf_note_stays_lf(vault):
    _driver().overwrite("Notes/N.md", NOTE.replace("\n", "\r\n") + "Third.\n")
    assert b"\r" not in (vault / "Notes" / "N.md").read_bytes()


def test_set_prop_and_a_move_keep_crlf_too(vault):
    (vault / "Notes" / "R.md").write_bytes(b"# R\r\n\r\nSee [[N]].\r\n")
    p = vault / "Notes" / "N.md"
    p.write_bytes(NOTE.replace("\n", "\r\n").encode("utf-8"))
    _driver().set_prop("Notes/N.md", "status", "done")
    assert p.read_bytes().count(b"\r\n") == p.read_bytes().count(b"\n")
    _driver().move("Notes/N.md", "Notes/M.md")
    raw = (vault / "Notes" / "R.md").read_bytes()
    assert b"[[M]]" in raw and raw.count(b"\r\n") == raw.count(b"\n")
