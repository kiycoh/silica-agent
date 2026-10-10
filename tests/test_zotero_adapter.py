# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0055 decisions 1, 3 and 5: the Zotero adapter reads the running app's
local API, resolves a target to papers, and gives each paper its identity."""
from __future__ import annotations

from pathlib import Path

import pytest

from silica.sources import zotero as z
from tests.zotero_fake import SERVER_ID, FakeZotero

_PDF = Path("/home/user/Zotero/storage/QH2BR53F/"
            "Packer et al. - 2024 - MemGPT Towards LLMs as Operating Systems.pdf")


def _client(fake: FakeZotero, server_id: str = SERVER_ID) -> z.Zotero:
    return z.Zotero(server_id=server_id, transport=fake.transport())


@pytest.mark.parametrize("target, want", [
    ("@packerMemGPTLLMsOperating2024", ("citekey", "packerMemGPTLLMsOperating2024")),
    ("zotero://select/library/items/8H4AC8GW", ("item", "8H4AC8GW")),
    ("zotero://select/library/collections/X7DB9QVX", ("collection", "X7DB9QVX")),
    ("paper.pdf", None),
    ("@", None),
    ("notes/@x", None),
    ("zotero://select/library/items/", None),
])
def test_parse_target(target, want):
    assert z.parse_target(target) == want


def test_a_paper_carries_its_identity_and_its_pdf():
    fake = FakeZotero()
    item, _ = fake.add_paper()

    paper = z.paper_of(_client(fake), item)

    assert paper.pdf == _PDF
    assert paper.leaf_name == "@packerMemGPTLLMsOperating2024.md"
    assert paper.fields == {
        "zotero_key": "8H4AC8GW",
        "citekey": "packerMemGPTLLMsOperating2024",
        "title": "MemGPT: Towards LLMs as Operating Systems",
        "authors": ["Packer, Charles", "Wooders, Sarah", "Lin, Kevin", "Fang, Vivian",
                    "Patil, Shishir G.", "Stoica, Ion", "Gonzalez, Joseph E."],
        "year": 2024,
        "venue": "arXiv",
        "doi": "10.48550/arXiv.2310.08560",
        "arxiv": "2310.08560",
        "pdf_key": "QH2BR53F",
        "pdf_md5": "16e65f7cc5c1d647eb6acffdf085ce62",
    }


def test_a_paper_without_a_citekey_is_named_after_its_key():
    fake = FakeZotero()
    item, _ = fake.add_paper()
    item["data"]["citationKey"] = ""

    paper = z.paper_of(_client(fake), item)

    assert paper.leaf_name == "@zotero-8H4AC8GW.md"
    assert "citekey" not in paper.fields


def test_an_item_whose_best_attachment_is_not_a_pdf_has_no_pdf():
    fake = FakeZotero()
    item, _ = fake.add_paper()
    item["links"]["attachment"]["attachmentType"] = "text/html"

    paper = z.paper_of(_client(fake), item)

    assert paper.pdf is None
    assert "pdf_md5" not in paper.fields


def test_a_citekey_target_resolves_to_its_paper():
    fake = FakeZotero()
    item, _ = fake.add_paper()
    fake.clone(item, "AAAA1111", citationKey="otherKey2020", collections=[])

    [paper] = z.resolve(_client(fake), "@packerMemGPTLLMsOperating2024")

    assert paper.fields["zotero_key"] == "8H4AC8GW"


def test_a_collection_target_takes_its_own_items_only():
    fake = FakeZotero()
    item, _ = fake.add_paper()
    fake.clone(item, "AAAA1111", citationKey="sub2020", collections=["SUBCOLL1"])

    papers = z.resolve(_client(fake), "zotero://select/library/collections/X7DB9QVX")

    assert [p.fields["zotero_key"] for p in papers] == ["8H4AC8GW"]


def test_another_library_answering_refuses_the_read():
    fake = FakeZotero(server_id="Qm41xTr8pZ0a")
    fake.add_paper()

    with pytest.raises(z.ZoteroError, match="another Zotero library"):
        z.resolve(_client(fake), "zotero://select/library/items/8H4AC8GW")


def test_a_closed_zotero_is_named_as_such():
    fake = FakeZotero()
    fake.refuse = True

    with pytest.raises(z.ZoteroError, match="not running"):
        z.resolve(_client(fake), "zotero://select/library/items/8H4AC8GW")


def test_a_paper_without_a_pdf_becomes_a_terminal_leaf_from_its_abstract(tmp_vault, monkeypatch):
    fake = FakeZotero()
    item, _ = fake.add_paper()
    del item["links"]["attachment"]
    monkeypatch.setattr(z, "client", lambda: _client(fake))

    adapter = z.ZOTERO
    stub = adapter.to_stub(adapter.read("zotero://select/library/items/8H4AC8GW"))

    from silica.kernel.write import frontmatter
    data, _, body = frontmatter.split(stub.body)
    assert stub.lane == "terminal"
    assert stub.note_path == "sources/@packerMemGPTLLMsOperating2024.md"
    assert data["zotero_key"] == "8H4AC8GW"
    assert "# MemGPT: Towards LLMs as Operating Systems" in body
    assert "virtual context management" in body


def test_memberships_name_each_papers_collections_live():
    fake = FakeZotero()
    item, _ = fake.add_paper()
    col = fake.collection("03-agents-and-memory")
    item["data"]["collections"] = [col["key"]]

    assert z.memberships(_client(fake)) == {"8H4AC8GW": ["03-agents-and-memory"]}
