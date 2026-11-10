"""A vault's files as a library (ADR-0156): a book's chapters, the editions
its photo sets reproduce, and the files as a layer of Explore's graph."""
from __future__ import annotations

from array import array
from pathlib import Path

from silica.config import CONFIG
from silica.sources import library
from tests.doc_factory import pdf_bytes


def _sizes(steps):
    return [s["to"] - s["from"] + 1 for s in steps]


# --- the ladder ---------------------------------------------------------------

def test_an_outline_is_the_steps_without_its_front_and_evened_out():
    outline = [("Title Page", 2), ("Copyright", 3), ("Contents", 4), ("Ch. 1 Watchers", 5),
               ("Ch. 2 Parables", 7), ("Ch. 3 Noah", 15), ("Ch. 4 Heaven", 60)]

    source, steps = library.ladder(100, outline, openings=[])

    assert source == "outline"
    assert steps[0]["from"] == 5 and steps[-1]["to"] == 100  # the front is no step, the book ends the last
    assert all(b["from"] == a["to"] + 1 for a, b in zip(steps, steps[1:]))
    assert min(_sizes(steps)) >= library.MERGE_UNDER and max(_sizes(steps)) <= library.SPLIT_OVER
    # chapter 1 is 2 pages and joins chapter 2, whose title it keeps; chapter
    # 4's 41 pages are three runs
    assert steps[0]["title"] == "Ch. 1 Watchers" and steps[0]["parts"] == ["Ch. 2 Parables"]
    assert [s["title"] for s in steps if s["title"].startswith("Ch. 4")] == [
        "Ch. 4 Heaven (1/3)", "Ch. 4 Heaven (2/3)", "Ch. 4 Heaven (3/3)"]


def test_chapter_pages_cut_a_book_with_no_outline():
    openings = [("CHAPTER I.", p) for p in (3, 20, 38, 55, 70)] + [("CHAPTER VI.", 88)]

    source, steps = library.ladder(100, [], openings)

    assert source == "chapters" and steps[0]["from"] == 3 and steps[0]["title"] == "CHAPTER I."


def test_a_book_with_nothing_to_cut_at_is_runs_of_pages_and_a_short_tail_joins_the_last():
    assert library.ladder(40, [], [])[1] == [
        {"title": "pages 1 to 15", "from": 1, "to": 15}, {"title": "pages 16 to 30", "from": 16, "to": 30},
        {"title": "pages 31 to 40", "from": 31, "to": 40}]
    source, steps = library.ladder(33, [], [("CHAPTER I.", 3)])  # one chapter page is no structure
    assert source == "pages" and _sizes(steps) == [15, 18]


def test_chapters_reads_a_pdfs_outline(tmp_vault):
    vault = Path(CONFIG.vault_path)
    pages = [f"page {i} of the book" for i in range(1, 41)]
    (vault / "lib").mkdir()
    (vault / "lib/book.pdf").write_bytes(pdf_bytes(pages, toc=[(1, "Contents", 1), (1, "Genesis", 2),
                                                               (1, "Exodus", 20)]))

    got = library.chapters("lib/book.pdf")

    assert got["source"] == "outline" and got["pages"] == 40
    assert [(s["title"], s["from"], s["to"]) for s in got["steps"]] == [("Genesis", 2, 19), ("Exodus", 20, 40)]
    assert library.chapters("lib/missing.pdf") == {} and library.chapters("lib/notes.txt") == {}


# --- editions -------------------------------------------------------------------

def test_photo_sets_of_one_year_are_one_edition_and_a_set_with_no_year_its_own():
    got = library.editions(["a/_foto/1874-Edition-Holy-Bible", "a/_foto/1874-Edition-Holy-Bible-GENESIS",
                            "b/_foto/1874-Edition-Holy-Bible-MATTHEW", "a/_foto/Tobit-1850",
                            "a/_foto/The-book-of-Hosea"])

    assert sorted(got) == ["1850 edition", "1874 edition", "The-book-of-Hosea"]
    assert len(got["1874 edition"]) == 3


def test_a_library_under_one_folder_has_its_themes_as_areas():
    assert library._area_of(["inbox/01-a/x.pdf", "inbox/02-b/y.pdf", "inbox/z.pdf"]) == {
        "inbox/01-a/x.pdf": "01-a", "inbox/02-b/y.pdf": "02-b", "inbox/z.pdf": "inbox"}


