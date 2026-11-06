"""A raw and the sources it became (ADR-0129 points 1 and 5, amended
2026-11-04): the PDF stays, its parts know their pages, a split document has
a parent note, the drawer and the rail reach the file through a note, and a
file the server deleted before is recognised when it is brought in again."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from silica.config import CONFIG
from silica.sources import convert as conv
from tests.doc_factory import pdf_bytes

PAGES = ["Alpha opens the survey of tracing models\nmore words about alpha on the first page",
         "Beta continues with deep models of knowledge\nmore words about beta on the second page",
         "Gamma closes with considerations on datasets\nmore words about gamma on the third page"]
TOC = [(1, "Alpha", 1), (1, "Beta", 2), (1, "Gamma", 3)]


def _fm(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])


@pytest.fixture
def client(tmp_vault, monkeypatch):
    from silica.sources import pdf_extract
    from silica.ui.web import server
    from silica.ui.web.session import SESSION

    monkeypatch.setattr(CONFIG, "pdf_provider", "pdfium")
    monkeypatch.setattr(server, "_embed_imported", lambda folders: None)
    pdf_extract._CACHE.clear()
    SESSION.reset()
    return TestClient(server.app)


def _split_survey(monkeypatch) -> tuple[Path, list[str], bytes]:
    """A three-page PDF in the Inbox, converted into three parts."""
    monkeypatch.setattr(conv, "_MAX_SEGMENT_CHARS", 120)  # a part a page, in a test-sized PDF
    vault = Path(CONFIG.vault_path)
    data = pdf_bytes(PAGES, toc=TOC)
    (vault / "Inbox").mkdir(exist_ok=True)
    (vault / "Inbox/survey.pdf").write_bytes(data)
    return vault, conv.convert("Inbox/survey.pdf"), data


# --- the conversion: pages per part, the parent note -------------------------

def test_a_split_pdf_gives_each_part_its_pages_and_writes_the_parent_note(client, monkeypatch):
    vault, parts, _ = _split_survey(monkeypatch)

    spans = [(f["source_page"], f["source_page_end"]) for f in (_fm(vault / p) for p in parts)]
    parent = vault / "Inbox/survey.md"
    fm = _fm(parent)

    assert len(parts) == 3 and spans == [(1, 2), (2, 3), (3, 3)]
    assert fm["document"] is True and fm["source_pages"] == 3
    assert all(f"[[{Path(p).stem}]] (pages" in parent.read_text(encoding="utf-8") for p in parts)
    assert "Inbox/survey.md" not in parts and conv.is_skippable_chunk("Inbox/survey.md")


def test_a_part_is_found_by_its_first_prose_and_not_by_a_heading_the_outline_wrote():
    texts = ["intro words here and the start of it", "page two has the deep models text", "page three"]
    parts = ["# Intro\n\nintro words here", "## Deep models\n\npage two has the deep models text",
             "## Heading on no page\n\nprose found on no page at all"]

    # the third keeps the page of the one before it
    assert conv.part_pages(parts, Path("x.pdf"), texts) == [(1, 2), (2, 2), (2, 3)]
    assert conv.part_pages(parts, Path("x.pdf"), []) is None


# --- the drawer and the rail ---------------------------------------------------

def test_a_part_opens_its_pdf_and_the_route_serves_it_under_the_asset_headers(client, monkeypatch):
    vault, parts, data = _split_survey(monkeypatch)

    raw = client.get("/note/raw", params={"path": parts[1]}).json()["raw"]
    shown = client.get("/raw", params={"path": parts[1]})
    saved = client.get("/raw", params={"path": parts[1], "download": 1})

    assert raw == {"name": "survey.pdf", "kept": True, "open": True, "pages": 3, "from": 2, "to": 3,
                   "path": "Inbox/survey.pdf"}
    assert shown.status_code == 200 and shown.content == data
    assert shown.headers["content-type"] == "application/pdf"
    assert "sandbox" in shown.headers["content-security-policy"]
    assert shown.headers["content-disposition"].startswith("inline")
    assert saved.headers["content-disposition"].startswith('attachment; filename="survey.pdf"')
    assert client.get("/note/raw", params={"path": "Inbox/survey.md"}).json()["raw"]["parts"] == 3


def test_a_raw_outside_the_vault_or_in_a_chat_is_offered_nowhere_and_the_route_refuses_it(client, tmp_path):
    outside = tmp_path / "elsewhere.pdf"
    outside.write_bytes(pdf_bytes(["x"]))
    vault = Path(CONFIG.vault_path)
    (vault / ".silica-chat/s1").mkdir(parents=True)
    (vault / ".silica-chat/s1/drop.pdf").write_bytes(pdf_bytes(["x"]))
    for name, value in (("far.md", str(outside)), ("up.md", "../elsewhere.pdf"), ("own.md", "far.md"),
                        ("drop.md", ".silica-chat/s1/drop.pdf")):
        (vault / name).write_text(f'---\nsource_file: "{value}"\n---\n\ntext\n', encoding="utf-8")

    for name in ("far.md", "up.md", "own.md", "drop.md"):
        assert "raw" not in client.get("/note/raw", params={"path": name}).json()
        assert client.get("/raw", params={"path": name}).status_code == 404


def test_the_rail_lists_a_raw_in_its_folder_and_the_frame_does_not(client, monkeypatch):
    from silica.ui.web.graph_view import render_tree

    _split_survey(monkeypatch)
    tree = client.get("/vault_info").json()["tree"]
    raws = {"Inbox/survey.pdf": ["Inbox/survey.md"]}

    assert 'class="tree-file" href="/raw?path=Inbox%2Fsurvey.md"' in tree
    assert 'data-raw="Inbox/survey.pdf"' in tree and '<span class="tree-ext">.pdf</span>' in tree
    assert "tree-file" not in render_tree([{"path": "Inbox/survey.md", "id": "Inbox/survey.md"}], raws=raws)
    # the folder counts notes, which its menu's delete removes, and not the raw beside them
    rail = render_tree([{"path": "Inbox/survey.md", "id": "Inbox/survey.md"}], actions=True, raws=raws)
    assert '<summary draggable="true">Inbox<span class="tree-n">1</span>' in rail


def test_silica_files_lists_a_converted_raw_as_the_raw_of_its_source(client, monkeypatch):
    import json

    from silica.tools import TOOLS

    _split_survey(monkeypatch)
    out = json.loads(TOOLS["silica_files"].run(folder="Inbox"))

    assert "Inbox/survey.pdf" not in out.get("unconverted", [])
    assert out["raws"] == [{"path": "Inbox/survey.pdf", "source": "Inbox/survey.md"}]


# --- a file the server deleted, brought in again --------------------------------

def test_the_same_file_brought_in_again_is_attached_to_its_notes_and_not_converted(client, monkeypatch):
    """Notes written while the server deleted the PDF name it by its bare
    name and carry no pages: the same bytes through the vault's + become
    their raw again, each part gets its pages, and no job runs."""
    monkeypatch.setenv("SILICA_HOSTED", "1")
    vault = Path(CONFIG.vault_path)
    data = pdf_bytes(PAGES, toc=TOC)
    sha = hashlib.sha256(data).hexdigest()
    parts = {"Inbox/survey/survey-1-alpha.md": "more words about alpha on the first page",
             "Inbox/survey/survey-2-beta.md": "more words about beta on the second page",
             "Inbox/survey/survey-3-gamma.md": "more words about gamma on the third page"}
    (vault / "Inbox/survey").mkdir(parents=True)
    for rel, text in parts.items():
        (vault / rel).write_text(f"---\nsource_sha256: {sha}\nsource_pages: 3\n"
                                 f'source_file: "survey.pdf"\n---\n\n## Part\n\n{text}\n', encoding="utf-8")
    gone = client.get("/note/raw", params={"path": "Inbox/survey/survey-2-beta.md"}).json()["raw"]

    files = [("files", ("survey.pdf", data, "application/pdf"))]
    (row,) = client.post("/vault/import", files=files, data={"paths": [""], "last": "1"}).json()["files"]
    back = client.get("/note/raw", params={"path": "Inbox/survey/survey-2-beta.md"}).json()["raw"]

    assert gone["kept"] is False and gone["gone"] == "removed" and gone["again"] is True
    assert row["status"] == "note" and row["attached"] is True and "job" not in row
    assert row["notes"] == sorted(parts)
    # the + puts it at the top of the vault (owner, 2026-11-02), where its notes now find it
    assert [_fm(vault / p)["source_file"] for p in sorted(parts)] == ["survey.pdf"] * 3
    assert (vault / "survey.pdf").is_file()
    assert back["kept"] is True and (back["from"], back["to"]) == (2, 3)
    assert not (vault / "Inbox/survey.md").exists()  # attached, not converted: no parent written


def test_another_file_is_converted_as_before(client, monkeypatch):
    monkeypatch.setenv("SILICA_HOSTED", "1")
    vault = Path(CONFIG.vault_path)
    (vault / "Inbox").mkdir(exist_ok=True)
    (vault / "Inbox/old.md").write_text(f"---\nsource_sha256: {'0' * 64}\n"
                                        'source_file: "old.pdf"\n---\n\ntext\n', encoding="utf-8")
    files = [("files", ("paper.pdf", pdf_bytes(["The full text of a paper."]), "application/pdf"))]

    (row,) = client.post("/vault/import", files=files, data={"paths": [""], "last": "1"}).json()["files"]

    assert row["status"] == "queued"
