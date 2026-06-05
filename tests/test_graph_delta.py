"""Tests for the co-occurrence vs wikilink DELTA report (graph_report.py).

The delta is the "hidden advantage": comparing the deterministic co-occurrence
graph against the authoritative wikilink graph yields an autonomous work plan,
computed with zero embedding tokens.

  - co-occurrence − wikilink  -> AUTOLINK candidates (related in text, unlinked)
  - wikilink − co-occurrence   -> STALE links (linked, but no textual co-presence)
  - high cooccur centrality + no hub note -> MISSING HUB (next note to create)
"""
from __future__ import annotations

import pytest

from silica.kernel.cooccurrence import CooccurStore, build_contribution
from silica.kernel.graph_report import (
    AutolinkCandidate,
    MissingHub,
    StaleLink,
    VaultReport,
    compute_report,
    to_markdown,
    _compute_cooccur_delta,
)


# ---------------------------------------------------------------------------
# Synthetic vault: wikilink structure + matching co-occurrence corpus.
#
# Wikilinks (EXTRACTED):  A↔B, B↔C, A↔C, D↔E, C→D ;  F orphan (ghost)
# Co-occurrence corpus (note body text, keyed by the SAME ids):
#   A: neural network        E: neural network    -> A,E share concepts, UNLINKED (3 hops)
#   B: beta cooking          C: beta cooking       -> B,C share concepts, and ARE linked
#   D: sailing boat          F: isolated
# So:
#   AUTOLINK: A–E (related in text, not wikilinked)
#   STALE:    A–B, A–C, C–D, D–E (linked, no shared concepts); NOT B–C (shared)
#   MISSING HUB: "neural"/"network"/"cooking" are central but have no note titled
#                so; "beta" is NOT missing (note B is titled "Beta").
# ---------------------------------------------------------------------------


def _make_node(nid, label, group, note_type="note"):
    return {"id": nid, "label": label, "group": group, "type": note_type}


def _make_edge(eid, src, dst, edge_type="EXTRACTED"):
    return {"id": eid, "from": src, "to": dst, "type": edge_type}


@pytest.fixture()
def synthetic_graph():
    nodes = [
        _make_node("A", "Alpha",   0),
        _make_node("B", "Beta",    0),
        _make_node("C", "Gamma",   0),
        _make_node("D", "Delta",   1),
        _make_node("E", "Epsilon", 1),
        _make_node("F", "Phi",    -1),
        {"id": "__unresolved__Ghost", "label": "Ghost", "group": -1, "type": "ghost"},
    ]
    edges = [
        _make_edge("e0", "A", "B"),
        _make_edge("e1", "B", "C"),
        _make_edge("e2", "A", "C"),
        _make_edge("e3", "D", "E"),
        _make_edge("e4", "C", "D"),
        _make_edge("e5", "F", "__unresolved__Ghost", "AMBIGUOUS"),
    ]
    return nodes, edges


@pytest.fixture()
def cooccur_store(tmp_path):
    st = CooccurStore(path=tmp_path / "c.json", lang="english")
    st.upsert_note("A", build_contribution("A", "neural network architecture"))
    st.upsert_note("B", build_contribution("B", "beta cooking pasta"))
    st.upsert_note("C", build_contribution("C", "beta cooking pizza"))
    st.upsert_note("D", build_contribution("D", "sailing boat harbour"))
    st.upsert_note("E", build_contribution("E", "neural network training"))
    st.upsert_note("F", build_contribution("F", "isolated lonely topic"))
    return st


@pytest.fixture()
def delta_report(synthetic_graph, cooccur_store):
    nodes, edges = synthetic_graph
    return compute_report(
        _nodes_edges_override=(nodes, edges),
        with_cooccurrence=True,
        _cooccur_store_override=cooccur_store,
    )


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_delta_absent_by_default(synthetic_graph, cooccur_store):
    nodes, edges = synthetic_graph
    r = compute_report(_nodes_edges_override=(nodes, edges))
    assert r.autolink_candidates == []
    assert r.stale_links == []
    assert r.missing_hubs == []


def test_delta_empty_store_no_exception(synthetic_graph, tmp_path):
    nodes, edges = synthetic_graph
    empty = CooccurStore(path=tmp_path / "empty.json")
    r = compute_report(
        _nodes_edges_override=(nodes, edges),
        with_cooccurrence=True,
        _cooccur_store_override=empty,
    )
    assert r.autolink_candidates == []
    assert r.stale_links == []
    assert r.missing_hubs == []


# ---------------------------------------------------------------------------
# co-occurrence − wikilink  ->  AUTOLINK candidates
# ---------------------------------------------------------------------------

def test_autolink_proposes_unlinked_text_related_pair(delta_report):
    pairs = {(a.source, a.target) for a in delta_report.autolink_candidates}
    assert ("A", "E") in pairs or ("E", "A") in pairs


