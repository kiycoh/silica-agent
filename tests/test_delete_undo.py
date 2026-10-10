"""A note deleted from the chat comes back with /undo (and /revert).

Before 2026-09-28 silica_delete took no restore point: /undo reads the
checkpoint store, which the delete never wrote, and restored through
DRIVER.overwrite, which refuses a missing file. The prompt-rules smoke run had
the model delete and rewrite a note on "Sistema la nota sugli autoencoder",
with a `confirm=true` it set itself.
"""
from __future__ import annotations

import pytest

from silica.config import CONFIG
from silica.cli import _handle_direct_shortcut


@pytest.fixture
def vault(tmp_path, monkeypatch):
    import silica.driver
    import silica.kernel.write.checkpoints as cp
    import silica.kernel.write.undo_journal as uj

    v = tmp_path / "v"
    (v / "Corso").mkdir(parents=True)
    (v / "vault.yaml").write_text("write_dir: ''\n", encoding="utf-8")
    (v / "Corso" / "Autoencoders.md").write_text("# Autoencoders\n\n[[Sparse autoencoders]]\n", encoding="utf-8")
    (v / "Corso" / "Other.md").write_text("# Other\n\nbody\n", encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(v))
    monkeypatch.setattr(silica.driver, "_driver", None)
    monkeypatch.setattr(cp, "_store", cp.CheckpointStore(tmp_path / "cp.db"))
    monkeypatch.setattr(uj, "_store", uj.UndoJournalStore(tmp_path / "uj.db"))
    return v


def _delete(ref):
    from silica.tools.wrapped import silica_delete
    return silica_delete(ref, confirm=True)


def test_undo_with_the_path_recreates_a_deleted_note(vault):
    before = (vault / "Corso" / "Autoencoders.md").read_text(encoding="utf-8")
    res = _delete("Autoencoders")
    assert res.get("success"), res
    assert not (vault / "Corso" / "Autoencoders.md").exists()

    assert _handle_direct_shortcut("/undo Corso/Autoencoders.md", []) is True
    assert (vault / "Corso" / "Autoencoders.md").read_text(encoding="utf-8") == before


def test_bare_undo_takes_back_the_newest_action_even_when_it_is_a_delete(vault):
    from silica.tools.notes import silica_patch_note

    silica_patch_note("Other", "Extra", "more text", "chat")  # an older write
    _delete("Autoencoders")                                   # the newest action

    assert _handle_direct_shortcut("/undo", []) is True
    assert (vault / "Corso" / "Autoencoders.md").exists()
    assert "more text" in (vault / "Corso" / "Other.md").read_text(encoding="utf-8")


def test_revert_also_recreates_it(vault):
    _delete("Autoencoders")
    assert _handle_direct_shortcut("/revert", []) is True
    assert (vault / "Corso" / "Autoencoders.md").exists()


def test_a_revert_never_overwrites_a_note_made_since_at_that_path(vault):
    from silica.kernel.write.undo_journal import get_undo_journal, revert_run

    _delete("Autoencoders")
    (vault / "Corso" / "Autoencoders.md").write_text("# Autoencoders\n\nwritten again by hand\n", encoding="utf-8")
    run_id = get_undo_journal().last_chat_delete(CONFIG.vault_path)[0]
    res = revert_run(run_id)
    assert res["skipped"] and not res["reverted"]
    assert "written again by hand" in (vault / "Corso" / "Autoencoders.md").read_text(encoding="utf-8")


def test_a_note_that_cannot_be_read_is_not_deleted_and_leaves_no_restore_point(vault):
    from silica.kernel.write.undo_journal import get_undo_journal

    res = _delete("Missing note")
    assert "error" in res
    assert get_undo_journal().last_chat_delete(CONFIG.vault_path) is None
