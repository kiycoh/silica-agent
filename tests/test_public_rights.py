"""Before you publish (ADR-0153 points 20 and 26): one question, whose is it, for each file,
folder of images and site. The build enforces the answers; the screen only reads them."""
from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import httpx
import pytest

from silica.sources.web_fetch import TooLarge, WrongType

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
BOOK = ("the probability of an event is a number between zero and one that measures how likely "
        "the event is to occur in a long run of independent trials of the same experiment")
ROSS = "Ross (Z-Library).pdf"


@pytest.fixture
def stats(tmp_vault, tmp_path, monkeypatch, linked):
    """A wiki of a book in two segments, lecture slides, hand notes and pasted images."""
    from silica.kernel.publish import add_vault, set_visibility
    from silica.kernel.vault_manifest import reset_manifest_cache
    from silica.kernel.write.provenance import append_record

    monkeypatch.setenv("SILICA_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("SILICA_PUBLIC_HANDLE", "marta")
    monkeypatch.delenv("SILICA_PUBLIC_BASE", raising=False)
    monkeypatch.delenv("SILICA_PUBLIC_REPORT", raising=False)
    reset_manifest_cache()
    root = Path(tmp_path) / "vault"
    v = tmp_vault
    v.note("sources/Ross-1.md", f'---\nsource_file: "/home/u/{ROSS}"\n---\n{BOOK}\n\n![[Ross-fig1.png]]\n')
    v.note("sources/Ross-2.md", f'---\nsource_file: "/home/u/{ROSS}"\n---\nConditional probability.\n')
    v.note("sources/Slides.md", '---\nsource_file: "/home/u/Slides.pdf"\n---\nRegression fits a line.\n')
    v.note("Stats/Bayes.md", "Bayes turns one conditional into the other. See [[Hand note]].\n\n![[Ross-fig1.png]]\n")
    v.note("Stats/Variance.md", "Spread around the mean, and [[Bayes]].\n")
    v.note("Stats/Regression.md", "A line through the points.\n")
    v.note("Stats/Hand note.md", "Mine. It goes to [[Bayes]] and to [[Nowhere at all]].\n")
    v.note("Stats/Descriptive/Box plot.md", "Quartiles.\n\n![[Pasted image 1.png]]\n\n![[Pasted image 2.png]]\n")
    for name in ("Ross-fig1.png", "Pasted image 1.png", "Pasted image 2.png"):
        (root / name).write_bytes(PNG + name.encode())
    for source, notes in (("Ross-1.md", ["Stats/Bayes"]), ("Ross-2.md", ["Stats/Variance"]),
                          ("Slides.md", ["Stats/Regression", "Stats/Variance"])):
        assert append_record(source, source, "run-" + source, notes, vault_path=str(root)) == "present"
    add_vault("Stats")
    set_visibility("stats", "public")
    yield root
    reset_manifest_cache()


def _live(root: Path) -> Path:
    return root.parent / "public" / "marta" / "live" / "stats"


def _names(root: Path) -> set[str]:
    return {p.name for p in _live(root).glob("*.html")}


def _sheet(**kw) -> dict:
    from silica.kernel import rights

    return rights.sheet(rights.read_wiki("stats", **kw), rights.answers())


def _say(key: str, answer: str, **kw) -> None:
    from silica.kernel import rights

    rights.save_answer(key, answer, **kw)


def _build() -> dict:
    from silica.ui.web.public_site import build

    return build("stats")


def _written(root: Path, *paths: str) -> None:
    """Stamp notes as Silica wrote them, so an untouched one reads untouched."""
    from silica.kernel.write.ops import InverseOp, InverseOpKind
    from silica.kernel.write.undo_journal import _content_hash, get_undo_journal

    store = get_undo_journal()
    run = store.start_run(vault=str(root))
    for p in paths:
        store.record(run, InverseOp(kind=InverseOpKind.delete_created, path=p),
                     _content_hash((root / p).read_text(encoding="utf-8")))


# --- files ---------------------------------------------------------------------

def test_a_row_is_the_real_file_and_not_the_segments_the_ledger_names(stats):
    files = {f["name"]: f for f in _sheet()["files"]}
    assert set(files) == {ROSS, "Slides.pdf"}
    assert {n["title"] for n in files[ROSS]["notes"]} == {"Bayes", "Variance"}
    assert {n["title"] for n in files["Slides.pdf"]["notes"]} == {"Regression", "Variance"}


def test_a_file_nobody_answered_for_keeps_its_notes_out_and_a_note_of_no_source_goes(stats):
    out = _build()
    assert _names(stats) >= {"hand-note.html", "box-plot.html"}
    assert not _names(stats) & {"bayes.html", "variance.html", "regression.html"}
    assert out["notes"] == 2


def test_someone_elses_keeps_a_file_out_as_no_answer_does(stats):
    _say(ROSS, "else")
    _build()
    assert "bayes.html" not in _names(stats)


def test_mine_lets_a_file_out_and_a_note_of_two_files_follows_the_stricter_answer(stats):
    _say(ROSS, "mine")
    _build()
    assert "bayes.html" in _names(stats) and "variance.html" not in _names(stats)
    _say("Slides.pdf", "mine")
    _build()
    assert {"variance.html", "regression.html"} <= _names(stats)


@pytest.mark.parametrize("license,credit,goes", [
    ("", "Sheldon Ross", False),
    ("CC BY", "", False),
    ("CC BY", "Sheldon Ross", True),
    ("Public domain or CC0", "", True),
    ("CC BY-ND", "Sheldon Ross", False),
])
def test_open_needs_a_license_a_credit_and_leave_to_make_something_from_it(stats, license, credit, goes):
    _say(ROSS, "open", license=license, credit=credit)
    _build()
    assert ("bayes.html" in _names(stats)) is goes


def test_the_credit_of_an_open_file_reaches_the_page_and_the_saved_copy(stats):
    _say(ROSS, "open", license="CC BY", credit="Sheldon Ross")
    _build()
    assert "Sheldon Ross" in (_live(stats) / "bayes.html").read_text(encoding="utf-8")
    assert "Sheldon Ross" in (_live(stats) / "bayes.md").read_text(encoding="utf-8")


def test_a_files_name_never_goes_out_and_the_manifest_says_what_did(stats):
    _say(ROSS, "open", license="CC BY", credit="Sheldon Ross")
    _build()
    text = "\n".join(p.read_text(encoding="utf-8") for p in _live(stats).glob("*") if p.is_file())
    assert "Z-Library" not in text and "Ross (" not in text
    rights = json.loads((_live(stats) / "vault.json").read_text(encoding="utf-8"))["rights"]
    assert rights["sources"] == [{"answer": "open", "license": "CC BY", "credit": "Sheldon Ross", "notes": 1}]


def test_an_answer_lives_in_the_manifest_and_a_later_look_asks_only_what_is_new(stats):
    assert _sheet()["ask"] is True
    _say(ROSS, "mine")
    _say("Slides.pdf", "else")
    _say("folder:Stats/Descriptive", "mine")
    assert "sources:" in (stats / "vault.yaml").read_text(encoding="utf-8")
    sheet = _sheet()
    assert sheet["ask"] is False
    assert all(f["old"] for f in sheet["files"]) and all(g["old"] for g in sheet["groups"])


def test_a_wiki_of_hand_notes_goes_straight_to_publish(tmp_vault, stats):
    from silica.kernel import rights
    from silica.kernel.publish import add_vault

    tmp_vault.note("Plain/One.md", "Written by hand.\n")
    add_vault("Plain")
    sheet = rights.sheet(rights.read_wiki("plain"), rights.answers())
    assert sheet["ask"] is False and sheet["plain"] == 1 and not sheet["files"] and not sheet["groups"]


# --- images --------------------------------------------------------------------

def test_an_image_nobody_answered_for_is_not_published(stats):
    _build()
    page = (_live(stats) / "box-plot.html").read_text(encoding="utf-8")
    md = (_live(stats) / "box-plot.md").read_text(encoding="utf-8")
    assert "Image not published" in page and "Image not published" in md
    assert 'src=""' not in page and "Pasted" not in page and "Pasted" not in md
    assert not list((_live(stats) / "assets").iterdir())


def test_a_folder_answer_publishes_its_images_but_the_ones_the_author_unticked(stats):
    _say("folder:Stats/Descriptive", "mine", off=["Pasted image 2.png"])
    out = _build()
    assert [p.name for p in (_live(stats) / "assets").iterdir()] == ["1-pasted-image-1.png"]
    assert out["images"] == 1
    assert "Image not published" in (_live(stats) / "box-plot.md").read_text(encoding="utf-8")


def test_an_image_a_leaf_embeds_follows_its_file_whichever_note_shows_it(stats, tmp_vault):
    tmp_vault.note("Stats/Hand note.md", "Mine, with the book's figure.\n\n![[Ross-fig1.png]]\n")
    sheet = _sheet()
    assert not [g for g in sheet["groups"] if "Ross-fig1.png" in str(g)]
    _build()
    assert "Image not published" in (_live(stats) / "hand-note.md").read_text(encoding="utf-8")
    _say(ROSS, "mine")
    _build()
    assert "Image not published" not in (_live(stats) / "hand-note.md").read_text(encoding="utf-8")


def test_an_image_only_a_staying_note_shows_is_not_asked_about(stats, tmp_vault):
    tmp_vault.note("Stats/Regression.md", "A line.\n\n![[Pasted image 1.png]]\n")
    tmp_vault.note("Stats/Descriptive/Box plot.md", "Quartiles.\n\n![[Pasted image 2.png]]\n")
    images = [i["name"] for g in _sheet()["groups"] for i in g["images"]]
    assert images == ["Pasted image 2.png"]


def _web(tmp_vault) -> None:
    tmp_vault.note("Stats/Web.md", '![Normal](https://upload.wikimedia.org/x/Normal.png)\n\n'
                                   '<img src="https://cdn.test/raw.png">\n')


def _png(url: str) -> dict:
    return {"data": PNG + url.encode(), "type": "image/png", "why": ""}


def test_a_remote_image_with_no_answer_stays_a_link_in_the_page_and_the_saved_copy(stats, tmp_vault):
    _web(tmp_vault)
    _sheet(fetch=_png)
    _build()
    page = (_live(stats) / "web.html").read_text(encoding="utf-8")
    md = (_live(stats) / "web.md").read_text(encoding="utf-8")
    assert '<img src="https://' not in page and "<img" not in md and "![" not in md
    assert 'href="https://upload.wikimedia.org/x/Normal.png"' in page
    assert "https://upload.wikimedia.org/x/Normal.png" in md and "https://cdn.test/raw.png" in md


def test_an_answered_site_is_copied_into_the_wiki_with_its_credit(stats, tmp_vault):
    _web(tmp_vault)
    groups = {g["key"]: g for g in _sheet(fetch=_png)["groups"]}
    assert groups["site:upload.wikimedia.org"]["credit"] == "from upload.wikimedia.org"
    _say("site:upload.wikimedia.org", "mine")
    _build()
    page = (_live(stats) / "web.html").read_text(encoding="utf-8")
    md = (_live(stats) / "web.md").read_text(encoding="utf-8")
    assert len(list((_live(stats) / "assets").iterdir())) == 1
    assert 'src="assets/' in page and "https://upload.wikimedia.org/x/Normal.png" in page
    assert "from upload.wikimedia.org" in page and "from upload.wikimedia.org" in md
    assert "![Normal](assets/" in md


def test_a_copy_outlives_the_site_it_came_from(stats, tmp_vault):
    _web(tmp_vault)
    _sheet(fetch=_png)
    _say("site:upload.wikimedia.org", "mine")

    def gone(url: str) -> dict:
        return {"data": b"", "type": "", "why": "Could not be read"}

    assert _sheet(fetch=gone)["count"]["images"] == 1
    _build()
    assert len(list((_live(stats) / "assets").iterdir())) == 1


@pytest.mark.parametrize("why", ["Over 2 MB", "An SVG", "Not an image", "Could not be read"])
def test_what_cannot_be_copied_stays_a_link_and_asks_nothing(stats, tmp_vault, why):
    _web(tmp_vault)
    groups = {g["key"]: g for g in _sheet(fetch=lambda url: {"data": b"", "type": "", "why": why})["groups"]}
    site = groups["site:upload.wikimedia.org"]
    assert not site["images"] and site["bad"][0]["why"] == why


@pytest.mark.parametrize("response,why", [
    (WrongType("refusing to read text/html content at x"), "Not an image"),
    (TooLarge("body at x passed 2097152 bytes; refusing to read on"), "Over 2 MB"),
    (ValueError("404 at x"), "Could not be read"),
    (httpx.Response(200, headers={"content-type": "image/svg+xml"}, content=b"<svg/>"), "An SVG"),
    (httpx.Response(200, headers={"content-type": "image/avif"}, content=b"x"), "Not an image"),
    (httpx.Response(200, headers={"content-type": "image/png"}, content=PNG), ""),
])
def test_the_guard_says_why_an_image_cannot_be_copied(monkeypatch, response, why):
    from silica.sources.discover import read

    def fake(url, state=None, *, types, cap):
        assert types == ("image/",) and cap == read.COVER_CAP
        if isinstance(response, Exception):
            raise response
        return response, url

    monkeypatch.setattr(read, "_get", fake)
    got = read.fetch_figure("https://a.test/x")
    assert got["why"] == why and bool(got["data"]) is (not why)


# --- the note an author wrote --------------------------------------------------

def _row(sheet: dict, title: str) -> dict:
    return next(n for f in sheet["files"] for n in f["notes"] if n["title"] == title)


def test_a_note_nobody_touched_since_silica_wrote_it_cannot_be_marked(stats):
    _written(stats, "Stats/Bayes.md")
    row = _row(_sheet(), "Bayes")
    assert row["can"] is False and row["status"] == "Not edited since Silica wrote it"


def test_a_note_the_journal_has_no_record_of_can_be_marked(stats):
    row = _row(_sheet(), "Bayes")
    assert row["can"] is True and row["status"].startswith("No record of what Silica wrote")


def test_a_note_the_author_edited_can_be_marked_and_then_goes_out_alone(stats, tmp_vault):
    from silica.kernel import rights

    _written(stats, "Stats/Bayes.md")
    tmp_vault.note("Stats/Bayes.md", "In my own words, after the exam.\n")
    assert _row(_sheet(), "Bayes")["can"] is True
    rights.set_rewritten("Stats/Bayes.md", True)
    _build()
    assert "bayes.html" in _names(stats) and "variance.html" not in _names(stats)
    assert "rewritten" not in (_live(stats) / "bayes.md").read_text(encoding="utf-8")
    manifest = json.loads((_live(stats) / "vault.json").read_text(encoding="utf-8"))
    assert manifest["rights"]["rewritten"] == ["bayes"]
    rights.set_rewritten("Stats/Bayes.md", False)
    _build()
    assert "bayes.html" not in _names(stats)


def test_a_mark_typed_on_a_note_nobody_touched_is_not_an_edit_and_does_not_release_it(stats):
    note = stats / "Stats/Bayes.md"
    _written(stats, "Stats/Bayes.md")
    note.write_text("---\nrewritten: true\n---\n" + note.read_text(encoding="utf-8"), encoding="utf-8")
    _say(ROSS, "else")
    row = _row(_sheet(), "Bayes")
    assert row["can"] is False and row["goes"] is False and row["status"].startswith("Not edited")


def test_a_note_the_author_edited_can_be_marked_again_after_the_mark_is_taken_back(stats, tmp_vault):
    from silica.kernel import rights

    _written(stats, "Stats/Bayes.md")
    tmp_vault.note("Stats/Bayes.md", "In my own words, after the exam.\n")
    rights.set_rewritten("Stats/Bayes.md", True)
    rights.set_rewritten("Stats/Bayes.md", False)
    assert _row(_sheet(), "Bayes")["can"] is True


def test_undo_takes_a_mark_back(stats, tmp_vault, capsys):
    from silica.cli import _handle_direct_shortcut
    from silica.kernel import rights

    tmp_vault.note("Stats/Bayes.md", "In my own words, after the exam.\n")
    rights.set_rewritten("Stats/Bayes.md", True)
    assert "rewritten" in (stats / "Stats/Bayes.md").read_text(encoding="utf-8")
    assert _handle_direct_shortcut("/undo Stats/Bayes.md", []) is True
    assert "rewritten" not in (stats / "Stats/Bayes.md").read_text(encoding="utf-8")


def test_one_note_with_two_kinds_of_data_counts_once(stats, tmp_vault):
    tmp_vault.note("Stats/Hand note.md", "Write to a.b@example.org or +39 333 1234567.\n")
    checks = _sheet()["checks"]
    assert len(checks["personal"]) == 2 and checks["personal_notes"] == 1


def test_a_note_copied_word_for_word_cannot_be_marked_and_a_hand_mark_does_not_release_it(stats, tmp_vault):
    from silica.kernel import rights

    tmp_vault.note("Stats/Bayes.md", f"---\nrewritten: true\n---\n{BOOK}\n")
    row = _row(_sheet(), "Bayes")
    assert row["can"] is False and "copied word for word" in row["status"]
    with pytest.raises(ValueError):
        rights.check_mark(rights.read_wiki("stats"), "Stats/Bayes.md")
    _build()
    assert "bayes.html" not in _names(stats)


def test_publish_true_alone_releases_nothing(stats, tmp_vault):
    tmp_vault.note("Stats/Bayes.md", "---\npublish: true\n---\nStill the book's.\n")
    _build()
    assert "bayes.html" not in _names(stats)


def test_the_share_counts_runs_of_eight_words_and_stops_at_another_language():
    from silica.kernel.rights import copied_share

    assert copied_share(BOOK, BOOK) == 100
    assert copied_share("I would put it differently, in a sentence of my own making today.", BOOK) == 0
    italian = ("La probabilità di un evento è un numero tra zero e uno che misura quanto sia "
               "probabile che accada in una lunga serie di prove indipendenti dello stesso esperimento")
    assert copied_share(italian, BOOK) is None


# --- other checks --------------------------------------------------------------

@pytest.mark.parametrize("text,found", [
    ("Write to marta.rossi@unipa.it for the slides.", "an email"),
    ("Call +39 333 1234567 after six.", "a phone"),
    ("Mobile 333 1234567.", "a phone"),
    ("Codice RSSMRA85T10A562S on the form.", "a tax code"),
    ("Pay to IT60 X054 2811 1010 0000 0123 456 by Friday.", "an IBAN"),
])
def test_personal_data_is_found(text, found):
    from silica.kernel.rights import personal_data

    assert personal_data(text) == [found]


def test_the_check_reads_what_goes_out_and_not_what_is_hidden():
    from silica.kernel.rights import _body, personal_data

    # a diagram's own %% comment is code: read as a remark it hid all that followed from the check
    fenced = "```mermaid\n%% a note\nflowchart LR\n```\n\nmario.rossi@example.com\n"
    assert personal_data(_body(fenced)) == ["an email"]
    assert personal_data(_body("%% mario.rossi@example.com %%\n<!-- mario.rossi@example.com -->\nplain\n")) == []


@pytest.mark.parametrize("text", [
    "In 1998 2004 and 2011 the rate was 3.14159 and 2.71828.",
    "IT60 X054 2811 1010 0000 0123 457 is not a real account.",
    "x = 333.1234567 in the table.",
])
def test_numbers_are_not_personal_data(text):
    from silica.kernel.rights import personal_data

    assert personal_data(text) == []


def test_personal_data_keeps_a_listed_wiki_out_of_the_catalog_and_the_screen_says_so(stats, tmp_vault):
    from silica.kernel.publish import update_vault

    update_vault("stats", {"license": "CC BY 4.0", "listed": True})
    assert _sheet()["wikis"] == "It can ask to enter Wikis."
    _build()
    assert json.loads((_live(stats) / "vault.json").read_text(encoding="utf-8"))["listed"] is True
    tmp_vault.note("Stats/Hand note.md", "Write to marta.rossi@unipa.it.\n")
    sheet = _sheet()
    assert sheet["ask"] is True and sheet["checks"]["personal"] == [["Hand note", "an email"]]
    assert sheet["wikis"] == "It can ask to enter Wikis once the note in Other checks is fixed."
    _build()
    assert json.loads((_live(stats) / "vault.json").read_text(encoding="utf-8"))["listed"] is False


def test_a_license_not_stated_keeps_a_wiki_out_of_the_catalog(stats):
    from silica.kernel.publish import update_vault

    update_vault("stats", {"listed": True})
    assert _sheet()["wikis"] == 'It cannot ask to enter Wikis while its license is "not stated".'
    _build()
    assert json.loads((_live(stats) / "vault.json").read_text(encoding="utf-8"))["listed"] is False


def test_a_share_alike_source_asks_the_wiki_to_carry_it(stats):
    from silica.kernel.publish import update_vault

    update_vault("stats", {"license": "CC BY 4.0"})
    _say(ROSS, "open", license="CC BY-SA", credit="Ross")
    assert _sheet()["wikis"] == "It can ask to enter Wikis once its license is CC BY-SA 4.0."
    update_vault("stats", {"license": "CC BY-SA 4.0"})
    assert _sheet()["wikis"] == "It can ask to enter Wikis."


def test_the_count_follows_the_answers_and_says_the_links_they_cost(stats):
    first = _sheet()
    assert first["count"] == {"notes": 2, "stay": 3, "images": 0, "kept": 3, "links": 0}
    assert first["checks"]["lost"] == 1 and first["checks"]["nowhere"] == 1
    _say(ROSS, "mine")
    _say("Slides.pdf", "mine")
    second = _sheet()
    assert second["count"] == {"notes": 5, "stay": 0, "images": 1, "kept": 2, "links": 0}
    assert second["checks"]["lost"] == 0


def test_a_private_wiki_is_asked_the_same_without_the_line_on_wikis(stats, monkeypatch):
    from silica.kernel import publish

    monkeypatch.setattr(publish, "private_allowed", lambda: "ok")
    publish.set_visibility("stats", "private")
    sheet = _sheet()
    assert sheet["ask"] is True and sheet["wikis"] == ""


# --- a photo's location --------------------------------------------------------

def _tiff(gps: bool = True) -> bytes:
    """A little-endian TIFF block: Orientation 6, and a GPS block with one rational."""
    ifd0_entries = 2 if gps else 1
    gps_at = 8 + 2 + 12 * ifd0_entries + 4
    out = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", ifd0_entries)
    out += struct.pack("<HHIHH", 0x0112, 3, 1, 6, 0)
    if gps:
        out += struct.pack("<HHII", 0x8825, 4, 1, gps_at)
    out += struct.pack("<I", 0)
    if gps:
        data_at = gps_at + 2 + 12 + 4
        out += struct.pack("<H", 1) + struct.pack("<HHII", 2, 5, 1, data_at) + struct.pack("<I", 0)
        out += struct.pack("<II", 38, 1)
    return out


def _jpeg(tiff: bytes) -> bytes:
    body = b"Exif\x00\x00" + tiff
    return b"\xff\xd8\xff\xe1" + struct.pack(">H", len(body) + 2) + body + b"\xff\xda\x00\x02\xff\xd9"


def _png_exif(tiff: bytes) -> bytes:
    crc = struct.pack(">I", zlib.crc32(b"eXIf" + tiff))
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(tiff)) + b"eXIf" + tiff + crc


