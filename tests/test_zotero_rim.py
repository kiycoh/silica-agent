# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0066: every vault reads the library, the link decides who writes, and a
collection's papers outside the vault sit on a rim, met by a note only through
COVERS (nearest 5 and a hypergeometric test, Bonferroni).

The frame's drawing is verified by opening it; what is pinned here is the
adapter's refusals, the verdicts, what the server hands the frame and the
Library, and the frame's pure rim layout run under node.
"""
from __future__ import annotations

import json
import math
import shutil

import pytest

from silica.config import CONFIG
from silica.kernel.vault_manifest import load_manifest, set_zotero
from silica.sources import zotero as z
from silica.sources import zotero_covers as zc
from silica.sources import zotero_write as zw
from tests.test_zotero_graph_frame import _island, _pure
from tests import test_zotero_web as _web
from tests.zotero_fake import SERVER_ID, FakeZotero

# The Settings/Library fixtures, shared rather than copied: one fake Zotero.
fake, web = _web.fake, _web.web
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
LEAF = "@packerMemGPTLLMsOperating2024.md"


@pytest.fixture
def holder(tmp_path, monkeypatch):
    """Another vault on this machine that holds the library's link."""
    other = tmp_path / "linked"
    other.mkdir()
    set_zotero(other, "library", SERVER_ID)
    monkeypatch.setattr("silica.kernel.recall.vault_registry.known_vaults",
                        lambda: [other, __import__("pathlib").Path(CONFIG.vault_path)])
    return other


# --- decision 1: the link decides who writes -------------------------------------

def test_an_unlinked_vault_cannot_import_a_paper_another_vault_holds(fake, holder):
    with pytest.raises(z.ZoteroError, match="linked to"):
        z.ZOTERO.read("@packerMemGPTLLMsOperating2024")


def test_with_no_vault_linked_an_import_runs_as_it_always_did(fake, monkeypatch):
    monkeypatch.setattr("silica.kernel.recall.vault_registry.known_vaults", lambda: [])
    item = z.ZOTERO.read("@packerMemGPTLLMsOperating2024")
    assert item.meta["paper"].key == "8H4AC8GW"


def test_groups_are_never_written_from_an_unlinked_vault(fake, monkeypatch):
    monkeypatch.setenv("SILICA_ZOTERO_API_KEY", "k" * 32)
    zot = z.Zotero(api_key="k" * 32, transport=fake.transport())
    with pytest.raises(z.ZoteroError, match="not linked"):
        zw.write_groups(zot, {"g": ["8H4AC8GW"]}, run_id="r")
    assert not [c for c in fake.collections.values() if c["data"]["name"] == zw.GROUPS_NAME]
    assert load_manifest(CONFIG.vault_path).zotero is None


def test_a_read_only_library_names_the_holder_and_peeks_its_leaf(fake, holder, web):
    col = fake.collection("03-agents-and-memory")
    fake.items["8H4AC8GW"]["data"]["collections"] = [col["key"]]
    (holder / "sources").mkdir()
    (holder / "sources" / LEAF).write_text(
        "---\nzotero_key: 8H4AC8GW\n---\n\n# MemGPT\n\nthe leaf in the linked vault\n", encoding="utf-8")

    lib = web.get("/zotero/library").json()
    [row] = web.get(f"/zotero/collection?key={col['key']}").json()["rows"]
    seen = web.get("/zotero/peek?key=8H4AC8GW").json()

    assert lib["read_only"] is True and lib["holder"] == str(holder)
    assert row["state"] == "there" and row["leaf"] == ""
    assert seen["vault"] == str(holder) and "the leaf in the linked vault" in seen["text"]


# --- decision 4: the verdicts ------------------------------------------------------

def test_the_tail_is_the_hypergeometric_upper_tail():
    assert zc.tail(1, 1, 1, 2) == 0.5
    assert math.isclose(zc.tail(4, 4, 4, 40), 1 / math.comb(40, 4))
    assert zc.tail(0, 5, 3, 10) == pytest.approx(1.0)


def test_a_note_covers_a_paper_when_its_collection_meets_it_beyond_chance():
    """Four papers of A meet x and nobody else does: 4/4, kept. Every paper
    meets the hub: k/K is the collection's share of the library and the test
    drops it, which is what nearest alone could not do."""
    hits = {f"p{i}": ["hub", "x"] for i in range(4)}
    hits.update({f"p{i}": ["hub", f"n{i}"] for i in range(4, 40)})
    members = {"A": [f"p{i}" for i in range(4)], "B": [f"p{i}" for i in range(4, 40)]}

    v = zc.verdicts(hits, members, {"A": "01-a", "B": "02-b"})

    assert v["tests"] == 2 + 37 and v["papers"] == 40
    assert v["covers"] == {f"p{i}": [["x", "01-a", 4, 4]] for i in range(4)}


