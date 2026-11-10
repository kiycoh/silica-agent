"""A Page's files (ADR-0143, ADR-0151): what goes out, and above all what does not."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def marta(tmp_vault, tmp_path, monkeypatch):
    """An account on the hosted service with one wiki: only the hosted service builds pages, since a
    computer sends its notes to its account and writes nothing under live/ (ADR-0157 point 10)."""
    from silica.kernel.base.manifest import reset_manifest_cache

    monkeypatch.setenv("SILICA_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("SILICA_PUBLIC_HANDLE", "marta")
    _on_host(monkeypatch)
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
    from silica.kernel.rights import save_answer

    add_vault("Memory")
    update_vault("memory", {"title": "Memoria", "line": "How remembering works."})
    set_visibility("memory", "public")
    save_answer("folder:Memory", "mine")
    yield Path(tmp_path)
    reset_manifest_cache()


def _on_host(monkeypatch) -> None:
    """The Page (`/@handle/`, its listing and `_s/`) is written on the hosted service alone: a
    computer's live/ is left to the person who may be serving it (ADR-0157, Consequences)."""
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "all")
    monkeypatch.setenv("SILICA_PUBLIC_BASE", "https://pages.test/")


def _all_text(live: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in live.rglob("*")
                     if p.is_file() and p.suffix in (".html", ".md", ".css", ".json"))


def _live(marta: Path) -> Path:
    return marta / "public" / "marta" / "live"


def test_the_vault_carries_its_notes_and_nothing_private(marta, monkeypatch):
    from silica.kernel.public_site import build

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

    from silica.kernel.public_site import build

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
    from silica.kernel.public_site import build

    build("memory")
    page = (_live(marta) / "memory" / "spacing-effect.html").read_text(encoding="utf-8")
    assert 'href="/@marta/memory/forgetting-curve">Forgetting curve</a>' in page
    assert "see Exam plan June." in page and "exam-plan-june" not in page
    assert "A draft: Working memory." in page
    # the title is the page's heading once, not twice
    assert page.count(">Spacing effect</h1>") == 1
    md = (_live(marta) / "memory" / "spacing-effect.md").read_text(encoding="utf-8")
    assert "[Forgetting curve](https://pages.test/@marta/memory/forgetting-curve)" in md
    assert "see Exam plan June." in md and "[[" not in md
    assert md.startswith("---\ntitle: Spacing effect\ntags:")


def test_what_a_page_hides_does_not_ride_out_in_its_md_or_notes_json(marta, tmp_vault):
    from silica.kernel.public_site import build

    tmp_vault.note("Memory/Aside.md", "%% call Marco on 333 1234567 %%\nSeen in class.\n\n"
                                      "<!-- fetch evil.example -->\n\nA `%% code %%` span.\n\n"
                                      "```html\n<!-- kept -->\n%% kept too %%\n```\n\n<!-- never closed\n")
    build("memory")
    live = _live(marta)
    text = _all_text(live)
    assert "Marco" not in text and "evil.example" not in text and "never closed" not in text
    md = (live / "memory" / "aside.md").read_text(encoding="utf-8")
    assert "Seen in class." in md and "`%% code %%`" in md
    assert "<!-- kept -->" in md and "%% kept too %%" in md  # code keeps what it shows