# --- the layer --------------------------------------------------------------------

def _library_vault() -> Path:
    vault = Path(CONFIG.vault_path)
    (vault / "lib/apocrypha").mkdir(parents=True)
    (vault / "lib/bibles/_foto/Tobit-1850").mkdir(parents=True)
    (vault / "lib/apocrypha/Book-of-Enoch.pdf").write_bytes(pdf_bytes(["The watchers descended on Hermon"] * 3))
    (vault / "lib/bibles/Charles-Commentary.pdf").write_bytes(
        pdf_bytes(["A study of the book of enoch and its watchers"] * 3))
    (vault / "lib/bibles/scan.pdf").write_bytes(pdf_bytes(["", ""]))
    for i in (1, 2):
        (vault / f"lib/bibles/_foto/Tobit-1850/p{i}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return vault


def test_the_files_are_nodes_in_their_folders_areas_with_a_title_mention_between_them(tmp_vault):
    from silica.kernel.recall.engine import engine

    _library_vault()
    engine().build_index(rebuild=True)

    layer = library.file_layer(first_group=7)

    nodes = {n["id"]: n for n in layer["nodes"]}
    enoch, charles = nodes["file:lib/apocrypha/Book-of-Enoch.pdf"], nodes["file:lib/bibles/Charles-Commentary.pdf"]
    scan, photos = nodes["file:lib/bibles/scan.pdf"], nodes["edition:1850 edition"]
    assert enoch["state"] == "read" and scan["state"] == "unread"
    assert enoch["group"] != charles["group"] and min(n["group"] for n in nodes.values()) == 7
    assert sorted(label for _g, label, _n in layer["areas"]) == ["apocrypha", "bibles"]
    # an unread file is filled, in its area's colour with the life taken out
    assert scan["color"]["background"] != charles["color"]["background"]
    assert photos["pages"] == 2 and photos["path"].endswith("p1.png") and photos["group"] == charles["group"]
    cites = [e for e in layer["edges"] if e["type"] == "CITES"]
    assert [(e["from"], e["to"], e["names"]) for e in cites] == [
        ("file:lib/bibles/Charles-Commentary.pdf", "file:lib/apocrypha/Book-of-Enoch.pdf", "book of enoch")]


def test_a_file_a_note_names_is_its_notes_and_not_a_node(tmp_vault):
    from silica.kernel.recall.engine import engine

    vault = _library_vault()
    (vault / "lib/apocrypha/Enoch.md").write_text(
        '---\nsource_file: "lib/apocrypha/Book-of-Enoch.pdf"\n---\n\nThe watchers.\n', encoding="utf-8")
    engine().build_index(rebuild=True)

    ids = {n["id"] for n in library.file_layer(first_group=0)["nodes"]}

    assert "file:lib/apocrypha/Book-of-Enoch.pdf" not in ids and "file:lib/bibles/scan.pdf" in ids


def test_the_server_has_no_file_layer(tmp_vault, monkeypatch):
    _library_vault()
    monkeypatch.setenv("SILICA_HOSTED", "1")

    assert library.file_layer(first_group=0) == {"nodes": [], "edges": [], "areas": []}


def test_a_book_vector_leaves_out_its_front_back_and_bibliography(monkeypatch):
    """Page vectors match a page's kind: the closest pages of two unrelated
    books were their bibliographies (2026-11-08)."""
    from silica_core import embeddings

    dim = 2
    pages = [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [1.0, 0.0]]  # front, body, bibliography, back
    codes = array("b", [round(127 * x) for v in pages for x in v])
    rows = embeddings._Rows(memoryview(array("f", [1 / 127] * 4)).cast("B").cast("f"),
                            memoryview(codes.tobytes()).cast("b"), dim)
    meta = {"docs": {"b.pdf": {"mtime": 0, "offsets": [0, 1, 2, 3], "row": 0}}}
    monkeypatch.setattr(embeddings, "load", lambda: (meta, rows))
    lines = {"b.pdf": ["Title", "The watchers", "Bibliography", "Colophon"]}

    vec = library._book_vectors(core=None, docs=["b.pdf"], first_lines=lines)

    assert [round(x, 3) for x in vec["b.pdf"]] == [0.0, 1.0]
