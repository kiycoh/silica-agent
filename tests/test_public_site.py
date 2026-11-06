"""A Page's files (ADR-0143, ADR-0151): what goes out, and above all what does not."""
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
    from silica.kernel.publish import add_vault, set_visibility, update_vault

    add_vault("Memory")
    update_vault("memory", {"title": "Memoria", "line": "How remembering works."})
    set_visibility("memory", "public")
    yield Path(tmp_path)
    reset_manifest_cache()


def _all_text(live: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in live.rglob("*")
                     if p.is_file() and p.suffix in (".html", ".md", ".css", ".json"))


def _live(marta: Path) -> Path:
    return marta / "public" / "marta" / "live"


def test_the_vault_carries_its_notes_and_nothing_private(marta):
    from silica.ui.web.public_site import build

    out = build("memory")
    live = _live(marta)
    vault = live / "memory"
    names = {p.name for p in vault.iterdir()}
    assert {"index.html", "vault.json", "notes.json", "spacing-effect.html", "spacing-effect.md",
            "forgetting-curve.html", "borrowed.html", "assets"} <= names
    assert "working-memory.html" not in names and "diary.html" not in names
    assert (live / "index.html").is_file() and (live / "vaults.json").is_file()
    assert out["notes"] == 3 and not out["private"]
    text = _all_text(live)
    for leaked in ("therapy on Tuesdays", "Thesis Rombo", "verbatim book", "converted lecture",
                   "Baddeley draft", "Courses", "Memory/", "Secret textbook", "ABCD1234",
                   "zotero_key", "source_file", "data-path", "/asset?path", "broken"):
        assert leaked not in text, leaked


def test_the_manifest_carries_the_cover_and_the_areas_of_the_published_notes(marta):
    import json

    from silica.ui.web.public_site import build

    build("memory")
    m = json.loads((_live(marta) / "memory" / "vault.json").read_text(encoding="utf-8"))
    assert (m["handle"], m["slug"], m["title"], m["line"], m["license"]) == (
        "marta", "memory", "Memoria", "How remembering works.", "not stated")
    assert m["count"] == {"notes": 3, "links": 1, "drawn": 2, "areas": 1}
    assert sorted(n[4] for n in m["n"]) == ["Forgetting curve", "Spacing effect"]
    assert all(0 <= n[0] <= 1 and 0 <= n[1] <= 1 for n in m["n"])
    assert m["areas"][0][1] == 2 and m["areas"][0][0] in ("Forgetting curve", "Spacing effect")
    assert m["unlinked"] == [["Borrowed", "borrowed"]]
    assert m["listed"] is False
    notes = json.loads((_live(marta) / "memory" / "notes.json").read_text(encoding="utf-8"))
    assert set(notes) == {"spacing-effect", "forgetting-curve", "borrowed"}
    assert "vault: Memoria" in notes["spacing-effect"]


def test_a_link_to_a_private_note_is_its_words_and_a_vault_note_a_link(marta):
    from silica.ui.web.public_site import build

    build("memory")
    page = (_live(marta) / "memory" / "spacing-effect.html").read_text(encoding="utf-8")
    assert 'href="forgetting-curve.html">Forgetting curve</a>' in page
    assert "see Exam plan June." in page and "exam-plan-june" not in page
    assert "A draft: Working memory." in page
    # the title is the page's heading once, not twice
    assert page.count(">Spacing effect</h1>") == 1
    assert 'href="../_s/pages.css"' in page and 'src="../_s/pages.js"' in page
    md = (_live(marta) / "memory" / "spacing-effect.md").read_text(encoding="utf-8")
    assert "[Forgetting curve](forgetting-curve.html)" in md
    assert "see Exam plan June." in md and "[[" not in md
    assert md.startswith("---\ntitle: Spacing effect\ntags:")


def test_only_images_a_published_note_cites_are_copied(marta):
    from silica.ui.web.public_site import build

    out = build("memory")
    assets = sorted(p.name for p in (_live(marta) / "memory" / "assets").iterdir())
    assert [a for a in assets if a.endswith(".png")] == ["1-hypnogram.png"]
    assert out["images"] == 1
    page = (_live(marta) / "memory" / "spacing-effect.html").read_text(encoding="utf-8")
    assert 'src="assets/1-hypnogram.png"' in page


def test_a_republished_copy_credits_its_author_and_points_at_the_original(marta, monkeypatch):
    from silica.ui.web.public_site import build

    monkeypatch.setenv("SILICA_PUBLIC_BASE", "https://silica-agent.dev")
    build("memory")
    page = (_live(marta) / "memory" / "borrowed.html").read_text(encoding="utf-8")
    assert "Saved from <b>@luca</b>" in page
    assert '<link rel="canonical" href="https://silica-agent.dev/@luca/borrowed">' in page
    assert 'name="save" value="https://silica-agent.dev/@luca/borrowed"' in page


