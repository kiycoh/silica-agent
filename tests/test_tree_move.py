# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Moving and renaming from the tree (ADR-0126): a selection dropped on a
folder, or a note renamed in its row, as the user's act and ONE journal run
under user:/move, with a preflight that refuses a taken destination before
anything moves and an undo that brings the whole set back."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

STATIC = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"

WS = ("---\ntype: workspace\nnotes:\n- \"[[CorsoA/Lezione 1]]\"\n---\n# Tesi\n")


@pytest.fixture
def client(tmp_vault):
    from silica.ui.web import server

    # Lezione 2 is a name two courses share (ADR-0114): a bare [[Lezione 2]]
    # from CorsoA means CorsoA's, by the resolver's same-folder rule.
    tmp_vault.note("CorsoA/Lezione 1.md", "# Lezione 1\n\nsee [[Lezione 2]] and [[Indice]]\n")
    tmp_vault.note("CorsoA/Lezione 2.md", "# L2 A\n")
    tmp_vault.note("CorsoB/Lezione 2.md", "# L2 B\n")
    tmp_vault.note("CorsoA/Indice.md", "# Indice\n\n[[Lezione 1]] and [[CorsoA/Lezione 2]]\n")
    tmp_vault.note("Archivio/Vecchio.md", "# Vecchio\n")
    tmp_vault.note("Altro/Nome preso.md", "# Preso\n")
    tmp_vault.note("Workspaces/Tesi.md", WS)
    from silica.config import CONFIG

    server._reset_session()
    yield TestClient(server.app), server, Path(CONFIG.vault_path)
    server._end_turn()


def move(tc, **payload):
    return tc.post("/tree/move", json=payload)


# -- a drop on a folder -------------------------------------------------------

def test_three_notes_move_as_one_run_that_undo_finds_through_any_of_them(client):
    from silica.kernel.write.undo_journal import USER_MOVE_SOURCE, get_undo_journal

    tc, _, vault = client
    r = move(tc, paths=["CorsoA/Lezione 1.md", "CorsoA/Lezione 2.md", "CorsoA/Indice.md"], folder="Archivio")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [m["to"] for m in body["moved"]] == [
        "Archivio/Lezione 1.md", "Archivio/Lezione 2.md", "Archivio/Indice.md"]
    assert body["skipped"] == [] and body["error"] is None
    assert all((vault / m["to"]).exists() and not (vault / m["from"]).exists() for m in body["moved"])
    journal = get_undo_journal()
    assert journal.run_info(body["run"])["source"] == USER_MOVE_SOURCE
    # /undo in the chat finds the run by any one of its notes, or with no argument
    assert journal.last_chat_move(None, "Archivio/Indice.md")[0] == body["run"]
    assert journal.last_chat_move(None)[0] == body["run"]

    back = tc.post("/tree/undo", json={"run": body["run"]}).json()
    assert sorted(back["restored"]) == sorted(m["from"] for m in body["moved"]) and back["skipped"] == []
    assert all((vault / m["from"]).exists() for m in body["moved"])
    assert tc.post("/tree/undo", json={"run": body["run"]}).status_code == 409


def test_a_note_already_in_the_folder_is_skipped_and_counted(client):
    tc, _, vault = client
    body = move(tc, paths=["Archivio/Vecchio.md", "CorsoA/Indice.md"], folder="Archivio/").json()
    assert body["skipped"] == ["Archivio/Vecchio.md"]
    assert [m["to"] for m in body["moved"]] == ["Archivio/Indice.md"]
    assert (vault / "Archivio/Vecchio.md").exists()


def test_the_vault_root_is_a_folder(client):
    tc, _, vault = client
    body = move(tc, paths=["CorsoA/Indice.md"], folder="").json()
    assert [m["to"] for m in body["moved"]] == ["Indice.md"] and (vault / "Indice.md").exists()


def test_a_new_folder_is_made_by_the_move(client):
    tc, _, vault = client
    body = move(tc, paths=["CorsoA/Indice.md"], folder="Nuova/Sotto").json()
    assert (vault / "Nuova/Sotto/Indice.md").exists() and body["error"] is None


