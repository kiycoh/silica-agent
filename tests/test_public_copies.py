"""What Pages, the app and silica-hosted must say alike and cannot import from one another.

The app and a published page share two files outright (pages/body.css, pages/cover.js); what
is below is what stays a copy: a string in two languages, an address that carries a hash by
hand, a file name another repository reads. A compare is the honest thing, as in
test_design_copies, and each one says why there is no one place to put it.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "silica" / "ui" / "web" / "static"
APP_JS = (STATIC / "app-public.js").read_text(encoding="utf-8")
PAGES_JS = (STATIC / "pages" / "pages.js").read_text(encoding="utf-8")
BODY = (STATIC / "pages" / "body.css").read_text(encoding="utf-8")


def _site_source() -> str:
    return (ROOT / "silica" / "kernel" / "public_site.py").read_text(encoding="utf-8")


def test_the_field_refuses_the_handles_the_server_and_the_catalog_refuse():
    """The field checks as typed, with no round trip; a handle it passes and the claim
    refuses is the wait this copy is there to spare."""
    from silica.kernel import publish
    from silica.ui.web import pages_catalog

    copy = re.search(r"const PUB_HANDLE = /(.+)/;", APP_JS).group(1)
    assert copy == publish.HANDLE_RE.pattern
    assert pages_catalog.HANDLE_RE is publish.HANDLE_RE


@pytest.mark.parametrize("words", ["In no area: notes with no link"])
def test_a_rows_words_are_the_views_and_the_page_built_for_a_reader_without_the_script(words):
    """vault.json carries data and no copy, and its bytes are published, so the room's words
    and the page's stay two texts."""
    assert words in APP_JS and words in _site_source()


def test_a_license_nobody_stated_reads_the_same_on_the_page_and_in_the_room():
    sentence = "Not stated by the author"
    assert f'"{sentence}" if m["license"] == "not stated"' in _site_source()
    assert f'"not stated" ? "{sentence}"' in PAGES_JS


def test_the_files_the_host_passes_keep_the_names_its_repository_reads():
    """silica-hosted bin/pages names these and nothing in this repository can see it."""
    from silica.kernel import publish

    assert (publish.HANDLE_FILE, publish.WIKIS_FILE, publish.CLAIM_FILE) == (
        "handle.json", "wikis.json", "handle-request.json")


def _version(text: str, name: str) -> str:
    return re.search(re.escape(name) + r"\?v=([0-9a-f]{8})", text).group(1)


def test_a_shared_files_address_changes_with_the_file():
    """index() versions what index.html names and sees neither an import nor a script added
    from a script, so these two carry their hash by hand and a browser cannot keep a stale one."""
    def digest(name: str) -> str:
        return hashlib.sha256((STATIC / "pages" / name).read_bytes()).hexdigest()[:8]

    assert _version((STATIC / "app-chat.css").read_text(encoding="utf-8"), "/static/pages/body.css") == digest("body.css")
    assert _version(APP_JS, "/static/pages/cover.js") == digest("cover.js")


def test_the_shared_sheet_names_one_list_of_surfaces_and_reads_only_tokens_both_define():
    """A token one sheet lacks makes the declaration invalid, with no error anywhere: `--warn`
    was absent from pages.css until this sheet needed it."""
    assert set(re.findall(r"^:is\([^)]*\)", BODY, re.M)) == {":is(.msg, #peek-body, #note-edit, #nv-edit, .body)"}
    app = "".join(p.read_text(encoding="utf-8") for p in sorted(STATIC.glob("app-*.css")))
    pages = (STATIC / "pages" / "pages.css").read_text(encoding="utf-8")
    for token in set(re.findall(r"var\((--[\w-]+)\)", BODY)):
        assert f"{token}:" in app and f"{token}:" in pages, token


def test_both_surfaces_load_the_shared_sheet_and_the_host_gets_it():
    from silica.kernel import public_site

    assert {"body.css", "cover.js"} <= set(public_site.static_files())
    assert '@import url("body.css");' in (STATIC / "pages" / "pages.css").read_text(encoding="utf-8")
    assert '@import url("/static/pages/body.css' in (STATIC / "app-chat.css").read_text(encoding="utf-8")
