"""Deleting a note other notes link to is the user's act (ADR-0092 point 2).

The model's `silica_delete` deletes a note nothing else links to and refuses
one that something links to, under any argument: `confirm=True` was a ritual
the model passed on its first call in both recorded deletes. The user deletes
a linked note with `/delete <path>`, journaled under `user:/delete`, and
`/undo` brings it back with its edges.
"""
from __future__ import annotations

import json

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
    notes = {
        "Autoencoders": "# Autoencoders\n\n[[Sparse autoencoders]] and [[Leaf]]\n",
        "Leaf": "# Leaf\n",
        "Linker": "# Linker\n\nsee [[Autoencoders]]\n",
        "Linker2": "# Linker2\n\nsee [[Autoencoders]] and [[Linker]]\n",
        "Lonely": "# Lonely\n\n[[Leaf2]]\n",
        "Leaf2": "# Leaf2\n",
    }
    for name, body in notes.items():
        (v / "Corso" / f"{name}.md").write_text(body, encoding="utf-8")
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


def _index_state():
    from silica.driver import DRIVER
    from silica.driver.fs_backend import ObsidianFSBackend

    live = DRIVER
    fresh = ObsidianFSBackend(vault_path=CONFIG.vault_path)
    fresh._ensure_index()
    return ((set(live._graph.edges()), set(live._unresolved_links)),
            (set(fresh._graph.edges()), set(fresh._unresolved_links)))


# -- the model's tool ---------------------------------------------------------

def test_a_linked_note_is_not_deleted_and_the_refusal_names_its_referrers(vault):
    from silica.kernel.write.undo_journal import get_undo_journal
    from silica.tools.wrapped import silica_delete

    res = silica_delete("Autoencoders")
    assert res["error"] == "not deleted: 2 notes link to Autoencoders"
    assert res["linked_from"] == ["Corso/Linker.md", "Corso/Linker2.md"]
    assert res["path"] == "Corso/Autoencoders.md"
    assert "/delete Corso/Autoencoders.md" in res["next"]
    assert (vault / "Corso" / "Autoencoders.md").exists()
    assert get_undo_journal().last_delete(CONFIG.vault_path) is None


@pytest.mark.parametrize("extra", [
    {},
    {"confirm": True},
    {"leave_unresolved": ["Corso/Linker.md", "Corso/Linker2.md"]},
], ids=["none", "confirm", "leave_unresolved"])
def test_no_argument_lets_the_model_delete_a_linked_note(vault, extra):
    from silica.tools import TOOLS

    res = json.loads(TOOLS["silica_delete"].run(ref="Autoencoders", **extra))
    assert res["error"].startswith("not deleted: 2 notes link to")
    assert (vault / "Corso" / "Autoencoders.md").exists()


def test_the_tool_no_longer_offers_confirm(vault):
    from silica.tools import TOOLS

    params = TOOLS["silica_delete"].json_schema()["function"]["parameters"]
    assert set(params["properties"]) == {"ref"}


def test_an_unlinked_note_is_deleted_and_the_result_names_what_it_orphaned(vault):
    from silica.kernel.write.undo_journal import get_undo_journal
    from silica.tools.wrapped import silica_delete

    res = silica_delete("Lonely")
    assert res["success"] is True
    assert res["deleted"] == "Corso/Lonely.md"
    assert res["orphaned"] == ["Corso/Leaf2.md"]
    assert res["undo"] == "/undo Corso/Lonely.md"
    assert not (vault / "Corso" / "Lonely.md").exists()
    assert get_undo_journal().last_chat_delete(CONFIG.vault_path)[1] == "Corso/Lonely.md"


# -- the user's command -------------------------------------------------------

def test_slash_delete_deletes_a_linked_note_and_says_what_it_left(vault, out):
    from silica.kernel.write.undo_journal import USER_DELETE_SOURCE, get_undo_journal

    assert _handle_direct_shortcut("/delete Corso/Autoencoders.md", []) is True
    assert not (vault / "Corso" / "Autoencoders.md").exists()
    text = out()
    assert "Corso/Linker.md" in text and "Corso/Linker2.md" in text  # now point nowhere
    assert "Corso/Leaf.md" in text                                   # left with no link in
    assert "/undo Corso/Autoencoders.md" in text
    journal = get_undo_journal()
    run_id = journal.last_delete(CONFIG.vault_path, "Corso/Autoencoders.md")[0]
    assert journal.run_info(run_id)["source"] == USER_DELETE_SOURCE
    live, rebuilt = _index_state()
    assert live == rebuilt


def test_slash_delete_takes_a_bare_name(vault, out):
    assert _handle_direct_shortcut("/delete Autoencoders", []) is True
    assert not (vault / "Corso" / "Autoencoders.md").exists()


def test_slash_delete_of_a_missing_note_deletes_nothing(vault, out):
    from silica.kernel.write.undo_journal import get_undo_journal

    assert _handle_direct_shortcut("/delete Corso/Nothing here.md", []) is True
    assert "not deleted" in out().lower()
    assert get_undo_journal().last_delete(CONFIG.vault_path) is None


def test_bare_slash_delete_asks_for_a_path(vault, out):
    assert _handle_direct_shortcut("/delete", []) is True
    assert "/delete <path>" in out()


def test_undo_brings_back_a_slash_delete_with_its_edges(vault, out):
    before = (vault / "Corso" / "Autoencoders.md").read_text(encoding="utf-8")
    _handle_direct_shortcut("/delete Corso/Autoencoders.md", [])

    assert _handle_direct_shortcut("/undo Corso/Autoencoders.md", []) is True
    assert (vault / "Corso" / "Autoencoders.md").read_text(encoding="utf-8") == before
    assert "you deleted it" in out()
    live, rebuilt = _index_state()
    assert live == rebuilt
    assert ("Corso/Linker.md", "Corso/Autoencoders.md") in live[0]


def test_bare_undo_takes_back_a_slash_delete_when_it_is_the_newest_action(vault, out):
    from silica.tools.notes import silica_patch_note

    silica_patch_note("Leaf2", "Extra", "more text", "chat")   # an older write
    _handle_direct_shortcut("/delete Corso/Autoencoders.md", [])

    assert _handle_direct_shortcut("/undo", []) is True
    assert (vault / "Corso" / "Autoencoders.md").exists()
    assert "more text" in (vault / "Corso" / "Leaf2.md").read_text(encoding="utf-8")
