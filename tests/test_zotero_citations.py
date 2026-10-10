# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0055 decision 6: citations are a derived edge, matched by DOI and arXiv
id from each paper leaf's references, computed at graph build and never written."""
from __future__ import annotations

import pytest

from silica.kernel.recall import citations as c


@pytest.mark.parametrize("raw, want", [
    ("10.48550/arXiv.2310.08560", "arxiv:2310.08560"),
    ("https://doi.org/10.1145/3586183.3606763", "doi:10.1145/3586183.3606763"),
    ("10.1145/ABC.Def", "doi:10.1145/abc.def"),
    ("arXiv:2310.08560v2", "arxiv:2310.08560"),
    ("2310.08560", "arxiv:2310.08560"),
    ("", ""),
    ("not an id", ""),
])
def test_norm_id(raw, want):
    assert c.norm_id(raw) == want


def test_ids_in_a_reference_list():
    refs = ("[1] C. Packer et al. MemGPT. arXiv:2310.08560v2, 2023.\n"
            "[2] A. B. Some paper. In ACL, doi:10.18653/v1/2023.acl-long.1.\n"
            "[3] Same as 1: https://arxiv.org/abs/2310.08560\n")
    assert c.ids_in(refs) == {"arxiv:2310.08560", "doi:10.18653/v1/2023.acl-long.1"}


def test_reference_text_starts_at_the_last_references_heading():
    body = ("## Introduction\n\nAs arXiv:1111.11111 showed.\n\n"
            "## References\n\n[1] arXiv:2222.22222\n")
    assert c.ids_in(c.reference_text(body)) == {"arxiv:2222.22222"}


def test_reference_text_without_a_heading_is_the_whole_body():
    assert c.ids_in(c.reference_text("see arXiv:2222.22222")) == {"arxiv:2222.22222"}


def _leaf(vault, name, *, key, authors, year, title, arxiv="", doi="", refs=""):
    fm = (f"---\nzotero_key: {key}\ntitle: \"{title}\"\nauthors:\n"
          + "".join(f"- \"{a}\"\n" for a in authors)
          + f"year: {year}\n" + (f"arxiv: \"{arxiv}\"\n" if arxiv else "")
          + (f"doi: \"{doi}\"\n" if doi else "") + "---\n\n")
    vault.note(f"sources/{name}", fm + f"# {title}\n\nbody\n\n## References\n\n{refs}\n")
    return {"id": f"sources/{name}", "label": name, "type": "note"}


def test_a_paper_cites_the_papers_its_references_name(tmp_vault):
    a = _leaf(tmp_vault, "@a.md", key="AAAA1111", authors=["Xu, Wujiang"], year=2025,
              title="A-MEM", arxiv="2502.12110",
              refs="[1] arXiv:2310.08560\n[2] arXiv:2502.12110 (this paper)")
    b = _leaf(tmp_vault, "@b.md", key="BBBB2222", authors=["Packer, Charles"], year=2024,
              title="MemGPT", doi="10.48550/arXiv.2310.08560")
    note = {"id": "Concepts/Memory.md", "label": "Memory", "type": "note"}

    edges, missing = c.paper_layer([a, b, note])

    assert [(e["from"], e["to"], e["type"]) for e in edges] == [
        ("sources/@a.md", "sources/@b.md", "CITES")]
    assert missing == []
    assert (a["type"], a["label"], b["label"]) == ("paper", "Xu 2025", "Packer 2024")
    assert note["type"] == "note"


def test_an_id_cited_by_five_papers_but_absent_is_a_missing_paper(tmp_vault):
    nodes = [_leaf(tmp_vault, f"@p{i}.md", key=f"PPPP000{i}", authors=[f"Author{i}, A"],
                   year=2020 + i, title=f"Paper {i}",
                   refs=("[1] OpenAI. GPT-4 technical report. arXiv:2303.08774, 2023.\n"
                         if i < 5 else "") + f"[2] arXiv:9999.{i:05d}")
             for i in range(6)]

    edges, missing = c.paper_layer(nodes)

    [gpt4] = missing
    assert gpt4["type"] == "missing"
    assert gpt4["id"] == "__missing__arxiv:2303.08774"
    assert gpt4["href"] == "https://arxiv.org/abs/2303.08774"
    assert "GPT-4 technical report" in gpt4["quote"]
    assert sorted(e["from"] for e in edges if e["to"] == gpt4["id"]) == [
        f"sources/@p{i}.md" for i in range(5)]


def test_four_citations_do_not_make_a_missing_paper(tmp_vault):
    nodes = [_leaf(tmp_vault, f"@p{i}.md", key=f"PPPP000{i}", authors=[f"A{i}, B"],
                   year=2020, title=f"P{i}", refs="[1] arXiv:2303.08774")
             for i in range(4)]

    _, missing = c.paper_layer(nodes)

    assert missing == []


def test_same_surname_and_year_get_a_and_b_by_title(tmp_vault):
    x = _leaf(tmp_vault, "@x.md", key="XXXX1111", authors=["Wang, Lei"], year=2025, title="Zeta")
    y = _leaf(tmp_vault, "@y.md", key="YYYY2222", authors=["Wang, Yu"], year=2025, title="Alpha")

    c.paper_layer([x, y])

    assert (y["label"], x["label"]) == ("Wang 2025a", "Wang 2025b")


def _island(html: str) -> dict:
    import json
    start = html.index('<script id="graph-data"')
    start = html.index(">", start) + 1
    return json.loads(html[start:html.index("</script>", start)])


def test_the_web_graph_carries_papers_citations_and_missing_papers(tmp_vault, monkeypatch, tmp_path):
    """The layer reaches the frame, and a missing paper is never counted or
    coloured as a note: it is appended after the partitions are computed."""
    import silica.ui.web.graph_view as gv

    monkeypatch.setattr(gv, "_vendored_lib_js", lambda: "/*JS*/")
    for i in range(5):
        _leaf(tmp_vault, f"@p{i}.md", key=f"PPPP000{i}", authors=[f"Author{i}, A"],
              year=2020, title=f"Paper {i}", arxiv=f"2001.0000{i}",
              refs="[1] arXiv:2303.08774\n" + (f"[2] arXiv:2001.0000{i + 1}" if i < 4 else ""))

    res = gv.export_graph(output_path=str(tmp_path / "g.html"))
    data = _island((tmp_path / "g.html").read_text(encoding="utf-8"))

    kinds = {n["id"]: n["type"] for n in data["nodes"]}
    assert kinds["sources/@p0.md"] == "paper"
    assert kinds["__missing__arxiv:2303.08774"] == "missing"
    cites = [(e["from"], e["to"]) for e in data["edges"] if e["type"] == "CITES"]
    assert ("sources/@p0.md", "sources/@p1.md") in cites
    assert res["nodes"] == 5
