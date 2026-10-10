# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0055 interface: the pure projections of app-zotero.js, run under node.

The DOM halves are thin and verified by opening the app; what a row, a status
or a label SAYS is decided here, where a test can reach it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static" / "app-zotero.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _call(tmp_path, fn: str, *args):
    src = JS.read_text(encoding="utf-8")
    pure = src[src.index("// zotero-pure:start"):src.index("// zotero-pure:end")]
    script = tmp_path / "z.js"
    script.write_text(pure + f"\nconsole.log(JSON.stringify({fn}(...JSON.parse(process.argv[2]))));\n")
    out = subprocess.run(["node", str(script), json.dumps(list(args))],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.parametrize("state, dot, words", [
    ({"state": "running", "version": "10.0.5", "items": 276, "bbt": True}, "●",
     "Zotero 10.0.5 · 276 items · Better BibTeX"),
    ({"state": "closed"}, "○", "not running"),
    ({"state": "api_off"}, "○", "running, API off"),
    ({"state": "read_only", "version": "8.0.1", "items": 3, "bbt": False}, "●", "read only: writes need Zotero 10"),
    ({"state": "other", "library": "Qm41", "linked_library": "dc8X"}, "○", "another library answers"),
])
def test_the_status_line_names_each_state(tmp_path, state, dot, words):
    s = _call(tmp_path, "zoteroStatus", state)
    assert s["dot"] == dot and words in s["text"]


def test_the_api_off_line_says_where_to_turn_it_on(tmp_path):
    s = _call(tmp_path, "zoteroStatus", {"state": "api_off"})
    assert "Allow other applications" in s["fix"]


def test_the_library_header_counts(tmp_path):
    assert _call(tmp_path, "libraryHeader", {"total": 276, "in_vault": 41, "new": 12}) == \
        "276 in Zotero · 41 in this vault · 12 new since last session"


@pytest.mark.parametrize("state, mark", [("in", "●"), ("out", "○"), ("changed", "◐"), ("new", "○")])
def test_a_row_marks_its_state(tmp_path, state, mark):
    assert _call(tmp_path, "rowMark", state)["mark"] == mark


def test_a_suggestion_reads_first_choice_and_vote_and_switches_to_the_second(tmp_path):
    row = {"first": "K1", "second": "K2", "votes": 2}
    names = {"K1": "03-agents-and-memory", "K2": "10-evaluation"}
    assert _call(tmp_path, "suggestion", row, names, False) == {
        "target": "K1", "text": "03-agents-and-memory", "vote": "2/3"}
    assert _call(tmp_path, "suggestion", row, names, True) == {
        "target": "K2", "text": "10-evaluation", "vote": "2nd"}


def test_bring_in_names_papers_by_citekey_when_they_have_one(tmp_path):
    rows = [{"key": "8H4AC8GW", "citekey": "packerMemGPT2024"}, {"key": "AAAA1111", "citekey": ""}]
    assert _call(tmp_path, "bringCommand", rows) == \
        "/nucleate @packerMemGPT2024 zotero://select/library/items/AAAA1111"


@pytest.mark.parametrize("fm, label", [
    ({"authors": ["Packer, Charles"], "year": 2024, "zotero_key": "X"}, "Packer 2024"),
    ({"authors": "Xu, Wujiang", "year": 2025, "zotero_key": "X"}, "Xu 2025"),
    ({"title": "Untitled", "zotero_key": "X"}, "Untitled n.d."),
    ({"title": "A note"}, ""),
])
def test_a_paper_label_is_surname_and_year(tmp_path, fm, label):
    assert _call(tmp_path, "paperLabel", fm) == label


def test_the_paper_strip_lists_three_authors_then_et_al(tmp_path):
    fm = {"authors": ["Packer, Charles", "Wooders, Sarah", "Lin, Kevin", "Fang, Vivian"],
          "year": 2024, "venue": "arXiv", "zotero_key": "8H4AC8GW",
          "doi": "10.48550/arXiv.2310.08560", "pdf_key": "QH2BR53F"}
    s = _call(tmp_path, "paperStrip", fm)
    assert s["byline"] == "Packer, Wooders, Lin et al. · 2024 · arXiv"
    assert s["links"] == [
        ["Zotero", "zotero://select/library/items/8H4AC8GW"],
        ["PDF", "zotero://open-pdf/library/items/QH2BR53F"],
        ["DOI", "https://doi.org/10.48550/arXiv.2310.08560"],
    ]


def test_the_node_projection_passes_the_paper_through(tmp_path):
    from tests.test_explore_panel import _project_node
    paper = {"fm": {"zotero_key": "8H4AC8GW"}, "cites": [], "cited_by": [], "notes": [],
             "missing": {"count": 0, "top": []}, "collections": []}
    d = _project_node(tmp_path, {"path": "sources/@p.md"}, {"title": "MemGPT", "paper": paper})
    assert d["paper"] == paper
    assert _project_node(tmp_path, {"path": "a.md"}, {"title": "A"})["paper"] is None


def test_linked_from_leaves_out_the_notes_from_the_paper(tmp_path):
    """Every note distilled from a paper links its leaf, so `Linked from`
    repeated `Notes from this paper` row for row (12 of 12, e2e 2026-10-05).
    What is left is the one thing it adds: notes citing the paper from elsewhere."""
    from tests.test_explore_panel import _project_node
    paper = {"fm": {}, "cites": [], "cited_by": [], "missing": {"count": 0, "top": []},
             "collections": [], "notes": [{"path": "Notes/RRF.md"}]}
    rel = {"backlinks": [{"path": "Notes/RRF.md"}, {"path": "Notes/Mine.md"}]}

    d = _project_node(tmp_path, {"path": "sources/@p.md"}, {"paper": paper, "related": rel})

    assert [r["path"] for r in d["from"]] == ["Notes/Mine.md"]


def test_the_global_read_chip_label_does_not_call_itself(tmp_path):
    """In a browser a top-level `function chipLabel` IS `window.chipLabel`, so
    a wrapper assigned there called itself until the stack overflowed, and the
    exception took the whole turn footer with it, write cards included (e2e,
    2026-10-05). Run as a classic script, where that aliasing holds."""
    src = JS.read_text(encoding="utf-8")
    pure = src[src.index("// zotero-pure:start"):src.index("// zotero-pure:end")]
    wiring = src[src.index("window.fillZoteroScopes ="):src.index('fetch("/zotero/labels")')]
    script = tmp_path / "w.js"
    script.write_text(
        "const vm = require('vm');\n"
        "globalThis.window = globalThis;\n"
        f"vm.runInThisContext({json.dumps('function fillZoteroScopes() {}' + pure + wiring)});\n"
        "window.paperLabels = {'sources/@a.md': 'Smith 2024'};\n"
        "console.log(JSON.stringify(window.chipLabel('sources/@a.md')));\n")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-400:]
    assert json.loads(out.stdout) == "Smith 2024"