def test_the_query_is_the_title_and_abstract_cut_as_one_string():
    assert zc.query_of("T", "a" * 1000) == ("T. " + "a" * 1000)[:600]
    assert zc.query_of("T", "") == "T. "


def test_nearest_drops_the_pipelines_staging_too(monkeypatch, tmp_vault):
    """An archived segment of a nucleated paper covered 33 of a collection's
    37 papers under its segment file name (2026-10-26): Done/ and the inbox
    are the pipeline's, not notes."""
    def search(query, k):
        return ["Done/Paper/Paper-1-intro.md", "Inbox/Other/Other-1-a.md", "N1.md", "N2.md", "N3.md",
                "N4.md", "N5.md"][:k]

    monkeypatch.setattr(zc, "_search", search)
    assert zc._nearest("q", lambda p: p) == ["N1.md", "N2.md", "N3.md", "N4.md", "N5.md"]


def test_nearest_drops_paper_leaves_and_asks_again(monkeypatch):
    asked = []

    def search(query, k):
        asked.append(k)
        paths = ["sources/@a.md", "sources/@b.md", "N1.md", "N2.md", "N3.md"]
        if k > 5:
            paths += ["N4.md", "N5.md", "N6.md"]
        return paths[:k]

    monkeypatch.setattr(zc, "_search", search)
    assert zc._nearest("q", lambda p: p) == ["N1.md", "N2.md", "N3.md", "N4.md", "N5.md"]
    assert asked == [5, 20], "the measured k=5 first"


def test_the_search_is_this_vaults_alone(monkeypatch):
    """The memory lane is another vault: the relation drawn is this one's."""
    import silica.tools.graph as graph_tools

    seen = {}

    class Up:
        def embed(self, texts, query=False):
            return [[1.0]]

    monkeypatch.setattr("silica.agent.providers.get_embedder", lambda cfg: Up())

    def facade(query, k, memory, folder):
        seen.update(k=k, memory=memory)
        return {"results": [{"path": "N1.md"}]}

    monkeypatch.setattr(graph_tools, "_facade_search", facade)
    assert zc._search("q", 5) == ["N1.md"] and seen == {"k": 5, "memory": False}


def test_a_search_without_the_embedder_stops_the_pass(monkeypatch):
    """The search answers from co-occurrence alone when the embedder is down,
    which is another search: 86 covered papers became 27 that way."""
    class Down:
        def embed(self, texts, query=False):
            raise ConnectionError("Connection error.")

    monkeypatch.setattr("silica.agent.providers.get_embedder", lambda cfg: Down())
    with pytest.raises(RuntimeError, match="embedder stopped answering"):
        zc._search("q", 5)


def test_a_pass_reads_the_library_searches_once_per_paper_and_keeps_its_versions(
        tmp_vault, monkeypatch):
    f = FakeZotero()
    item, _ = f.add_paper()
    temporal = f.collection("12-temporal")
    f.clone(item, "PPPP0001", collections=[temporal["key"]], title="One")
    f.clone(item, "PPPP0002", collections=[temporal["key"]], title="Two")
    f.items["8H4AC8GW"]["data"]["collections"] = []
    monkeypatch.setattr(z, "client", lambda: z.Zotero(transport=f.transport()))
    monkeypatch.setattr("silica.kernel.recall.mindmap.note_resolver", lambda: (lambda p: p))
    monkeypatch.setattr("silica.kernel.recall.sync.vault_version", lambda: "v1")
    searched = []

    def search(query, k):
        searched.append(query)
        return ["Concepts/Time.md"]

    monkeypatch.setattr(zc, "_search", search)

    zc._pass(CONFIG.vault_path)
    cache = zc.cached(CONFIG.vault_path)

    assert sorted(q[:5] for q in searched) == ["One. ", "Two. "], "a paper in no collection is not searched"
    assert all(len(q) <= zc.QUERY_CHARS for q in searched)
    assert cache["library_version"] == f.library_version and cache["vault_version"] == "v1"
    assert cache["papers"] == 2 and cache["k"] == zc.K_NEAREST
    assert zc.freshness(cache, f.library_version, "v1") == "fresh"
    assert zc.freshness(cache, f.library_version + 1, "v1") == "stale"
    assert zc.freshness(None, 1, "v1") == "none"
    assert zc.status()["running"] is False and zc.status()["error"] == ""


# --- decisions 2, 3, 5: what the frame is handed ----------------------------------

