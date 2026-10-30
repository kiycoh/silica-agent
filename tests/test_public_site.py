"""The public page's files (ADR-0143): what goes out, and above all what does not."""
from __future__ import annotations

from pathlib import Path

import pytest

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def marta(tmp_vault, tmp_path, monkeypatch):
    from silica.kernel.vault_manifest import reset_manifest_cache

    monkeypatch.setenv("SILICA_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("SILICA_PUBLIC_HANDLE", "marta")
    monkeypatch.delenv("SILICA_PUBLIC_BASE", raising=False)
    monkeypatch.delenv("SILICA_PUBLIC_REPORT", raising=False)
    reset_manifest_cache()
    v = tmp_vault
    v.note("Memory/Spacing effect.md",
           "---\ntags: [memory, learning]\nsource_file: \"/home/u/Secret textbook.pdf\"\n"
           "zotero_key: ABCD1234\n---\n# Spacing effect\n\n"
           "Reviews spread over days. It follows from the [[Forgetting curve]].\n\n"
           "How I use it: see [[Exam plan June]]. A draft: [[Working memory]].\n\n"
           "![[hypnogram.png]]\n")
    v.note("Memory/Forgetting curve.md", "Ebbinghaus measured it. Back to [[Spacing effect]].\n")
    v.note("Memory/Working memory.md", "---\npublish: false\n---\nBaddeley draft, not reviewed.\n")
    v.note("Memory/Borrowed.md",
           "---\nauthor: \"@luca\"\nsource: https://silica-agent.dev/@luca/borrowed\nsaved: 2026-10-30\n"
           "form: clip\npublish: true\n---\nLuca's text.\n")
    v.note("Courses/Exam plan June.md", "Physiology on 12 June. Thesis Rombo deadline.\n")
    v.note("Courses/Diary.md", "Only mine: therapy on Tuesdays.\n![[private-scan.png]]\n")
    v.note("sources/Spacing effect.md", "the verbatim book chapter\n")
    v.note("Inbox/lecture.md", "converted lecture\n")
    (Path(tmp_path) / "vault" / "hypnogram.png").write_bytes(PNG)
    (Path(tmp_path) / "vault" / "private-scan.png").write_bytes(PNG + b"x")
    from silica.kernel.publish import set_folder

    set_folder("Memory", True)
    yield Path(tmp_path)
    reset_manifest_cache()


def _all_text(live: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in live.rglob("*")
                     if p.is_file() and p.suffix in (".html", ".md", ".css", ".json"))


def test_the_page_carries_the_public_notes_and_nothing_private(marta):
    from silica.ui.web.public_site import build

    out = build()
    live = marta / "public" / "marta" / "live"
    names = sorted(p.name for p in live.iterdir())
    assert {"index.html", "spacing-effect.html", "spacing-effect.md", "forgetting-curve.html",
            "borrowed.html", "assets"} <= set(names)
    assert "working-memory.html" not in names and "diary.html" not in names
    assert out["notes"] == 3
    text = _all_text(live)
    for leaked in ("therapy on Tuesdays", "Thesis Rombo", "verbatim book", "converted lecture",
                   "Baddeley draft", "Courses", "Memory/", "Secret textbook", "ABCD1234",
                   "zotero_key", "source_file", "data-path", "/asset?path", "broken"):
        assert leaked not in text, leaked


def test_a_link_to_a_private_note_is_its_words_and_a_public_one_a_link(marta):
    from silica.ui.web.public_site import build

    build()
    page = (marta / "public" / "marta" / "live" / "spacing-effect.html").read_text(encoding="utf-8")
    assert 'href="forgetting-curve.html">Forgetting curve</a>' in page
    assert "see Exam plan June." in page and "exam-plan-june" not in page
    assert "A draft: Working memory." in page
    # the title is the page's heading once, not twice
    assert page.count(">Spacing effect</h1>") == 1 and "<h1>Spacing effect</h1>" in page
    md = (marta / "public" / "marta" / "live" / "spacing-effect.md").read_text(encoding="utf-8")
    assert "[Forgetting curve](forgetting-curve.html)" in md
    assert "see Exam plan June." in md and "[[" not in md
    assert md.startswith("---\ntitle: Spacing effect\ntags:")


def test_only_images_a_public_note_cites_are_copied(marta):
    from silica.ui.web.public_site import build

    out = build()
    assets = sorted(p.name for p in (marta / "public" / "marta" / "live" / "assets").iterdir())
    assert [a for a in assets if a.endswith(".png")] == ["1-hypnogram.png"]
    assert out["images"] == 1
    page = (marta / "public" / "marta" / "live" / "spacing-effect.html").read_text(encoding="utf-8")
    assert 'src="assets/1-hypnogram.png"' in page


def test_a_republished_copy_credits_its_author_and_points_at_the_original(marta, monkeypatch):
    from silica.ui.web.public_site import build

    monkeypatch.setenv("SILICA_PUBLIC_BASE", "https://silica-agent.dev")
    build()
    live = marta / "public" / "marta" / "live"
    page = (live / "borrowed.html").read_text(encoding="utf-8")
    assert "Saved from <b>@luca</b>" in page
    assert '<link rel="canonical" href="https://silica-agent.dev/@luca/borrowed">' in page
    assert "save=https%3A%2F%2Fsilica-agent.dev%2F%40luca%2Fborrowed" in page
    index = (live / "index.html").read_text(encoding="utf-8")
    assert "Saved from others" in index and "from @luca" in index


def test_with_an_address_links_are_absolute_and_each_page_can_be_saved(marta, monkeypatch):
    from silica.ui.web.public_site import build

    monkeypatch.setenv("SILICA_PUBLIC_BASE", "https://silica-agent.dev/")
    monkeypatch.setenv("SILICA_PUBLIC_REPORT", "reports@silica-agent.dev")
    build()
    page = (marta / "public" / "marta" / "live" / "spacing-effect.html").read_text(encoding="utf-8")
    assert '<link rel="canonical" href="https://silica-agent.dev/@marta/spacing-effect">' in page
    assert 'href="/@marta/forgetting-curve"' in page
    assert "https://app.silica-agent.dev/?save=https%3A%2F%2Fsilica-agent.dev%2F%40marta%2Fspacing-effect" in page
    assert "mailto:reports@silica-agent.dev" in page
    md = (marta / "public" / "marta" / "live" / "spacing-effect.md").read_text(encoding="utf-8")
    assert "[Forgetting curve](https://silica-agent.dev/@marta/forgetting-curve)" in md


def test_without_an_address_there_is_no_save_button(marta):
    from silica.ui.web.public_site import build

    build()
    page = (marta / "public" / "marta" / "live" / "spacing-effect.html").read_text(encoding="utf-8")
    assert "Save to Silica" not in page and "canonical" not in page


def test_a_new_publication_replaces_the_old_and_the_paused_one(marta):
    from silica.kernel.publish import set_note, set_page
    from silica.ui.web.public_site import build

    build()
    live = marta / "public" / "marta" / "live"
    set_page(False)
    set_note("Memory/Forgetting curve.md", False)
    set_page(True)
    build()
    assert not (live / "forgetting-curve.html").exists()
    assert not (marta / "public" / "marta" / "paused").exists()
    assert not list((marta / "public" / "marta").glob(".build-*"))


def test_the_page_is_readable_by_the_web_server_whatever_the_umask(marta):
    import os
    import stat

    from silica.ui.web.public_site import build

    old = os.umask(0o077)  # the hosted unit's
    try:
        build()
    finally:
        os.umask(old)
    root = marta / "public" / "marta"
    for p in [root, *(root / "live").rglob("*"), root / "live"]:
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode & 0o004, (p, oct(mode))
