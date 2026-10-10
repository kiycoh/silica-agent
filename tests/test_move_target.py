"""silica_move keeps a note a note.

prompt-rules probe, 2026-09-28: asked to move Lezione 30 into a folder, the
model passed `to="Corsi/Machine Learning/Archivio/Lezione 30"` and the vault
gained a file with no extension, which Obsidian does not list as a note.
"""
from __future__ import annotations

import pytest

from silica.config import CONFIG


@pytest.fixture
def vault(tmp_path, monkeypatch):
    import silica.driver

    v = tmp_path / "v"
    (v / "Corso").mkdir(parents=True)
    (v / "vault.yaml").write_text("write_dir: ''\n", encoding="utf-8")
    (v / "Corso" / "Lezione 30.md").write_text("# Lezione 30\n\nreti ricorrenti\n", encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(v))
    monkeypatch.setattr(silica.driver, "_driver", None)
    return v


def _move(ref, to):
    from silica.tools.wrapped import silica_move
    return silica_move(ref, to)


def test_a_target_without_extension_stays_a_note(vault):
    res = _move("Lezione 30", "Archivio/Lezione 30")
    assert res.get("success"), res
    assert (vault / "Archivio" / "Lezione 30.md").is_file()
    assert not (vault / "Archivio" / "Lezione 30").exists()


def test_a_folder_target_keeps_the_notes_name(vault):
    res = _move("Lezione 30", "Archivio/")
    assert res.get("success"), res
    assert (vault / "Archivio" / "Lezione 30.md").is_file()


def test_a_full_target_is_used_as_given(vault):
    res = _move("Corso/Lezione 30.md", "Archivio/RNN.md")
    assert res.get("success"), res
    assert (vault / "Archivio" / "RNN.md").is_file()


def test_a_folder_target_without_a_slash_is_a_folder(vault):
    """prompt-rules probe, run C: to="Corsi/Machine Learning/Archivio" (the
    folder the user named) renamed the note to Archivio.md."""
    res = _move("Lezione 30", "Corsi/Machine Learning/Archivio")
    assert res.get("success"), res
    assert (vault / "Corsi" / "Machine Learning" / "Archivio" / "Lezione 30.md").is_file()
    assert not (vault / "Corsi" / "Machine Learning" / "Archivio.md").exists()