def test_a_jpegs_location_is_blanked_and_its_orientation_stays():
    from silica.kernel.image_meta import has_location, strip_location

    data = _jpeg(_tiff())
    assert has_location(data)
    clean = strip_location(data)
    assert len(clean) == len(data) and not has_location(clean)
    assert struct.pack("<I", 38) not in clean
    assert struct.pack("<HHIHH", 0x0112, 3, 1, 6, 0) in clean


def test_a_pngs_location_is_blanked_and_its_checksum_still_holds():
    from silica.kernel.image_meta import has_location, strip_location

    clean = strip_location(_png_exif(_tiff()))
    assert not has_location(clean)
    length = struct.unpack(">I", clean[8:12])[0]
    body = clean[12:16 + length]
    assert struct.unpack(">I", clean[16 + length:20 + length])[0] == zlib.crc32(body)


@pytest.mark.parametrize("data", [_jpeg(_tiff(gps=False)), PNG, b"", b"\xff\xd8\xff\xe1\x00", b"not an image"])
def test_an_image_with_no_location_is_returned_as_it_came(data):
    from silica.kernel.image_meta import has_location, strip_location

    assert strip_location(data) == data and not has_location(data)


def test_a_published_photo_loses_its_location_and_the_sheet_counts_it(stats):
    (stats / "Pasted image 1.png").write_bytes(_jpeg(_tiff()))
    assert _sheet()["checks"]["location"] == 1
    _say("folder:Stats/Descriptive", "mine")
    _build()
    from silica.kernel.image_meta import has_location

    published = next((_live(stats) / "assets").glob("*pasted-image-1*"))
    assert not has_location(published.read_bytes())