def test_a_taken_destination_refuses_the_whole_set_before_anything_moves(client, tmp_vault):
    tc, _, vault = client
    tmp_vault.note("Archivio/Indice.md", "# another Indice\n")
    r = move(tc, paths=["CorsoA/Lezione 2.md", "CorsoA/Indice.md"], folder="Archivio")
    assert r.status_code == 409
    assert "Archivio/Indice.md" in r.json()["detail"]
    assert (vault / "CorsoA/Lezione 2.md").exists() and (vault / "CorsoA/Indice.md").exists()
    assert (vault / "Archivio/Indice.md").read_text(encoding="utf-8") == "# another Indice\n"


def test_two_notes_of_one_name_cannot_land_in_one_folder(client):
    tc, _, vault = client
    r = move(tc, paths=["CorsoA/Lezione 2.md", "CorsoB/Lezione 2.md"], folder="Archivio")
    assert r.status_code == 409 and "Lezione 2" in r.json()["detail"]
    assert (vault / "CorsoA/Lezione 2.md").exists() and (vault / "CorsoB/Lezione 2.md").exists()


def test_a_moved_notes_ambiguous_link_keeps_its_target(client):
    """Out of CorsoA, a bare [[Lezione 2]] would resolve to CorsoB's by the
    shortest path; the move writes the path first, so it still means CorsoA's."""
    from silica.driver import DRIVER

    tc, _, vault = client
    assert move(tc, paths=["CorsoA/Lezione 1.md"], folder="Archivio").status_code == 200
    text = (vault / "Archivio/Lezione 1.md").read_text(encoding="utf-8")
    assert "[[CorsoA/Lezione 2]]" in text and "[[Indice]]" in text  # Indice is unique: left bare
    assert {r.path for r in DRIVER.links("Archivio/Lezione 1.md")} >= {"CorsoA/Lezione 2.md"}


def test_incoming_links_follow_in_a_body_and_in_a_workspaces_frontmatter(client):
    tc, _, vault = client
    assert move(tc, paths=["CorsoA/Lezione 1.md", "CorsoA/Lezione 2.md"], folder="Archivio").status_code == 200
    indice = (vault / "CorsoA/Indice.md").read_text(encoding="utf-8")
    assert "[[Archivio/Lezione 2]]" in indice and "[[CorsoA/Lezione 2]]" not in indice
    ws = (vault / "Workspaces/Tesi.md").read_text(encoding="utf-8")
    assert "[[Archivio/Lezione 1]]" in ws


def test_a_referrer_left_behind_keeps_its_target_when_the_name_is_shared(client):
    """CorsoA/Lezione 1 says [[Lezione 2]] and means CorsoA's by the same-folder
    rule; once CorsoA/Lezione 2 leaves, the bare name would resolve to another
    folder's namesake. The move writes the path into that referrer, as Obsidian
    does for a name several notes share."""
    from silica.driver import DRIVER

    tc, _, vault = client
    assert move(tc, paths=["CorsoA/Lezione 2.md"], folder="Archivio").status_code == 200
    body = (vault / "CorsoA/Lezione 1.md").read_text(encoding="utf-8")
    assert "[[Archivio/Lezione 2]]" in body and "[[Indice]]" in body
    assert {r.path for r in DRIVER.links("CorsoA/Lezione 1.md")} >= {"Archivio/Lezione 2.md"}


def test_the_response_names_the_referrers_the_page_must_reload(client):
    tc, _, _ = client
    body = move(tc, paths=["CorsoA/Lezione 2.md"], folder="Archivio").json()
    assert body["rewritten"] == ["CorsoA/Indice.md", "CorsoA/Lezione 1.md"]


# -- a rename -----------------------------------------------------------------

