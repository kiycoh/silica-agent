# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The trash (ADR-0128): every note a delete took out of the vault, the
user's or the chat's, read from the undo journal that already held its body.
One note restores alone, not with the run it was deleted in; "delete for
good" leaves no trace of the body in the journal's files."""
from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from silica.cli import _handle_direct_shortcut
from silica.config import CONFIG


@pytest.fixture
def vault(tmp_path, monkeypatch):
    import silica.driver
    import silica.kernel.write.checkpoints as cp
    import silica.kernel.write.undo_journal as uj

    v = tmp_path / "v"
    (v / "Corso").mkdir(parents=True)
    (v / "vault.yaml").write_text("write_dir: ''\n", encoding="utf-8")
    for name in ("A", "B", "C"):
        (v / "Corso" / f"{name}.md").write_text(f"# {name}\n\nbody of {name}\n", encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(v))
    monkeypatch.setattr(silica.driver, "_driver", None)
    monkeypatch.setattr(cp, "_store", cp.CheckpointStore(tmp_path / "cp.db"))
    monkeypatch.setattr(uj, "_store", uj.UndoJournalStore(tmp_path / "uj.db"))
    return v


@pytest.fixture
def out(monkeypatch):
    console = Console(record=True, width=300)
    monkeypatch.setattr("silica.cli.CONSOLE", console)
    return lambda: console.export_text()


def _delete(*names, source="user:/delete"):
    from silica.tools.wrapped import delete_notes_with_restore_point

    notes = [(f"Corso/{n}.md", (Path(CONFIG.vault_path) / "Corso" / f"{n}.md").read_text())
             for n in names]
    return delete_notes_with_restore_point(notes, source)["run"]


def _trash():
    from silica.kernel.write.undo_journal import get_undo_journal

    return get_undo_journal().trashed(CONFIG.vault_path)


# -- the journal --------------------------------------------------------------

def test_the_trash_holds_the_users_and_the_chats_deletes_newest_first(vault):
    from silica.kernel.write.undo_journal import revert_run

    _delete("A")
    _delete("B", source="chat:silica_delete")
    assert [(t["path"], t["source"]) for t in _trash()] == [
        ("Corso/B.md", "chat:silica_delete"), ("Corso/A.md", "user:/delete")]
    # an undo of the run takes the note out of the trash with it
    revert_run(_trash()[0]["run"])
    assert [t["path"] for t in _trash()] == ["Corso/A.md"]


def test_one_note_of_a_folder_delete_comes_back_alone(vault):
    from silica.kernel.write.undo_journal import get_undo_journal, restore_from_trash

    run = _delete("A", "B")
    item = next(t for t in _trash() if t["path"] == "Corso/A.md")
    assert restore_from_trash(item) is None
    assert (vault / "Corso/A.md").read_text() == "# A\n\nbody of A\n"
    assert not (vault / "Corso/B.md").exists()
    assert [t["path"] for t in _trash()] == ["Corso/B.md"]
    assert get_undo_journal().run_info(run)["reverted_at"] is None  # B is still undoable
    # the last note out closes the run, so the toast's undo finds nothing left
    assert restore_from_trash(_trash()[0]) is None
    assert get_undo_journal().run_info(run)["reverted_at"] is not None


def test_a_note_written_since_at_the_path_is_not_overwritten(vault):
    from silica.kernel.write.undo_journal import restore_from_trash

    _delete("A")
    (vault / "Corso/A.md").write_text("# A\n\nnew\n", encoding="utf-8")
    assert restore_from_trash(_trash()[0]) == "a note is at that path now"
    assert (vault / "Corso/A.md").read_text() == "# A\n\nnew\n"
    assert len(_trash()) == 1