@pytest.mark.parametrize("body, gone, kept", [
    # a comment wrapped around a fence: the fence is the comment's, as an author who comments code out means it
    ("a\n\n%%\nnote\n```\nsecret\n```\nnote\n%%\n\nb\n", ["note", "secret", "%%", "```"], ["a", "b"]),
    ("a\n\n<!--\nnote\n```\nsecret\n```\nnote\n-->\n\nb\n", ["note", "secret", "-->", "```"], ["a", "b"]),
    # never closed, with a fence below it: a page hid all of it before, and must still
    ("a\n\n<!-- open\n\n```\nsecret\n```\n\nsecret too\n", ["open", "secret"], ["a"]),
    ("a\n\n%% open\n\nsecret\n", ["open", "secret"], ["a"]),
    # the first opener wins: a lone %% in an HTML comment is the comment's, and pairs with nothing below
    ("<!-- 5%% off -->\nreal text\n%% remark %%\ntail\n", ["5%%", "remark", "-->"], ["real text", "tail"]),
    ("%% an <!-- in a remark %%\nreal text\n", ["remark", "<!--"], ["real text"]),
    # <!--> and <!---> are whole comments to markdown-it: what follows is the note
    ("a\n\n<!-->\n\nreal text\n", ["<!--"], ["a", "real text"]),
    ("a\n\n<!--->\n\nreal text\n", ["<!--"], ["a", "real text"]),
    # two stray backticks are no code span across paragraphs, so what lies between them is prose
    ("a ` tick\n\n%% secret %%\n\nb ` tick\n", ["secret"], ["a ` tick", "b ` tick"]),
    # code keeps what it shows, wherever markdown-it finds it
    ("a\n\n    <!-- code -->\n    %% code too %%\n\nb\n", [], ["<!-- code -->", "%% code too %%"]),
    ("> ~~~\n> <!-- code -->\n> ~~~\n", [], ["<!-- code -->"]),
    ("1. step\n\n    ~~~\n    %% code %%\n    ~~~\n", [], ["%% code %%"]),
    # a list's continuation looks like an indented block and is prose
    ("- item\n\n    %% secret %%\n\n    b\n", ["secret"], ["b"]),
])
def test_a_published_body_keeps_code_and_loses_every_comment(body, gone, kept):
    from silica.kernel.base.markdown import strip_ofm_meta

    out = strip_ofm_meta(body, published=True)
    assert not [w for w in gone if w in out], out
    assert all(w in out for w in kept), out


def test_the_drawer_keeps_what_only_a_published_body_loses():
    from silica.kernel.base.markdown import strip_ofm_meta

    # markdown-it hides an HTML comment there, and a lone %% in a chat answer is not a remark
    assert strip_ofm_meta("a <!-- b --> c") == "a <!-- b --> c"
    assert strip_ofm_meta("100%% sure\n\nthe rest\n") == "100%% sure\n\nthe rest\n"
    assert strip_ofm_meta("<!-- 5%% -->\nreal\n%% gone %%\n") == "<!-- 5%% -->\nreal\n\n"


def test_a_link_in_a_table_stays_a_link_in_the_md(marta, tmp_vault):
    from silica.kernel.public_site import build

    tmp_vault.note("Memory/Table.md", "| Note | What |\n|---|---|\n| [[Spacing effect\\|the effect]] | x |\n")
    build("memory")
    md = (_live(marta) / "memory" / "table.md").read_text(encoding="utf-8")
    assert "[the effect](" in md and "spacing-effect" in md


def test_a_link_finds_the_note_that_goes_out_when_a_private_one_shares_its_name(marta, tmp_vault):
    from silica.kernel.public_site import build

    # sorted first, so the plain resolver names it, and a note saved from someone else is the usual way here
    tmp_vault.note("Archive/Forgetting curve.md", "not in any wiki\n")
    tmp_vault.note("Memory/Links.md", "See [[Forgetting curve]].\n")
    build("memory")
    md = (_live(marta) / "memory" / "links.md").read_text(encoding="utf-8")
    assert "[Forgetting curve](" in md and "forgetting-curve" in md


def test_only_images_a_published_note_cites_are_copied(marta):
    from silica.kernel.public_site import build

    out = build("memory")
    assets = sorted(p.name for p in (_live(marta) / "memory" / "assets").iterdir())
    assert [a for a in assets if a.endswith(".png")] == ["1-hypnogram.png"]
    assert out["images"] == 1
    page = (_live(marta) / "memory" / "spacing-effect.html").read_text(encoding="utf-8")
    assert 'src="/@marta/memory/assets/1-hypnogram.png"' in page


def test_a_republished_copy_credits_its_author_and_points_at_the_original(marta, monkeypatch):
    from silica.kernel.public_site import build

    monkeypatch.setenv("SILICA_PUBLIC_BASE", "https://silica-agent.dev")
    build("memory")
    page = (_live(marta) / "memory" / "borrowed.html").read_text(encoding="utf-8")
    assert "Saved from <b>@luca</b>" in page
    assert '<link rel="canonical" href="https://silica-agent.dev/@luca/borrowed">' in page
    assert 'name="save" value="https://silica-agent.dev/@luca/borrowed"' in page