def test_with_an_address_links_are_absolute_and_every_page_can_be_saved_and_reported(marta, monkeypatch):
    from silica.ui.web.public_site import build

    monkeypatch.setenv("SILICA_PUBLIC_BASE", "https://pages.test/")
    build("memory")
    vault = _live(marta) / "memory"
    page = (vault / "spacing-effect.html").read_text(encoding="utf-8")
    assert '<link rel="canonical" href="https://pages.test/@marta/memory/spacing-effect">' in page
    assert 'href="/@marta/memory/forgetting-curve"' in page
    assert 'href="/_s/pages.css"' in page and 'src="/_s/pages.js"' in page
    assert 'action="https://app.silica-agent.dev/"' in page and 'formaction="http://localhost:8765/"' in page
    assert 'name="save" value="https://pages.test/@marta/memory/spacing-effect"' in page
    assert 'action="/_report"' in page
    index = (vault / "index.html").read_text(encoding="utf-8")
    assert 'name="save" value="https://pages.test/@marta/memory/"' in index
    assert "Save 3 notes to Silica" in index and 'form="save"' in index and 'action="/_report"' in index
    assert '<a href="https://pages.test/">Pages</a> / <a href="/@marta/">@marta</a> / Memoria' in index
    md = (vault / "spacing-effect.md").read_text(encoding="utf-8")
    assert "[Forgetting curve](https://pages.test/@marta/memory/forgetting-curve)" in md
    assert not (_live(marta) / "_s").exists()  # the host serves the sheet


def test_a_vault_and_a_note_name_the_room_and_keep_their_page_for_a_reader_without_the_script(marta, monkeypatch):
    from silica.ui.web.public_site import build

    monkeypatch.setenv("SILICA_PUBLIC_BASE", "https://pages.test/")
    build("memory")
    vault = _live(marta) / "memory"
    index = (vault / "index.html").read_text(encoding="utf-8")
    room = 'data-room="/@marta/memory/vault.json" data-here="/@marta/memory/" data-vaults="/@marta/vaults.json"'
    assert f'<main class="sheet" {room}>' in index
    # all of it still there with no script: the picture, Save by areas, and every note
    assert "<svg" in index and 'name="areas"' in index and 'href="/@marta/memory/spacing-effect"' in index
    note = (vault / "spacing-effect.html").read_text(encoding="utf-8")
    assert f'<main class="sheet" {room} data-note="spacing-effect">' in note
    assert 'class="body"' in note and "Linked from" in note
    assert "data-other" not in index + note  # another address is the room's to ask, behind a link


def test_a_page_built_without_a_host_tells_the_room_its_files_end_in_html(marta):
    from silica.ui.web.public_site import build

    build("memory")
    index = (_live(marta) / "memory" / "index.html").read_text(encoding="utf-8")
    assert 'data-room="vault.json" data-here="" data-vaults="../vaults.json" data-ext=".html"' in index


def test_a_page_of_one_public_vault_sends_the_script_to_that_map(marta, monkeypatch):
    from silica.kernel.publish import add_vault, set_visibility
    from silica.ui.web.public_site import build

    monkeypatch.setenv("SILICA_PUBLIC_BASE", "https://pages.test/")
    build("memory")
    page = (_live(marta) / "index.html").read_text(encoding="utf-8")
    assert 'data-one="/@marta/memory/"' in page and "vcard" in page  # the card stays for no script
    other = add_vault("Courses")["slug"]
    set_visibility(other, "public")
    build(other)
    assert "data-one" not in (_live(marta) / "index.html").read_text(encoding="utf-8")


def test_without_an_address_there_is_no_save_and_the_folder_opens_from_disk(marta):
    from silica.ui.web.public_site import build

    build("memory")
    live = _live(marta)
    page = (live / "memory" / "spacing-effect.html").read_text(encoding="utf-8")
    assert "Save to Silica" not in page and "canonical" not in page and "_report" not in page
    assert (live / "_s" / "pages.css").is_file() and (live / "_s" / "pages.js").is_file()
    index = (live / "index.html").read_text(encoding="utf-8")
    assert 'href="memory/index.html"' in index and 'data-graph="memory/vault.json"' in index


def test_off_takes_the_vault_and_the_page_down_and_a_republish_replaces_the_paused_copy(marta):
    from silica.kernel.publish import set_note, set_visibility
    from silica.ui.web.public_site import build, write_page

    build("memory")
    live = _live(marta)
    set_visibility("memory", "off")
    write_page()
    assert not (live / "memory").exists() and not (live / "index.html").exists()
    set_note("Memory/Forgetting curve.md", False)
    set_visibility("memory", "public")
    build("memory")
    assert not (live / "memory" / "forgetting-curve.html").exists()
    assert not (marta / "public" / "marta" / "paused" / "memory").exists()
    assert not list((marta / "public" / "marta").glob(".build-*"))