@pytest.mark.parametrize("href, action", [
    ("#fn1", "footnote"),
    ("#fnref1:2", "footnote"),
    ("zotero://select/library/items/8H4AC8GW", "scheme"),
    ("obsidian://open?vault=v&file=x.md", "scheme"),
    ("https://doi.org/10.1/x", "external"),
    ("#explore/graph", None),
    ("javascript:alert(1)", None),
    ("nota.md", None),
])
def test_what_a_link_in_a_note_does(tmp_path, href, action):
    assert _call(tmp_path, "linkAction", href) == action


def test_a_read_chip_names_a_paper_by_surname_and_year(tmp_path):
    labels = {"sources/@packer2024.md": "Packer 2024"}
    assert _call(tmp_path, "chipLabelFor", "sources/@packer2024.md", labels) == "Packer 2024"
    assert _call(tmp_path, "chipLabelFor", "Concepts/Paging.md", labels) == "Paging"


@pytest.mark.parametrize("value, scope", [
    ("collection:03-agents-and-memory", {"kind": "collection", "ids": ["03-agents-and-memory"], "label": "L"}),
    ("collections:", {"kind": "collection", "ids": ["01-retrieval", "12-temporal"], "all": True, "label": "L"}),
    ('collections:["01-retrieval","04-graphs"]',
     {"kind": "collection", "ids": ["01-retrieval", "04-graphs"], "label": "L"}),
    ("papers:", {"kind": "papers", "label": "L"}),
    ("folder:Concepts/", {"kind": "folder", "prefix": "Concepts/", "label": "L"}),
    ("zone:3", {"kind": "zone", "id": 3, "label": "L"}),
])
def test_a_scope_value_becomes_the_scope_the_frame_reads(tmp_path, value, scope):
    """A collection is named, not numbered: Number("03-agents") is NaN. Since
    ADR-0066 a collection scope is a set: one, the Library tab's pick, or all."""
    assert _call(tmp_path, "parseScope", value, "L", ["01-retrieval", "12-temporal"]) == scope
    assert _call(tmp_path, "scopeValue", scope) == value


