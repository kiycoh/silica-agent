# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Deleting from the tree's row menu (ADR-0123): a note, or every note the
rail shows under a folder, as the user's act and ONE journal run, so the
toast's undo brings the whole set back. The row's ◇ left for the menu."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from silica.ui.web.graph_view import render_tree  # noqa: E402

STATIC = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"


@pytest.fixture
def client(tmp_vault):
    from silica.ui.web import server

    tmp_vault.note("Corso/ML/Autoencoders.md", "# Autoencoders\n\nsee [[VAE]]\n")
    tmp_vault.note("Corso/ML/VAE.md", "# VAE\n")
    tmp_vault.note("Corso/ML/Indice.md", "# Indice\n\n[[Autoencoders]] and [[VAE]]\n")
    # the prefix of the folder, not the folder: a delete of Corso/ML must not reach it
    tmp_vault.note("Corso/ML2/Other.md", "# Other\n")
    tmp_vault.note("Corso/Stat/Regressione.md", "# Regressione\n\nlike [[VAE]]\n")
    from silica.config import CONFIG

    server._reset_session()
    yield TestClient(server.app), server, Path(CONFIG.vault_path)
    server._end_turn()  # a test that claimed the turn's slot must not leave it claimed


# -- the tree -----------------------------------------------------------------

def test_the_rail_row_carries_the_menu_and_no_diamond():
    html = render_tree([{"id": "a/b/c.md", "path": "a/b/c.md", "type": "note"}], actions=True)
    assert 'class="tree-more" data-note="a/b/c.md"' in html
    # the folder's own path, as the ◇ carried it: the drag onto a workspace reads it
    assert 'class="tree-more" data-folder="a"' in html and 'data-folder="a/b"' in html
    assert "tree-ws" not in html and "M12 3l9 9-9 9-9-9z" not in html


def test_the_frame_tree_has_no_menu():
    assert "tree-more" not in render_tree([{"id": "a/c.md", "path": "a/c.md", "type": "note"}])


def test_no_script_or_style_still_looks_for_the_diamond():
    for f in [*STATIC.glob("*.js"), *STATIC.glob("*.css")]:
        assert ".tree-ws" not in f.read_text(encoding="utf-8"), f.name
    shell = (STATIC / "app-shell.js").read_text(encoding="utf-8")
    handler = shell[shell.index('$("#tree").addEventListener("click"'):]
    # inside the row, so tested before the row's own click opens the note
    assert handler.index('.closest(".tree-more")') < handler.index('.closest(".tree-note")')
    drag = (STATIC / "app-workspaces.js").read_text(encoding="utf-8")
    assert '.querySelector(".tree-more")?.dataset.folder' in drag


# -- a note -------------------------------------------------------------------

def test_a_linked_note_is_deleted_and_the_undo_brings_it_back(client):
    tc, _, vault = client
    r = tc.post("/tree/delete", json={"path": "Corso/ML/VAE.md"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] == ["Corso/ML/VAE.md"] and not (vault / "Corso/ML/VAE.md").exists()
    # the user's act: no refusal for a linked note, and each broken link named
    assert {d["note"] for d in body["dangling"]} == {
        "Corso/ML/Autoencoders.md", "Corso/ML/Indice.md", "Corso/Stat/Regressione.md"}

    back = tc.post("/tree/undo-delete", json={"run": body["run"]}).json()
    assert back["restored"] == ["Corso/ML/VAE.md"] and back["skipped"] == []
    assert (vault / "Corso/ML/VAE.md").read_text(encoding="utf-8") == "# VAE\n"
    assert tc.post("/tree/undo-delete", json={"run": body["run"]}).status_code == 409


def test_an_unknown_note_is_not_found(client):
    tc, _, _ = client
    assert tc.post("/tree/delete", json={"path": "../outside.md"}).status_code == 404
    assert tc.post("/tree/delete", json={}).status_code == 400


# -- a folder -----------------------------------------------------------------

def test_a_folder_is_its_notes_in_one_run_and_one_undo(client):
    from silica.kernel.write.undo_journal import USER_DELETE_SOURCE, get_undo_journal

    tc, _, vault = client
    body = tc.post("/tree/delete", json={"folder": "Corso/ML/"}).json()
    assert body["deleted"] == ["Corso/ML/Autoencoders.md", "Corso/ML/Indice.md", "Corso/ML/VAE.md"]
    assert (vault / "Corso/ML2/Other.md").exists()
    # links between the deleted notes break nothing kept; the one from outside does
    assert body["dangling"] == [{"note": "Corso/Stat/Regressione.md", "link": "VAE"}]
    assert get_undo_journal().run_info(body["run"])["source"] == USER_DELETE_SOURCE
    # /undo in the chat finds the same run by any one of its notes
    assert get_undo_journal().last_delete(None, "Corso/ML/Indice.md")[0] == body["run"]

    back = tc.post("/tree/undo-delete", json={"run": body["run"]}).json()
    assert sorted(back["restored"]) == body["deleted"]
    assert all((vault / p).exists() for p in body["deleted"])


def test_an_empty_folder_is_not_found(client):
    tc, _, _ = client
    assert tc.post("/tree/delete", json={"folder": "Nowhere"}).status_code == 404


# -- what the menu may not do -------------------------------------------------

def test_a_running_turn_refuses_the_delete_and_the_undo(client):
    tc, server, vault = client
    run = tc.post("/tree/delete", json={"path": "Corso/ML/Indice.md"}).json()["run"]
    assert server._begin_turn()
    assert tc.post("/tree/delete", json={"path": "Corso/ML/VAE.md"}).status_code == 409
    assert tc.post("/tree/undo-delete", json={"run": run}).status_code == 409
    assert (vault / "Corso/ML/VAE.md").exists()


def test_the_undo_takes_back_only_a_delete_the_person_made(client):
    from silica.kernel.write.undo_journal import CHAT_DELETE_SOURCE, get_undo_journal

    tc, _, _ = client
    chat_run = get_undo_journal().start_run(source=CHAT_DELETE_SOURCE, vault=None)
    assert tc.post("/tree/undo-delete", json={"run": chat_run}).status_code == 404
    assert tc.post("/tree/undo-delete", json={"run": "no-such-run"}).status_code == 404


def test_a_refused_note_stops_the_folder_and_leaves_the_run(client, monkeypatch):
    """The notes before it stay deleted and undoable; the refused one's
    inverse leaves the run, so the undo neither skips it nor sweeps it."""
    import silica.driver
    import silica.tools.wrapped as wrapped
    from silica.kernel.write.undo_journal import get_undo_journal

    real = silica.driver.get_driver()

    class Refuses:
        def delete(self, ref):
            if str(ref) == "Corso/ML/Indice.md":
                raise RuntimeError("File not found: /srv/tenant/vault/Corso/ML/Indice.md")
            return real.delete(ref)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(wrapped, "DRIVER", Refuses())
    tc, _, vault = client
    body = tc.post("/tree/delete", json={"folder": "Corso/ML"}).json()
    assert body["deleted"] == ["Corso/ML/Autoencoders.md"]
    assert body["error"] == "2 of 3 could not be deleted" and "/srv" not in body["error"]
    assert [i.path for i, _ in get_undo_journal().inverses_for(body["run"])] == ["Corso/ML/Autoencoders.md"]

    original = (vault / "Corso/ML/Indice.md").read_text(encoding="utf-8")
    back = tc.post("/tree/undo-delete", json={"run": body["run"]}).json()
    assert back == {"restored": ["Corso/ML/Autoencoders.md"], "skipped": []}
    assert (vault / "Corso/ML/Indice.md").read_text(encoding="utf-8") == original
