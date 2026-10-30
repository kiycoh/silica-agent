"""The public page's choice (ADR-0143): folders, notes, and what never goes out."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def vault(tmp_vault, tmp_path, monkeypatch):
    from silica.kernel.vault_manifest import reset_manifest_cache

    monkeypatch.delenv("SILICA_PUBLIC_HANDLE", raising=False)
    monkeypatch.setenv("SILICA_PUBLIC_DIR", str(tmp_path / "public"))
    reset_manifest_cache()
    tmp_vault.note("Memory/Spacing effect.md", "# Spacing effect\n\nSee [[Forgetting curve]].\n")
    tmp_vault.note("Memory/Forgetting curve.md", "Ebbinghaus.\n")
    tmp_vault.note("Memory/Working memory.md", "---\ntags: [memory]\n---\nDraft.\n")
    tmp_vault.note("Memory/Clipped.md", "---\nform: clip\nsource: web-fetch\n---\nSomeone else's.\n")
    tmp_vault.note("Courses/Exam plan June.md", "Private plan.\n")
    tmp_vault.note("sources/Spacing effect.md", "the verbatim book\n")
    tmp_vault.note("Inbox/lecture.md", "converted\n")
    tmp_vault.note(".silica-chat/s1/a.md", "attachment\n")
    yield tmp_vault
    reset_manifest_cache()


def _states():
    from silica.kernel.publish import selection

    return selection()["notes"]


def test_nothing_is_public_until_the_person_says_so(vault):
    from silica.kernel.publish import settings

    assert settings() == {"page": False, "folders": []}
    assert {s for s in _states().values()} <= {"off", "never"}


def test_a_folder_globe_takes_its_notes_but_not_a_clip(vault):
    from silica.kernel.publish import set_folder

    assert set_folder("Memory", True) == ["Memory"]
    st = _states()
    assert st["Memory/Spacing effect.md"] == "on"
    assert st["Memory/Working memory.md"] == "on"
    assert st["Memory/Clipped.md"] == "off"
    assert st["Courses/Exam plan June.md"] == "off"


def test_silica_folders_are_never_published_and_refuse_a_globe(vault):
    from silica.kernel.publish import set_folder, set_note

    st = _states()
    # sources/ is outside the notes walk already; whatever reaches it is never
    assert st.get("sources/Spacing effect.md", "never") == "never"
    assert st.get(".silica-chat/s1/a.md", "never") == "never"
    assert st["Inbox/lecture.md"] == "never"
    for folder in ("sources", "Inbox", ".silica-chat"):
        with pytest.raises(ValueError):
            set_folder(folder, True)
    with pytest.raises(ValueError):
        set_note("sources/Spacing effect.md", True)


def test_a_note_globe_beats_its_folder_and_writes_the_least(vault, tmp_path):
    from silica.kernel.publish import set_folder, set_note

    set_folder("Memory", True)
    assert set_note("Memory/Working memory.md", False) == "struck"
    text = (Path(tmp_path) / "vault" / "Memory/Working memory.md").read_text(encoding="utf-8")
    assert "publish: false" in text and "True" not in text and "tags:" in text
    assert _states()["Memory/Working memory.md"] == "struck"

    # back on inside a public folder: the folder decides again, no key left
    assert set_note("Memory/Working memory.md", True) == "on"
    text = (Path(tmp_path) / "vault" / "Memory/Working memory.md").read_text(encoding="utf-8")
    assert "publish" not in text and "tags:" in text

    # a note in a private folder needs its own true, and loses it when off
    assert set_note("Courses/Exam plan June.md", True) == "on"
    exam = (Path(tmp_path) / "vault" / "Courses/Exam plan June.md").read_text(encoding="utf-8")
    assert exam.startswith("---\npublish: true\n---")
    assert set_note("Courses/Exam plan June.md", False) == "off"
    exam = (Path(tmp_path) / "vault" / "Courses/Exam plan June.md").read_text(encoding="utf-8")
    assert exam == "Private plan.\n"

    # a clip is published only by its own globe
    assert set_note("Memory/Clipped.md", True) == "on"
    assert "publish: true" in (Path(tmp_path) / "vault" / "Memory/Clipped.md").read_text(encoding="utf-8")


def test_the_public_block_keeps_the_rest_of_vault_yaml(vault, tmp_path):
    from silica.kernel.publish import set_folder, set_page, settings

    manifest = Path(tmp_path) / "vault" / "vault.yaml"
    manifest.write_text("# my vault\nlanguage: Italian\n", encoding="utf-8")
    set_folder("Memory", True)
    set_folder("Courses", True)
    set_page(True)
    text = manifest.read_text(encoding="utf-8")
    assert text.startswith("# my vault\nlanguage: Italian\n")
    assert settings() == {"page": True, "folders": ["Courses", "Memory"]}
    set_folder("Courses", False)
    assert settings()["folders"] == ["Memory"]


def test_off_moves_the_live_page_aside_and_on_brings_it_back(vault, tmp_path):
    from silica.kernel.publish import handle, set_page, site_root

    root = site_root(handle())
    (root / "live").mkdir(parents=True)
    (root / "live" / "index.html").write_text("page", encoding="utf-8")
    set_page(False)
    assert not (root / "live").exists() and (root / "paused" / "index.html").is_file()
    set_page(True)
    assert (root / "live" / "index.html").read_text(encoding="utf-8") == "page"


def test_the_server_gives_no_page_without_a_handle_from_the_host(vault, monkeypatch):
    from silica.kernel.publish import handle

    monkeypatch.setenv("SILICA_HOSTED", "1")
    assert handle() == ""
    monkeypatch.setenv("SILICA_PUBLIC_HANDLE", "Marta")
    assert handle() == "marta"


def test_a_slug_folds_accents(vault):
    from silica.kernel.publish import slugify

    assert slugify("Perché la memoria è fragile?") == "perche-la-memoria-e-fragile"