def test_a_private_vault_is_built_beside_the_served_folder_with_its_readers(marta, monkeypatch):
    import json

    from silica.kernel.publish import set_visibility, update_vault
    from silica.ui.web.public_site import build

    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "all")
    monkeypatch.setenv("SILICA_PUBLIC_BASE", "https://pages.test")
    build("memory")
    set_visibility("memory", "private")
    update_vault("memory", {"readers": ["sara"]})
    out = build("memory")
    root = marta / "public" / "marta"
    assert out["private"] and not (root / "live" / "memory").exists()
    priv = root / "private" / "memory"
    assert json.loads((priv / "readers.json").read_text(encoding="utf-8")) == ["sara"]
    assert {p.name for p in priv.iterdir()} == {"vault.json", "notes.json", "readers.json", "assets"}
    assert json.loads((priv / "vault.json").read_text(encoding="utf-8"))["listed"] is False
    notes = json.loads((priv / "notes.json").read_text(encoding="utf-8"))
    assert "![hypnogram.png](assets/1-hypnogram.png)" in notes["spacing-effect"]


def test_two_vaults_share_the_page_and_each_lists_the_other(marta, tmp_path):
    import json

    from silica.kernel.publish import add_vault, set_visibility
    from silica.ui.web.public_site import build

    (Path(tmp_path) / "vault" / "Courses").mkdir(exist_ok=True)
    add_vault("Courses")
    set_visibility("courses", "public")
    build("memory")
    build("courses")
    live = _live(marta)
    listing = json.loads((live / "vaults.json").read_text(encoding="utf-8"))
    assert sorted(v["slug"] for v in listing) == ["courses", "memory"]
    index = (live / "index.html").read_text(encoding="utf-8")
    assert "2 vaults" in index and "Memoria" in index and "Courses" in index


def test_the_page_is_readable_by_the_web_server_whatever_the_umask(marta):
    import os
    import stat

    from silica.ui.web.public_site import build

    old = os.umask(0o077)  # the hosted unit's
    try:
        build("memory")
    finally:
        os.umask(old)
    root = marta / "public" / "marta"
    for p in [root, *(root / "live").rglob("*"), root / "live"]:
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode & 0o004, (p, oct(mode))


def test_the_catalog_lists_only_listed_vaults_under_the_handle_the_host_gave(marta, monkeypatch, tmp_path):
    import json

    from silica.ui.web import pages_catalog
    from silica.ui.web.public_site import build

    monkeypatch.setenv("SILICA_PUBLIC_BASE", "https://pages.test")
    build("memory")
    public = marta / "public"
    # the host's layout: <public>/<account>/<handle>/live
    host = tmp_path / "srv"
    (host / "t01").mkdir(parents=True)
    (public / "marta").rename(host / "t01" / "marta")
    tsv = tmp_path / "handles.tsv"
    tsv.write_text("marta\tt01\n", encoding="utf-8")
    out = tmp_path / "pages"
    assert pages_catalog.write(host, tsv, out, "https://app.test") == 0
    m = host / "t01" / "marta" / "live" / "memory" / "vault.json"
    m.write_text(m.read_text(encoding="utf-8").replace('"listed":false', '"listed":true'), encoding="utf-8")
    # an account that wrote a manifest under a handle it does not hold is not listed
    (host / "t02" / "kiycoh" / "live" / "x").mkdir(parents=True)
    (host / "t02" / "kiycoh" / "live" / "x" / "vault.json").write_text(
        json.dumps({"listed": True, "title": "Fake", "handle": "kiycoh"}), encoding="utf-8")
    tsv.write_text("marta\tt01\nkiycoh\tt03\n", encoding="utf-8")
    assert pages_catalog.write(host, tsv, out, "https://app.test") == 1
    index = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="/@marta/memory/"' in index and "Memoria" in index and "Fake" not in index
    assert (out / "_s" / "pages.js").is_file() and (out / "_s" / "archivo-latin.woff2").is_file()
    # a hostile title is text, never markup
    m.write_text(m.read_text(encoding="utf-8").replace('"Memoria"', '"<script>x</script>"'), encoding="utf-8")
    pages_catalog.write(host, tsv, out, "https://app.test")
    assert "<script>x" not in (out / "index.html").read_text(encoding="utf-8")


def test_a_diagram_is_printed_as_its_source_and_drawn_by_the_shared_mermaid(marta, tmp_vault):
    from silica.ui.web.public_site import build

    tmp_vault.note("Memory/Forgetting curve.md",
               "Ebbinghaus measured it. Back to [[Spacing effect]].\n\n"
               "```mermaid\nflowchart LR\n  A[\"<b>learn</b>\"] --> B[forget]\n```\n")
    build("memory")
    live = _live(marta)
    page = (live / "memory" / "forgetting-curve.html").read_text(encoding="utf-8")
    # the source, escaped, in the block pages.js hands to mermaid; no script of the note's own
    assert '<pre class="mermaid">' in page and "&lt;b&gt;learn&lt;/b&gt;" in page
    assert "<script>" not in page and page.count("<script") == 1  # pages.js alone
    assert (live / "_s" / "mermaid.min.js").is_file()