def test_delete_for_good_leaves_no_byte_of_the_body_in_the_journal(vault, tmp_path):
    from silica.kernel.write.undo_journal import get_undo_journal

    marker = "zqxj-the-only-copy-of-this-sentence"
    (vault / "Corso/A.md").write_text(f"# A\n\n{marker}\n", encoding="utf-8")
    _delete("A")
    files = [p for p in tmp_path.glob("uj.db*")]
    assert any(marker.encode() in p.read_bytes() for p in files)  # the probe can see it
    item = _trash()[0]
    get_undo_journal().leave_trash(item["run"], item["path"])
    assert _trash() == []
    for p in tmp_path.glob("uj.db*"):
        assert marker.encode() not in p.read_bytes(), p.name


# -- the REPL -----------------------------------------------------------------

def test_slash_trash_lists_restores_erases_and_empties(vault, out):
    _delete("A", "B", "C")
    assert _handle_direct_shortcut("/trash", []) is True
    text = out()
    assert "Trash: 3 notes" in text and "Corso/A.md" in text and "you" in text

    _handle_direct_shortcut("/trash restore Corso/A.md", [])
    assert "Restored: Corso/A.md" in out() and (vault / "Corso/A.md").exists()
    _handle_direct_shortcut("/trash erase Corso/B.md", [])
    assert "Deleted for good: Corso/B.md" in out()
    assert [t["path"] for t in _trash()] == ["Corso/C.md"]
    _handle_direct_shortcut("/trash restore Corso/B.md", [])
    assert "Not in the trash" in out()
    _handle_direct_shortcut("/trash empty", [])
    assert _trash() == [] and not (vault / "Corso/C.md").exists()
    _handle_direct_shortcut("/trash", [])
    assert out().rstrip().endswith("The trash is empty.")


def test_slash_delete_says_the_note_is_in_the_trash(vault, out):
    _handle_direct_shortcut("/delete Corso/A.md", [])
    assert "In the trash: /undo Corso/A.md brings it back." in out()


# -- the GUI's endpoints ------------------------------------------------------

@pytest.fixture
def client(tmp_vault):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.ui.web import server

    tmp_vault.note("Corso/A.md", "# A\n")
    tmp_vault.note("Corso/B.md", "# B\n")
    server._reset_session()
    yield TestClient(server.app), Path(CONFIG.vault_path)
    server._end_turn()


def test_the_gui_lists_restores_one_note_and_erases(client):
    tc, vault = client
    run = tc.post("/tree/delete", json={"folder": "Corso"}).json()["run"]
    items = tc.get("/trash").json()["items"]
    assert {t["path"] for t in items} == {"Corso/A.md", "Corso/B.md"}
    assert all(t["run"] == run and t["taken"] is False for t in items)

    assert tc.post("/trash/restore", json={"run": run, "path": "Corso/A.md"}).status_code == 200
    assert (vault / "Corso/A.md").read_text() == "# A\n" and not (vault / "Corso/B.md").exists()
    # only what this vault's trash lists: a run or a path the page made up is not found
    assert tc.post("/trash/restore", json={"run": run, "path": "Corso/A.md"}).status_code == 404
    assert tc.post("/trash/erase", json={"run": "nope", "path": "Corso/B.md"}).status_code == 404

    (vault / "Corso/B.md").write_text("# B again\n", encoding="utf-8")
    assert tc.get("/trash").json()["items"][0]["taken"] is True
    r = tc.post("/trash/restore", json={"run": run, "path": "Corso/B.md"})
    assert r.status_code == 409 and (vault / "Corso/B.md").read_text() == "# B again\n"

    assert tc.post("/trash/erase", json={"all": True}).json() == {"erased": 1}
    assert tc.get("/trash").json()["items"] == []


def test_the_trash_is_the_last_group_of_the_notes_list():
    static = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    pane = html[html.index('id="side-vault"'):html.index("</section>", html.index('id="side-vault"'))]
    assert pane.index('id="tree"') < pane.index('id="trash-group"')
    assert 'id="trash-group" hidden' in pane
