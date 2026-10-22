# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/undo takes back a note the chat created, and a move the chat made.

Found 2026-10-11 (Khoj audit, ADR-0092 review): /undo of a note silica_write_note
created wrote the checkpoint floor "" back and left a 0-byte note that kept
taking name links; silica_move recorded nothing, so the GUI's "move this note
back" sent an /undo that answered "Nothing to undo".
"""
from __future__ import annotations

import pytest

from silica.cli import _handle_direct_shortcut
from silica.config import CONFIG


@pytest.fixture
def vault(tmp_path, monkeypatch):
    import silica.driver
    import silica.kernel.write.checkpoints as cp
    import silica.kernel.write.undo_journal as uj

    v = tmp_path / "v"
    (v / "Corso").mkdir(parents=True)
    (v / "Archive").mkdir()
    (v / "vault.yaml").write_text("write_dir: ''\n", encoding="utf-8")
    (v / "Corso" / "Autoencoders.md").write_text("# Autoencoders\n\nbody\n", encoding="utf-8")
    (v / "Corso" / "Linker.md").write_text("# Linker\n\nsee [[Autoencoders]]\n", encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(v))
    monkeypatch.setattr(silica.driver, "_driver", None)
    monkeypatch.setattr(cp, "_store", cp.CheckpointStore(tmp_path / "cp.db"))
    monkeypatch.setattr(uj, "_store", uj.UndoJournalStore(tmp_path / "uj.db"))
    return v


# -- a note the chat created ------------------------------------------------

def test_undo_of_a_note_the_chat_created_removes_it(vault):
    from silica.tools.notes import silica_write_note

    res = silica_write_note("Corso/Fresh.md", "# Fresh\n\nnew text\n")
    assert res.get("success"), res
    assert (vault / "Corso" / "Fresh.md").exists()

    assert _handle_direct_shortcut("/undo Corso/Fresh.md", []) is True
    assert not (vault / "Corso" / "Fresh.md").exists()


@pytest.mark.parametrize("second", ["/undo Corso/Fresh.md", "/undo"])
def test_a_second_undo_brings_back_the_created_note_the_first_removed(vault, second):
    from silica.tools.notes import silica_write_note

    silica_write_note("Corso/Fresh.md", "# Fresh\n\nnew text\n")
    created = (vault / "Corso" / "Fresh.md").read_text(encoding="utf-8")
    (vault / "Corso" / "Fresh.md").write_text(created + "edited by hand\n", encoding="utf-8")

    _handle_direct_shortcut("/undo Corso/Fresh.md", [])
    assert not (vault / "Corso" / "Fresh.md").exists()
    _handle_direct_shortcut(second, [])
    # the note as it was when removed, the hand edit included
    assert (vault / "Corso" / "Fresh.md").read_text(encoding="utf-8") == created + "edited by hand\n"


def test_undo_walks_back_a_patch_then_removes_the_created_note(vault):
    from silica.tools.notes import silica_patch_note, silica_write_note

    silica_write_note("Corso/Fresh.md", "# Fresh\n\nnew text\n")
    created = (vault / "Corso" / "Fresh.md").read_text(encoding="utf-8")
    silica_patch_note("Corso/Fresh", "Extra", "more", "chat")

    _handle_direct_shortcut("/undo Corso/Fresh.md", [])
    assert (vault / "Corso" / "Fresh.md").read_text(encoding="utf-8") == created
    _handle_direct_shortcut("/undo Corso/Fresh.md", [])
    assert not (vault / "Corso" / "Fresh.md").exists()


def test_a_note_that_existed_empty_is_restored_empty_not_removed(tmp_path):
    from silica.kernel.write.checkpoints import CheckpointStore

    store = CheckpointStore(tmp_path / "cp.db")
    store.push("a.md", "", "filled")
    assert store.undo("a.md") == ""
    assert store.absent_floor("a.md") is False

    store.push("b.md", None, "created")
    assert store.undo("b.md") == ""
    assert store.absent_floor("b.md") is True


def test_a_store_written_before_the_absent_column_still_opens(tmp_path):
    import sqlite3

    from silica.kernel.write.checkpoints import CheckpointStore

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE checkpoints (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                      "path TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL);"
                      "INSERT INTO checkpoints (path, content, created_at) VALUES ('a.md', '', 1.0);"
                      "INSERT INTO checkpoints (path, content, created_at) VALUES ('a.md', 'x', 2.0);")
    con.commit()
    con.close()
    store = CheckpointStore(db)
    assert store.undo("a.md") == ""
    assert store.absent_floor("a.md") is False  # a legacy floor is not known to be absent


# -- a move the chat made ---------------------------------------------------

def test_undo_with_the_cards_ref_moves_the_note_back(vault):
    from silica.tools.wrapped import silica_move

    res = silica_move("Autoencoders", "Archive")  # the card's ref is the raw `to`
    assert res.get("success"), res
    assert (vault / "Archive" / "Autoencoders.md").exists()

    assert _handle_direct_shortcut("/undo Archive", []) is True
    assert (vault / "Corso" / "Autoencoders.md").exists()
    assert not (vault / "Archive" / "Autoencoders.md").exists()


def test_undo_with_the_landed_path_moves_the_note_back(vault):
    from silica.tools.wrapped import silica_move

    silica_move("Autoencoders", "Archive/Autoencoders.md")
    _handle_direct_shortcut("/undo Archive/Autoencoders.md", [])
    assert (vault / "Corso" / "Autoencoders.md").exists()


def test_bare_undo_takes_back_a_move_when_it_is_the_newest_action(vault):
    from silica.tools.notes import silica_patch_note
    from silica.tools.wrapped import silica_move

    silica_patch_note("Linker", "Extra", "more text", "chat")  # an older write
    silica_move("Autoencoders", "Archive")                     # the newest action

    assert _handle_direct_shortcut("/undo", []) is True
    assert (vault / "Corso" / "Autoencoders.md").exists()
    assert "more text" in (vault / "Corso" / "Linker.md").read_text(encoding="utf-8")


def test_a_patch_after_the_move_is_undone_before_the_move(vault):
    from silica.tools.notes import silica_patch_note
    from silica.tools.wrapped import silica_move

    silica_move("Autoencoders", "Archive")
    silica_patch_note("Archive/Autoencoders", "Extra", "later text", "chat")

    _handle_direct_shortcut("/undo Archive/Autoencoders.md", [])
    moved = vault / "Archive" / "Autoencoders.md"
    assert moved.exists() and "later text" not in moved.read_text(encoding="utf-8")


def test_a_failed_move_leaves_no_restore_point(vault):
    from silica.kernel.write.undo_journal import get_undo_journal
    from silica.tools.wrapped import silica_move

    res = silica_move("Missing note", "Archive")
    assert "error" in res
    assert get_undo_journal().last_chat_move(CONFIG.vault_path) is None