def test_a_rename_keeps_the_folder_rewrites_links_and_is_undone(client):
    from silica.kernel.write.undo_journal import USER_MOVE_SOURCE, get_undo_journal

    tc, _, vault = client
    r = move(tc, path="CorsoA/Indice.md", name="Sommario")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["moved"] == [{"from": "CorsoA/Indice.md", "to": "CorsoA/Sommario.md"}]
    assert "[[Sommario]]" in (vault / "CorsoA/Lezione 1.md").read_text(encoding="utf-8")
    assert get_undo_journal().run_info(body["run"])["source"] == USER_MOVE_SOURCE
    back = tc.post("/tree/undo", json={"run": body["run"]}).json()
    assert back["restored"] == ["CorsoA/Indice.md"] and (vault / "CorsoA/Indice.md").exists()
    assert "[[Indice]]" in (vault / "CorsoA/Lezione 1.md").read_text(encoding="utf-8")


def test_a_rename_refuses_a_name_another_note_carries_in_any_folder_or_case(client):
    tc, _, vault = client
    assert move(tc, path="CorsoA/Indice.md", name="nome PRESO").status_code == 409
    assert move(tc, path="CorsoA/Indice.md", name="Lezione 2").status_code == 409
    assert (vault / "CorsoA/Indice.md").exists()


def test_a_change_of_case_alone_is_allowed(client):
    tc, _, vault = client
    assert move(tc, path="CorsoA/Indice.md", name="indice").status_code == 200
    assert (vault / "CorsoA/indice.md").exists()


def test_bad_names_and_bad_folders_are_refused(client):
    tc, _, _ = client
    for name in ["", ".hidden", "a/b", "x#y", "[z]", "q|r"]:
        assert move(tc, path="CorsoA/Indice.md", name=name).status_code == 400, name
    for folder in [".nascosta", "ok/.nascosta", "x#y"]:
        assert move(tc, paths=["CorsoA/Indice.md"], folder=folder).status_code == 400, folder
    assert move(tc, paths=["CorsoA/Indice.md", "CorsoA/Lezione 2.md"], name="x").status_code == 400
    assert move(tc, paths=["Nowhere.md"], folder="Archivio").status_code == 404
    assert move(tc).status_code == 400


# -- what the drop may not do -------------------------------------------------

def test_a_running_turn_refuses_the_move_and_the_undo(client):
    tc, server, vault = client
    run = move(tc, paths=["CorsoA/Indice.md"], folder="Archivio").json()["run"]
    assert server._begin_turn()
    assert move(tc, paths=["CorsoA/Lezione 2.md"], folder="Archivio").status_code == 409
    assert tc.post("/tree/undo", json={"run": run}).status_code == 409
    assert (vault / "CorsoA/Lezione 2.md").exists()


def test_the_undo_takes_back_only_a_move_or_a_delete_the_person_made(client):
    from silica.kernel.write.undo_journal import CHAT_MOVE_SOURCE, get_undo_journal

    tc, _, _ = client
    chat_run = get_undo_journal().start_run(source=CHAT_MOVE_SOURCE, vault=None)
    assert tc.post("/tree/undo", json={"run": chat_run}).status_code == 404


def test_an_undo_whose_origin_is_taken_is_refused_and_named(client, tmp_vault):
    tc, _, vault = client
    run = move(tc, paths=["CorsoA/Indice.md"], folder="Archivio").json()["run"]
    tmp_vault.note("CorsoA/Indice.md", "# a new Indice\n")
    back = tc.post("/tree/undo", json={"run": run}).json()
    assert back["restored"] == [] and back["skipped"] == ["CorsoA/Indice.md"]
    assert (vault / "CorsoA/Indice.md").read_text(encoding="utf-8") == "# a new Indice\n"
    assert (vault / "Archivio/Indice.md").exists()


def test_a_torn_move_keeps_its_undo_and_names_no_server_path(client, monkeypatch):
    """The driver renames first and rewrites referrers after: a failure in
    between leaves the file moved, so the inverse must stay."""
    import silica.driver
    import silica.tools.wrapped as wrapped
    from silica.kernel.write.undo_journal import get_undo_journal

    real = silica.driver.get_driver()

    class Tears:
        torn = False

        def move(self, ref, to):
            real.move(ref, to)
            if not Tears.torn:  # once: the undo's move back must go through
                Tears.torn = True
                raise RuntimeError("referrer rewrite failed under /srv/tenant/vault")

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(wrapped, "DRIVER", Tears())
    tc, _, vault = client
    body = move(tc, paths=["CorsoA/Indice.md"], folder="Archivio").json()
    assert body["moved"] == [] and body["error"] == "1 of 1 could not be moved"
    assert (vault / "Archivio/Indice.md").exists()
    assert [i.path for i, _ in get_undo_journal().inverses_for(body["run"])] == ["CorsoA/Indice.md"]
    back = tc.post("/tree/undo", json={"run": body["run"]}).json()
    assert back["restored"] == ["CorsoA/Indice.md"] and (vault / "CorsoA/Indice.md").exists()