def test_with_an_address_links_are_absolute_and_every_page_can_be_saved_and_reported(marta, monkeypatch):
    from silica.kernel.public_site import build

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
    assert '<a href="https://pages.test/">Wikis</a> / <a href="/@marta/">@marta</a> / Memoria' in index
    # the catalog is Wikis wherever a reader meets it, the bar and the crumbs (ADR-0157 point 1)
    for html in (page, index):
        assert '<a href="https://pages.test/">Wikis</a>' in html and not re.search(r"\bPages\b", html)
    md = (vault / "spacing-effect.md").read_text(encoding="utf-8")
    assert "[Forgetting curve](https://pages.test/@marta/memory/forgetting-curve)" in md
    assert not (_live(marta) / "_s").exists()  # the host serves the sheet


def test_a_vault_and_a_note_name_the_room_and_keep_their_page_for_a_reader_without_the_script(marta, monkeypatch):
    from silica.kernel.public_site import build

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


def test_a_page_of_one_public_vault_sends_the_script_to_that_map(marta, monkeypatch):
    from silica.kernel.publish import add_vault, set_visibility
    from silica.kernel.public_site import build

    build("memory")
    page = (_live(marta) / "index.html").read_text(encoding="utf-8")
    assert 'data-one="/@marta/memory/"' in page and "vcard" in page  # the card stays for no script
    other = add_vault("Courses")["slug"]
    set_visibility(other, "public")
    build(other)
    assert "data-one" not in (_live(marta) / "index.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("is_linked", [False, True], ids=["alone", "linked"])
def test_a_computer_builds_no_page_linked_or_not(marta, monkeypatch, is_linked):
    """A computer's wiki goes out as a file, or as notes its account builds (ADR-0157 points 4 and
    10): no build writes its live/, which a person may be serving, even once linked."""
    from silica.kernel import account_link
    from silica.kernel.public_site import build

    monkeypatch.delenv("SILICA_HOSTED")
    if is_linked:
        account_link._keep("t" * 43, "marta", "https://link.test")
    with pytest.raises(ValueError, match="builds the pages" if is_linked else "Link a Silica account"):
        build("memory")
    assert not (marta / "public").exists()


def test_the_page_sweeps_a_served_folder_whose_wiki_is_gone(marta):
    """A wiki removed from the hosted account, or left Off by a crash between two renames, leaves
    its address; the Page's own `_s/` stays."""
    from silica.kernel.public_site import build, write_page

    build("memory")
    live = _live(marta)
    for name in ("removed", "_s"):
        (live / name).mkdir()
    write_page()
    assert not (live / "removed").exists() and (live / "_s").is_dir() and (live / "memory").is_dir()


def test_off_takes_the_vault_and_the_page_down_and_a_republish_replaces_the_paused_copy(marta, monkeypatch):
    from silica.kernel.publish import set_note, set_visibility
    from silica.kernel.public_site import build, write_page

    build("memory")
    live = _live(marta)
    assert (live / "index.html").is_file()
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
    from silica.kernel.public_site import build

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


def test_two_vaults_share_the_page_and_each_lists_the_other(marta, tmp_path, monkeypatch):
    import json

    from silica.kernel.publish import add_vault, set_visibility
    from silica.kernel.public_site import build

    (Path(tmp_path) / "vault" / "Courses").mkdir(exist_ok=True)
    add_vault("Courses")
    set_visibility("courses", "public")
    build("memory")
    build("courses")
    live = _live(marta)
    listing = json.loads((live / "vaults.json").read_text(encoding="utf-8"))
    assert sorted(v["slug"] for v in listing) == ["courses", "memory"]
    index = (live / "index.html").read_text(encoding="utf-8")
    assert "2 wikis ·" in index and "Memoria" in index and "Courses" in index


def test_the_page_is_readable_by_the_web_server_whatever_the_umask(marta, monkeypatch):
    import os
    import stat

    from silica.kernel.public_site import build

    old = os.umask(0o077)  # the hosted unit's
    try:
        build("memory")
    finally:
        os.umask(old)
    root = marta / "public" / "marta"
    for p in [root, *(root / "live").rglob("*"), root / "live"]:
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode & 0o004, (p, oct(mode))


def test_the_catalog_lists_only_what_the_host_approved_under_the_handle_it_gave(marta, monkeypatch, tmp_path):
    import json

    from silica.ui.web import pages_catalog
    from silica.kernel.public_site import build

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
    wikis = tmp_path / "wikis.tsv"
    assert pages_catalog.write(host, tsv, wikis, out, "https://app.test") == 0
    m = host / "t01" / "marta" / "live" / "memory" / "vault.json"
    m.write_text(m.read_text(encoding="utf-8").replace('"listed":false', '"listed":true'), encoding="utf-8")
    # a manifest's `listed` is its author's request (ADR-0153 point 7): alone it lists nothing
    assert pages_catalog.write(host, tsv, wikis, out, "https://app.test") == 0
    wikis.write_text("t01\tmemory\tabc\t2026-11-06\n", encoding="utf-8")
    # an account that wrote a manifest under a handle it does not hold is not listed
    (host / "t02" / "kiycoh" / "live" / "x").mkdir(parents=True)
    (host / "t02" / "kiycoh" / "live" / "x" / "vault.json").write_text(
        json.dumps({"listed": True, "title": "Fake", "handle": "kiycoh"}), encoding="utf-8")
    tsv.write_text("marta\tt01\nkiycoh\tt03\n", encoding="utf-8")
    assert pages_catalog.write(host, tsv, wikis, out, "https://app.test") == 1
    index = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="/@marta/memory/"' in index and "Memoria" in index and "Fake" not in index
    # the catalog is called Wikis, and sends a person to the app's view of that name (ADR-0157 point 1)
    assert "<title>Wikis · Silica</title>" in index and '<h1 id="cat-h">Wikis</h1>' in index
    assert "open Wikis → My wikis" in index and not re.search(r"\bPages\b|My Wikis", index)
    assert (out / "_s" / "pages.js").is_file() and (out / "_s" / "archivo-latin.woff2").is_file()
    # index.json is what index.html shows, as data for the app (ADR-0157 point 2): same wikis,
    # each with its license and address, and the unlisted or unapproved ones absent from both
    data = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert [(e["handle"], e["slug"]) for e in data] == [("marta", "memory")]
    assert data[0]["url"] == "/@marta/memory/" and data[0]["license"] and data[0]["title"] == "Memoria"
    assert "Fake" not in json.dumps(data)
    # a hostile title is text, never markup
    m.write_text(m.read_text(encoding="utf-8").replace('"Memoria"', '"<script>x</script>"'), encoding="utf-8")
    pages_catalog.write(host, tsv, wikis, out, "https://app.test")
    assert "<script>x" not in (out / "index.html").read_text(encoding="utf-8")
    # an approved wiki its author took back asks for nothing, so the host's row alone lists nothing
    m.write_text(m.read_text(encoding="utf-8").replace('"listed":true', '"listed":false'), encoding="utf-8")
    assert pages_catalog.write(host, tsv, wikis, out, "https://app.test") == 0
    assert json.loads((out / "index.json").read_text(encoding="utf-8")) == []


def test_a_diagram_is_printed_as_its_source_and_drawn_by_the_shared_mermaid(marta, tmp_vault, monkeypatch):
    from silica.kernel.public_site import build

    tmp_vault.note("Memory/Forgetting curve.md",
               "Ebbinghaus measured it. Back to [[Spacing effect]].\n\n"
               "```mermaid\nflowchart LR\n  A[\"<b>learn</b>\"] --> B[forget]\n```\n")
    build("memory")
    live = _live(marta)
    page = (live / "memory" / "forgetting-curve.html").read_text(encoding="utf-8")
    # the source, escaped, in the block pages.js hands to mermaid; no script of the note's own
    assert '<pre class="mermaid">' in page and "&lt;b&gt;learn&lt;/b&gt;" in page
    assert "<script>" not in page and page.count("<script") == 1  # pages.js alone
    # the host's one copy under /_s/, where pages.js finds mermaid.min.js beside it
    assert '/_s/pages.js"' in page


# --- the renderer's contract ----------------------------------------------------

def test_a_surface_writes_its_own_links_and_images_and_the_grammar_names_no_route():
    from silica.kernel.text.render import render

    seen: list[str] = []

    def link(path: str, display: str) -> str:
        seen.append(path)
        return f"<i data-to='{path}'>{display}</i>"

    html = render("[[Note|words]] and ![[a.png]] and ![b](c.png) and <img src=\"d.png\">",
                  link=link, asset=lambda t: f"/files/{t}", resolve=lambda ref: ref + ".md", remote_images=False)
    assert seen == ["Note.md"] and "<i data-to='Note.md'>words</i>" in html
    assert 'src="/files/a.png"' in html and 'src="/files/c.png"' in html and 'src="/files/d.png"' in html
    assert "note-link" not in html and "/asset" not in html


def test_an_address_a_surface_gave_is_its_own_where_a_stranger_image_becomes_a_link():
    from silica.kernel.text.render import render

    html = render('![x](y.png)\n\n<img src="z.png">\n\n![t](https://evil.test/p.png)\n\n<img src="https://evil.test/q.png">\n',
                  link=lambda p, d: d, asset=lambda t: f"https://wikis.test/assets/{t}", remote_images=False)
    assert 'src="https://wikis.test/assets/y.png"' in html and 'src="https://wikis.test/assets/z.png"' in html
    assert "<img" in html and "evil.test/p.png\"" not in html.split("<img")[1]
    assert html.count("image from evil.test") == 2


def test_a_stranger_image_leaves_the_notes_after_it_to_the_surface():
    """The first cut of this renderer named the link it wrote for a foreign image `link`, the
    surface's callback, and the next note ref in the paragraph called a Token."""
    from silica.kernel.text.render import render

    html = render("![t](https://evil.test/p.png) then [[Note]]", link=lambda p, d: f"<u>{d}</u>",
                  asset=lambda t: t, resolve=lambda ref: ref, remote_images=False)
    assert "image from evil.test" in html and "<u>Note</u>" in html


def test_an_image_nothing_was_found_for_keeps_its_place_with_no_address():
    from silica.kernel.text.render import render

    assert 'src=""' in render("![[gone.png]]", link=lambda p, d: d, asset=lambda t: "", resolve=lambda r: r)
    assert 'src=""' in render("![x](gone.png)", link=lambda p, d: d, asset=lambda t: "", remote_images=False)
    assert 'src=""' in render('<img src="gone.png">', link=lambda p, d: d, asset=lambda t: "", remote_images=False)


def test_a_notes_links_reach_the_page_however_the_app_writes_its_anchor(marta, monkeypatch):
    """Reordering two attributes in the app's anchor used to publish wikis whose links were dead,
    because the build read them back out of the HTML."""
    import shutil
    import zipfile

    from silica.kernel.public_site import build_file
    from silica.ui.web import render as app

    monkeypatch.setattr(app, "anchor", lambda path, display: f'<a data-path="{path}" class="note-link">{display}</a>')
    # A wiki file, the one build whose links are relative now that a computer builds no page
    # (ADR-0157 point 10): the links are the renderer's either way.
    path, _ = build_file("memory")
    try:
        with zipfile.ZipFile(path) as z:
            page = z.read("memory/spacing-effect.html").decode("utf-8")
    finally:
        shutil.rmtree(path.parent, ignore_errors=True)
    assert '<a class="note-link" href="forgetting-curve.html">Forgetting curve</a>' in page


def test_words_that_spell_the_apps_image_route_are_words_and_copy_nothing(marta, tmp_vault):
    """The build read `/asset?path=` out of the whole page, so a note that wrote it in a link or
    in prose lost it, and the file it named was published with no answer for it (2026-11-07)."""
    from silica.kernel.public_site import build

    tmp_vault.note("Memory/Forgetting curve.md",
                   "Ebbinghaus. [scan](/asset?path=private-scan.png) and /asset?path=private-scan.png in prose.\n")
    build("memory")
    live = _live(marta) / "memory"
    assert not [p for p in (live / "assets").iterdir() if "scan" in p.name]
    assert "in prose" in (live / "forgetting-curve.html").read_text(encoding="utf-8")
    assert "/asset?path=private-scan.png in prose" in (live / "forgetting-curve.html").read_text(encoding="utf-8")