def _library_for_the_frame(vault, monkeypatch):
    import silica.ui.web.graph_view as gv

    monkeypatch.setattr(gv, "_vendored_lib_js", lambda: "/*JS*/")
    monkeypatch.setattr("silica.kernel.recall.sync.vault_version", lambda: "v1")
    f = FakeZotero()
    item, _ = f.add_paper()
    temporal = f.collection("12-temporal")
    f.items["8H4AC8GW"]["data"]["collections"] = [temporal["key"]]
    f.clone(item, "OUTS0001", collections=[temporal["key"]], citationKey="out2025",
            creators=[{"lastName": "Outer", "creatorType": "author"}])
    f.clone(item, "LOOSE001", collections=[], citationKey="loose")
    monkeypatch.setattr(z, "client", lambda: z.Zotero(transport=f.transport()))
    vault.note("sources/" + LEAF, "---\nzotero_key: 8H4AC8GW\ntitle: \"MemGPT\"\n---\n\n# MemGPT\n")
    vault.note("Concepts/Memory.md", "# Memory\n\n[[Concepts/Paging]]\n")
    vault.note("Concepts/Paging.md", "# Paging\n")
    return f


def test_a_paper_with_no_leaf_here_is_an_outside_paper_joined_after_the_counts(
        tmp_vault, monkeypatch, tmp_path):
    import silica.ui.web.graph_view as gv

    f = _library_for_the_frame(tmp_vault, monkeypatch)
    zc._cache_path(CONFIG.vault_path).parent.mkdir(parents=True, exist_ok=True)
    zc._cache_path(CONFIG.vault_path).write_text(json.dumps({
        "library_version": f.library_version, "vault_version": "v1", "k": zc.K_NEAREST,
        "chars": zc.QUERY_CHARS, "papers": 2,
        "covers": {"OUTS0001": [["Concepts/Memory.md", "12-temporal", 2, 2],
                                ["Gone.md", "12-temporal", 2, 2]]}}))

    res = gv.export_graph(output_path=str(tmp_path / "g.html"))
    html = (tmp_path / "g.html").read_text()
    data = _island(html)
    by_id = {n["id"]: n for n in data["nodes"]}

    out = by_id["zotero:OUTS0001"]
    assert out["type"] == "outside" and out["label"] == "Outer 2024" and out["covered"] is True
    assert out["paper"]["collections"] == ["12-temporal"]
    assert by_id["sources/" + LEAF]["type"] == "paper", "a leaf here stays inside the layout"
    assert "zotero:LOOSE001" not in by_id, "a paper in no collection can never be scoped"
    assert [e for e in data["edges"] if e["type"] == "COVERS"] == [
        {"from": "zotero:OUTS0001", "to": "Concepts/Memory.md", "type": "COVERS", "width": 1.1,
         "color": {"opacity": 1.0}}], "a note deleted since the pass draws no line"
    assert data["covers"]["state"] == "fresh"
    assert res["nodes"] == 3, "the outside paper is no note of this vault"
    assert 'id="covers-row"' in html and 'id="rim-row"' in html and 'id="uncovered-row"' in html


def test_before_any_pass_the_rim_has_no_verdict(tmp_vault, monkeypatch, tmp_path):
    import silica.ui.web.graph_view as gv

    _library_for_the_frame(tmp_vault, monkeypatch)
    gv.export_graph(output_path=str(tmp_path / "g.html"))
    data = _island((tmp_path / "g.html").read_text())

    [out] = [n for n in data["nodes"] if n["type"] == "outside"]
    assert out["covered"] is None and data["covers"]["state"] == "none"
    assert not [e for e in data["edges"] if e["type"] == "COVERS"]


def test_a_closed_zotero_draws_no_rim(tmp_vault, monkeypatch, tmp_path):
    """conftest points the client at a closed port."""
    import silica.ui.web.graph_view as gv

    monkeypatch.setattr(gv, "_vendored_lib_js", lambda: "/*JS*/")
    tmp_vault.note("Concepts/Memory.md", "# Memory\n")
    gv.export_graph(output_path=str(tmp_path / "g.html"))
    html = (tmp_path / "g.html").read_text()

    assert not [n for n in _island(html)["nodes"] if n.get("type") == "outside"]
    assert 'id="covers-row"' not in html


# --- the frame's rim, run ------------------------------------------------------------