def test_a_collection_reads_covered_of_total_once_a_pass_has_run(tmp_path):
    """ADR-0066 decision 5: "12-temporal · 0 of 12". Before any pass there is
    no verdict to count, so only the total; leaves sit inside the graph and
    are named apart."""
    cols = [{"name": "12-temporal", "size": 0, "outside": 12, "covered": 0},
            {"name": "11-security", "size": 2, "outside": 11, "covered": 4}]
    fresh = {"collections": cols, "rim": {"outside": 23, "covered": 4}, "covers": {"state": "fresh"}}
    assert _call(tmp_path, "zoteroScopeOptions", fresh, True) == [
        ["collections:", "All collections · 4 of 23"],
        ["collection:12-temporal", "12-temporal · 0 of 12"],
        ["collection:11-security", "11-security · 4 of 11 · 2 in vault"]]
    pending = {**fresh, "covers": {"state": "none"}}
    assert _call(tmp_path, "zoteroScopeOptions", pending, False) == [
        ["collection:12-temporal", "12-temporal · 12"],
        ["collection:11-security", "11-security · 11 · 2 in vault"]]


def test_the_chip_counts_the_pass_then_offers_the_rebuild(tmp_path):
    """ADR-0066 decision 6: it never rebuilds on its own, for the reason the
    "vault changed" chip does not: a rebuild loses the camera and the focus."""
    running = _call(tmp_path, "coversCue", {"running": True, "done": 120, "total": 271})
    assert running["text"] == "covers · 120 of 271 papers" and running["busy"] is True
    ready = _call(tmp_path, "coversCue", {"running": False, "ready": True})
    assert ready["text"] == "⟳ covers ready" and ready["busy"] is False
    assert _call(tmp_path, "coversCue", None) is None


def test_a_read_only_library_names_the_vault_that_holds_the_link(tmp_path):
    lib = {"total": 276, "in_vault": 0, "new": 0, "read_only": True, "holder": "/tmp/ze2e"}
    assert _call(tmp_path, "libraryHeader", lib) == "276 in Zotero · read only here · linked to /tmp/ze2e"
    assert _call(tmp_path, "libraryHeader", {**lib, "holder": ""}).endswith("no vault linked")


def test_the_collections_group_offers_papers_only_on_the_scope_picker_alone(tmp_path):
    groups = {"papers": 6, "collections": [{"name": "03-agents-and-memory", "size": 4}]}
    assert _call(tmp_path, "zoteroScopeOptions", groups, True) == [
        ["papers:", "Papers only · 6"], ["collection:03-agents-and-memory", "03-agents-and-memory · 4"]]
    assert _call(tmp_path, "zoteroScopeOptions", groups, False) == [
        ["collection:03-agents-and-memory", "03-agents-and-memory · 4"]]


def test_a_missing_paper_reaches_the_panel_with_its_reference(tmp_path):
    from tests.test_explore_panel import _project_node
    node = {"name": "arXiv 2303.08774", "ghost": False, "path": "",
            "quote": "OpenAI. GPT-4 technical report.", "href": "https://arxiv.org/abs/2303.08774"}
    d = _project_node(tmp_path, node, {"title": "arXiv 2303.08774"})
    assert d["cited"] == {"quote": "OpenAI. GPT-4 technical report.",
                          "href": "https://arxiv.org/abs/2303.08774"}