def test_listing_a_blocked_wiki_after_publish_does_not_reach_the_catalog(stats):
    from silica.ui.web.public_site import set_listed

    _build()
    set_listed("stats", True)
    assert json.loads((_live(stats) / "vault.json").read_text(encoding="utf-8"))["listed"] is False


def test_numbered_files_come_in_their_order(stats, tmp_vault):
    from silica.kernel.write.provenance import append_record

    for n in (10, 2):
        tmp_vault.note(f"sources/L{n}.md", f'---\nsource_file: "/home/u/Lezione {n}.pdf"\n---\nText.\n')
        tmp_vault.note(f"Stats/Note {n}.md", "Text.\n")
        append_record(f"L{n}.md", "x", f"r{n}", [f"Stats/Note {n}"], vault_path=str(stats))
    names = [f["name"] for f in _sheet()["files"]]
    assert names.index("Lezione 2.pdf") < names.index("Lezione 10.pdf")


def test_an_address_with_brackets_is_fetched_whole(stats, tmp_vault):
    url = "https://upload.wikimedia.org/x/Bell_(curve).png"
    tmp_vault.note("Stats/Web.md", f"![Bell]({url})\n")
    asked: list[str] = []

    def fetch(address: str) -> dict:
        asked.append(address)
        return _png(address)

    _sheet(fetch=fetch)
    assert asked == [url]