def test_autolink_carries_shared_concept_evidence(delta_report):
    cand = next(
        a for a in delta_report.autolink_candidates
        if {a.source, a.target} == {"A", "E"}
    )
    assert any("neural" in s or "network" in s for s in cand.shared)
    assert cand.weight > 0


def test_autolink_excludes_already_wikilinked_pairs(delta_report):
    pairs = {frozenset((a.source, a.target)) for a in delta_report.autolink_candidates}
    # B and C share concepts but are already wikilinked -> never an autolink
    assert frozenset(("B", "C")) not in pairs


# ---------------------------------------------------------------------------
# wikilink − co-occurrence  ->  STALE links
# ---------------------------------------------------------------------------

def test_stale_flags_wikilink_without_shared_concepts(delta_report):
    pairs = {frozenset((s.source, s.target)) for s in delta_report.stale_links}
    assert frozenset(("A", "B")) in pairs   # linked, but neural vs cooking: no overlap


def test_stale_excludes_wikilink_with_shared_concepts(delta_report):
    pairs = {frozenset((s.source, s.target)) for s in delta_report.stale_links}
    assert frozenset(("B", "C")) not in pairs   # linked AND share "beta cooking"


# ---------------------------------------------------------------------------
# high cooccur centrality + no hub note  ->  MISSING HUB
# ---------------------------------------------------------------------------

def test_missing_hub_surfaces_central_unhubbed_concept(delta_report):
    concepts = {h.concept for h in delta_report.missing_hubs}
    assert any(c in concepts for c in ("neural", "network"))


def test_missing_hub_excludes_concept_with_a_titled_note(delta_report):
    # note B is titled "Beta", so the concept "beta" is already formalised
    concepts = {h.concept for h in delta_report.missing_hubs}
    assert "beta" not in concepts


def test_missing_hubs_sorted_by_centrality_desc(delta_report):
    cents = [h.centrality for h in delta_report.missing_hubs]
    assert cents == sorted(cents, reverse=True)


# ---------------------------------------------------------------------------
# Unit: _compute_cooccur_delta is injectable and returns three lists
# ---------------------------------------------------------------------------

def test_compute_cooccur_delta_returns_three_lists(synthetic_graph, cooccur_store):
    import networkx as nx
    nodes, edges = synthetic_graph
    real_ids = {n["id"] for n in nodes if n.get("type") != "ghost"}
    G = nx.Graph()
    G.add_nodes_from(real_ids)
    for e in edges:
        if e["type"] == "EXTRACTED":
            G.add_edge(e["from"], e["to"])
    node_label = {n["id"]: n["label"] for n in nodes if n.get("type") != "ghost"}
    report = VaultReport(
        generated_at="", scope="", totals={}, god_nodes=[], bridges=[],
        orphans=[], dangling=[], clusters=[],
    )
    al, sl, mh = _compute_cooccur_delta(
        report, G, node_label, cooccur_store=cooccur_store, k=10
    )
    assert all(isinstance(x, AutolinkCandidate) for x in al)
    assert all(isinstance(x, StaleLink) for x in sl)
    assert all(isinstance(x, MissingHub) for x in mh)


# ---------------------------------------------------------------------------
# Output / totals
# ---------------------------------------------------------------------------

def test_totals_include_delta_counts(delta_report):
    assert "autolink_candidates" in delta_report.totals
    assert "stale_links" in delta_report.totals
    assert "missing_hubs" in delta_report.totals


def test_markdown_renders_delta_sections(delta_report):
    md = to_markdown(delta_report)
    assert "Autolink" in md
    assert "Stale" in md
    assert "Hub" in md  # "Missing Hubs" section header


def test_markdown_omits_delta_sections_when_empty(synthetic_graph):
    nodes, edges = synthetic_graph
    r = compute_report(_nodes_edges_override=(nodes, edges))
    md = to_markdown(r)
    assert "Autolink Candidates" not in md
    assert "Stale Links" not in md


# ---------------------------------------------------------------------------
# Tool surface: the delta is reachable via silica_vault_report
# ---------------------------------------------------------------------------

def test_vault_report_tool_exposes_with_cooccurrence_flag():
    from silica.tools.composed import VaultReportArgs
    args = VaultReportArgs()
    assert args.with_cooccurrence is False  # default off, opt-in like with_embeddings


def test_delta_report_json_serializable(delta_report, tmp_path):
    import dataclasses
    import orjson
    from silica.kernel.graph_report import write_report
    paths = write_report(delta_report, str(tmp_path / "GRAPH_REPORT.md"))
    data = orjson.loads((tmp_path / "GRAPH_REPORT.json").read_bytes())
    # nested delta dataclasses survive the asdict -> orjson round-trip
    assert "autolink_candidates" in data
    assert isinstance(data["autolink_candidates"], list)
    assert dataclasses.asdict(delta_report)["stale_links"] == data["stale_links"]
