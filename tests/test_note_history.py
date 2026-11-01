# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A note's history (ADR-0144): undo marks a version instead of deleting it, an
edit made outside Silica is kept as a version before Silica writes over it or
undoes past it, rows belong to their vault, and the GUI lists, diffs and
restores a version."""
from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from silica.cli import _handle_direct_shortcut
from silica.config import CONFIG
from silica.kernel.write.checkpoints import CheckpointStore


@pytest.fixture
def store(tmp_path):
    return CheckpointStore(tmp_path / "cp.db")


def test_a_hand_edit_between_two_writes_survives_the_undo_of_the_second(store):
    """Reproduced 2026-11-01: the undo returned "P1" and the edit was gone."""
    store.push("A.md", "ORIG", "P1")
    store.push("A.md", "P1 + HAND", "P2")  # the next write reads the edited note as prior
    assert store.undo("A.md") == "P1 + HAND"
    assert store.undo("A.md") == "P1"
    assert store.undo("A.md") == "ORIG"
    assert store.undo("A.md") is None


def test_a_line_ending_is_not_a_hand_edit(store):
    store.push("A.md", "ORIG", "P1\n")
    store.push("A.md", "P1\r\n", "P2")
    assert [v["kind"] for v in store.history("A.md")] == ["silica", "silica", "original"]


def test_undo_marks_the_version_and_the_history_keeps_it(store):
    store.push("A.md", "ORIG", "P1")
    store.push("A.md", "P1", "P2")
    assert store.undo("A.md") == "P1"
    assert store.depth("A.md") == 2
    hist = store.history("A.md")
    assert [(v["kind"], v["undone"]) for v in hist] == [
        ("silica", True), ("silica", False), ("original", False)]
    assert store.version("A.md", hist[0]["id"]) == "P2"


def test_undo_over_a_hand_edit_keeps_the_edit_as_an_undone_version(store):
    store.push("A.md", "ORIG", "P1")
    assert store.edited_by_hand("A.md", "P1 + HAND")
    assert store.undo("A.md", current="P1 + HAND") == "ORIG"
    hand = store.history("A.md")[0]
    assert hand["kind"] == "hand" and hand["undone"]
    assert store.version("A.md", hand["id"]) == "P1 + HAND"
    # the person's text is not a body Silica wrote
    import hashlib
    assert hashlib.sha256(b"P1 + HAND").hexdigest() not in store.hashes_for("A.md")


def test_rows_belong_to_their_vault(store, monkeypatch, tmp_path):
    import silica.kernel.write.checkpoints as cp

    monkeypatch.setattr(cp, "_current_vault", lambda: "/vault/one")
    store.push("Index.md", "ORIG", "ONE")
    monkeypatch.setattr(cp, "_current_vault", lambda: "/vault/two")
    assert store.depth("Index.md") == 0 and store.history("Index.md") == []
    store.push("Index.md", "TWO-ORIG", "TWO")
    assert store.undo("Index.md") == "TWO-ORIG"
    monkeypatch.setattr(cp, "_current_vault", lambda: "/vault/one")
    assert store.undo("Index.md") == "ORIG"


# -- /undo reads the disk first ------------------------------------------------

@pytest.fixture
def vault(tmp_path, monkeypatch):
    import silica.driver

    v = tmp_path / "v"
    v.mkdir()
    (v / "vault.yaml").write_text("write_dir: ''\n", encoding="utf-8")
    (v / "A.md").write_text("# A\n\nfirst\n", encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(v))
    monkeypatch.setattr(silica.driver, "_driver", None)
    return v


@pytest.fixture
def out(monkeypatch):
    console = Console(record=True, width=300)
    monkeypatch.setattr("silica.cli.CONSOLE", console)
    return lambda: console.export_text()


def test_slash_undo_says_it_takes_back_a_hand_edit_and_keeps_it(vault, out):
    from silica.kernel.write.checkpoints import get_checkpoint_store

    store = get_checkpoint_store()
    note = vault / "A.md"
    store.push("A.md", note.read_text(), "# A\n\nfirst\n\nsilica\n")
    note.write_text("# A\n\nfirst\n\nsilica\n\nmine\n", encoding="utf-8")  # an edit in Obsidian

    _handle_direct_shortcut("/undo A.md", [])
    assert note.read_text() == "# A\n\nfirst\n"
    assert "taken back too and kept in the note's history" in out()
    hand = store.history("A.md")[0]
    assert store.version("A.md", hand["id"]).endswith("mine\n")


def test_slash_undo_without_a_hand_edit_says_nothing_more(vault, out):
    from silica.kernel.write.checkpoints import get_checkpoint_store

    note = vault / "A.md"
    get_checkpoint_store().push("A.md", note.read_text(), "# A\n\nsilica\n")
    note.write_text("# A\n\nsilica\n", encoding="utf-8")
    _handle_direct_shortcut("/undo A.md", [])
    assert note.read_text() == "# A\n\nfirst\n"
    assert "taken back too" not in out()


# -- the GUI's endpoints ------------------------------------------------------

@pytest.fixture
def client(tmp_vault):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.ui.web import server

    tmp_vault.note("Corso/A.md", "# A\n\nfirst\n")
    server._reset_session()
    yield TestClient(server.app), Path(CONFIG.vault_path)
    server._end_turn()


def test_the_gui_lists_diffs_and_restores_a_version_and_undo_takes_the_restore_back(client):
    from silica.kernel.write.checkpoints import get_checkpoint_store

    tc, vault = client
    note = vault / "Corso/A.md"
    get_checkpoint_store().push("Corso/A.md", note.read_text(), "# A\n\nsecond\n")
    note.write_text("# A\n\nsecond\n", encoding="utf-8")

    versions = tc.get("/note/history", params={"path": "Corso/A"}).json()["versions"]
    assert [v["kind"] for v in versions] == ["silica", "original"]
    first = versions[1]["id"]

    lines = tc.get("/note/history/diff", params={"path": "Corso/A.md", "id": first}).json()["lines"]
    assert {"op": "-", "text": "second"} in lines and {"op": "+", "text": "first"} in lines

    assert tc.post("/note/history/restore", json={"path": "Corso/A.md", "id": first}).status_code == 200
    assert note.read_text() == "# A\n\nfirst\n"
    assert tc.get("/note/history", params={"path": "Corso/A.md"}).json()["versions"][0]["kind"] == "restore"

    _handle_direct_shortcut("/undo Corso/A.md", [])
    assert note.read_text() == "# A\n\nsecond\n"


def test_the_gui_refuses_a_version_of_another_note_or_none(client):
    from silica.kernel.write.checkpoints import get_checkpoint_store

    tc, vault = client
    (vault / "Corso/B.md").write_text("# B\n", encoding="utf-8")
    get_checkpoint_store().push("Corso/B.md", "# B\n", "# B\n\nmore\n")
    b_floor = get_checkpoint_store().history("Corso/B.md")[-1]["id"]
    assert tc.get("/note/history/diff", params={"path": "Corso/A.md", "id": b_floor}).status_code == 404
    assert tc.get("/note/history/diff", params={"path": "Corso/A.md", "id": "x"}).status_code == 404
    assert tc.get("/note/history", params={"path": "Corso/None.md"}).status_code == 404


def test_a_run_before_image_is_a_version(client):
    from silica.kernel.write.ops import InverseOp, InverseOpKind
    from silica.kernel.write.undo_journal import get_undo_journal

    tc, vault = client
    journal = get_undo_journal()
    run = journal.start_run(source="lezione.pdf", vault=CONFIG.vault_path)
    journal.record(run, InverseOp(kind=InverseOpKind.restore_version, path="Corso/A.md",
                                  prior_content="# A\n\nbefore nucleate\n"), None)
    versions = tc.get("/note/history", params={"path": "Corso/A.md"}).json()["versions"]
    before = [v for v in versions if v["kind"] == "before"]
    assert before and before[0]["source"] == "lezione.pdf"
    diff = tc.get("/note/history/diff", params={"path": "Corso/A.md", "id": before[0]["id"]}).json()
    assert {"op": "+", "text": "before nucleate"} in diff["lines"]