@needs_node
def test_the_rim_runs_clockwise_in_menu_order_covered_first(tmp_path):
    got = _pure(tmp_path, """
      const p = (id, cols, covered) => ({id, label: id, paper: {collections: cols},
                                         covers: covered ? [["n", cols[0], 1, 1]] : []});
      const papers = [p("b1", ["12-temporal"], false), p("a1", ["01-retrieval"], false),
                      p("a2", ["01-retrieval"], true), p("both", ["12-temporal", "01-retrieval"], false),
                      p("off", ["99-other"], true)];
      const out = rimLayout(papers, ["12-temporal", "01-retrieval"]);
      console.log(JSON.stringify({at: out.at, arcs: out.arcs,
        one: rimLayout([papers[0]], ["12-temporal"]).arcs,
        labels: [arcLabel(out.arcs[0], true), arcLabel(out.arcs[0], false)]}));
    """)
    arcs, at = got["arcs"], got["at"]
    assert [a["name"] for a in arcs] == ["01-retrieval", "12-temporal"], "the menu's order"
    assert [(a["n"], a["covered"]) for a in arcs] == [(3, 1), (1, 0)], \
        "a paper in two scoped collections sits in the first of them"
    assert "off" not in at
    assert at["a2"] < at["a1"], "covered papers first, so the lines leave from one end"
    assert arcs[0]["a0"] == 0 and math.isclose(arcs[1]["a0"], arcs[0]["a1"] + 0.05)
    assert math.isclose(arcs[1]["a1"] + 0.05, 2 * math.pi)
    assert got["one"][0]["a1"] == pytest.approx(2 * math.pi), "one arc has no gap to leave"
    assert got["labels"] == ["01-retrieval · 3", "01-retrieval · 1 of 3"]


@needs_node
def test_the_scope_reads_a_set_and_the_rim_paper_says_its_collections(tmp_path):
    got = _pure(tmp_path, """
      const out = {type: "outside", paper: {collections: ["12-temporal"], title: "T"},
                   covers: [], covered: false};
      const leaf = {type: "paper", paper: {collections: ["01-retrieval"]}};
      const under = (scope, node) => { SCOPE = scope; return inScope(node); };
      const both = {kind: "collection", ids: ["01-retrieval", "12-temporal"]};
      console.log(JSON.stringify([
        under(both, out), under(both, leaf), under({kind: "collection", ids: ["01-retrieval"]}, out),
        under({kind: "papers"}, out), paperTip(out),
        paperTip({...out, covered: null}),
      ]));
    """.replace("const out =", "let SCOPE = null;\n      const out ="), "inScope")
    assert got[:4] == [True, True, False, False], "Papers only is the citation graph, never the rim"
    assert "12-temporal" in got[4] and "no note here covers it" in got[4]
    assert "not computed yet" in got[5]


def test_the_forces_never_see_the_rim():
    """ADR-0066 decision 3: pinned, and moving no note. d3's link force splits
    each pull by the degree of its two ends, so a COVERS edge has to stay out
    of it, not merely weigh zero."""
    from tests.test_zotero_graph_frame import _js

    js = _js()
    assert "G.graphData({ nodes: ALL_NODES, links: ALL_EDGES })" in js
    assert "const ALL_NODES = RAW_NODES.concat(RIM_NODES)" in js, "the rim rides at the end"
    wrap = js[js.index("function rimless("):js.index("function buildGraph(")]
    assert "nodes.filter(n => !isRim(n))" in wrap and 'l.type !== "COVERS"' in wrap
    assert 'for (const name of ["link", "charge", "center"]) G.d3Force(name, rimless(' in js


def test_the_panel_carries_a_rim_paper_and_its_abstract(tmp_path):
    from tests.test_explore_panel import _project_node

    if shutil.which("node") is None:
        pytest.skip("needs node")
    o = {"key": "OUTS0001", "title": "T", "collections": ["12-temporal"], "covered": True,
         "covers": [{"path": "Concepts/Memory.md", "name": "Memory", "collection": "12-temporal",
                     "k": 4, "K": 6}]}
    d = _project_node(tmp_path, {"name": "Outer 2024", "path": "", "outside": o, "links": None},
                      {"title": "T", "item": {"abstract": "A."}})
    assert d["outside"]["covers"][0]["k"] == 4 and d["outside"]["item"]["abstract"] == "A."
    assert d["count"] == "" and d["title"] == "T"


def test_the_vault_stays_around_the_rim_faint_and_unnamed():
    """ADR-0066 amendment: under a collection scope the notes no paper reaches
    stay, as the mock drew them, and keep only their wikilinks; what a row of
    the key switched off stays off. A context note is a dot with no name."""
    from tests.test_zotero_graph_frame import _js

    js = _js()
    flt = js[js.index("function computeFilters("):js.index("function syncRimKey(")]
    assert "if (n._hidden && !toggledOff(n)) { n._hidden = false; n._ctx = true; }" in flt
    assert 'e._ctx = rimOn && beyond && !off && (e.type === "EXTRACTED" || e.type === "AMBIGUOUS")' in flt
    paint = js[js.index("function linkPaint("):js.index("// --- 3D: every link in ONE LineSegments")]
    assert '(l._ctx ? "x" : "")' in paint, "a cached paint would outlive the scope"
    draw = js[js.index("function drawNode("):js.index("function drawNodeLabel(")]
    assert "n._dim || n._ctx || scale < 0.6" in draw