def test_a_refusal_after_the_preflight_stops_the_set_and_keeps_what_moved(client, monkeypatch):
    import silica.driver
    import silica.tools.wrapped as wrapped
    from silica.kernel.write.undo_journal import get_undo_journal

    real = silica.driver.get_driver()

    class Refuses:
        def move(self, ref, to):
            if str(ref).endswith("Lezione 2.md"):
                raise RuntimeError("disk full at /srv/tenant/vault")
            return real.move(ref, to)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(wrapped, "DRIVER", Refuses())
    tc, _, vault = client
    body = move(tc, paths=["CorsoA/Indice.md", "CorsoA/Lezione 2.md", "CorsoA/Lezione 1.md"], folder="Archivio").json()
    assert [m["to"] for m in body["moved"]] == ["Archivio/Indice.md"]
    assert body["error"] == "2 of 3 could not be moved" and "/srv" not in body["error"]
    assert (vault / "CorsoA/Lezione 2.md").exists() and (vault / "CorsoA/Lezione 1.md").exists()
    assert [i.path for i, _ in get_undo_journal().inverses_for(body["run"])] == ["CorsoA/Indice.md"]


# -- the chat's move and the ingest's archive -------------------------------------

def test_silica_move_refuses_a_taken_destination(client):
    from silica.tools.wrapped import silica_move

    _, _, vault = client
    res = silica_move("CorsoA/Indice.md", "Archivio/Vecchio.md")
    assert "error" in res and "Archivio/Vecchio.md" in res["error"]
    assert (vault / "CorsoA/Indice.md").exists()
    assert (vault / "Archivio/Vecchio.md").read_text(encoding="utf-8") == "# Vecchio\n"


def test_the_drivers_move_still_replaces_as_the_archive_step_relies_on(client):
    """cleanup_source archives a source onto a fixed path and a second
    nucleation replaces the earlier copy: the refusal lives in the callers
    above, not in DRIVER.move (ADR-0126 point 12)."""
    from silica.driver import DRIVER

    _, _, vault = client
    DRIVER.move("CorsoA/Indice.md", "Archivio/Vecchio.md")
    assert (vault / "Archivio/Vecchio.md").read_text(encoding="utf-8").startswith("# Indice")


# -- the delete takes a selection ---------------------------------------------------

def test_tree_delete_takes_a_selection_as_one_run(client):
    tc, _, vault = client
    body = tc.post("/tree/delete", json={"paths": ["CorsoA/Indice.md", "Archivio/Vecchio.md"]}).json()
    assert body["deleted"] == ["CorsoA/Indice.md", "Archivio/Vecchio.md"]
    back = tc.post("/tree/undo", json={"run": body["run"]}).json()
    assert sorted(back["restored"]) == sorted(body["deleted"]) and (vault / "Archivio/Vecchio.md").exists()


# -- the page -----------------------------------------------------------------

def test_the_page_drags_copy_or_move_and_undoes_through_one_endpoint():
    ws = (STATIC / "app-workspaces.js").read_text(encoding="utf-8")
    assert 'effectAllowed = "copyMove"' in ws
    shell = (STATIC / "app-shell.js").read_text(encoding="utf-8")
    assert '"/tree/undo"' in shell and "/tree/undo-delete" not in shell
    assert 'setAttribute("aria-selected"' not in shell  # not valid on a link; the live count line says it
    editor = (STATIC / "app-editor.js").read_text(encoding="utf-8")
    # CodeMirror's own drop would insert a dragged row's URL: the page's type is taken first
    assert "drop(" in editor[editor.index("domEventHandlers"):]
