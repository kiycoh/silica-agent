# SPDX-License-Identifier: AGPL-3.0-or-later
"""Comparing two parts of the vault (folders, areas or zones): what
characterises each, and what puts them in touch. Terms by rate ratio with a
floor and a vault-wide ceiling; contact by wikilinks and k-NN pairs, never by
shared terms, which measured generic on the test vault twice ("dati",
"esempio", "può")."""
import re
from pathlib import Path

import pytest

from silica.kernel.recall.compare import compare_sets

WEB = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"


class _Store:
    """stem -> {note: count}; labels are the stems upper-cased."""
    def __init__(self, post):
        self._post = post
    def stem_postings(self):
        return self._post
    def node_label(self, stem):
        return stem.upper()


def test_terms_are_rated_against_the_other_side_with_a_floor_and_a_ceiling():
    a = {f"a{i}" for i in range(10)}
    b = {f"b{i}" for i in range(10)}
    post = {
        "stat": {**{f"a{i}": 1 for i in range(8)}, "b0": 1},        # A's word
        "code": {**{f"b{i}": 1 for i in range(7)}},                 # B's word
        "data": {**{f"a{i}": 1 for i in range(9)}, **{f"b{i}": 1 for i in range(9)}, "c0": 1, "c1": 1},  # everywhere: ceiling
        "rare": {"a0": 1},                                          # below the floor
    }
    out = compare_sets(a, b, store=_Store(post), vault_size=22, nodes=[], edges=[], sim=[], floor=0.15)
    assert out["a"]["terms"] == ["STAT"] and out["b"]["terms"] == ["CODE"]
    assert out["a"]["size"] == 10 and out["b"]["size"] == 10
    assert "shared" not in out


def test_contact_is_links_and_knn_pairs_counted_once():
    a, b = {"a0", "a1"}, {"b0", "b1"}
    E = lambda x, y, t="EXTRACTED": {"type": t, "from": x, "to": y}
    edges = [E("a0", "b0"), E("b0", "a0"), E("a1", "a0"), E("a1", "b1", "SIMILAR")]
    sim = [{"type": "SIMILAR", "from": "a1", "to": "b1"}, {"type": "SIMILAR", "from": "b1", "to": "a1"},
           {"type": "SIMILAR", "from": "a0", "to": "a1"}]
    out = compare_sets(a, b, store=_Store({}), vault_size=4, nodes=[], edges=edges, sim=sim)
    assert out["links"] == [("a0", "b0")]           # the mutual pair once, the inner link never
    assert out["knn"] == [("a1", "b1")]


@pytest.fixture
def client(tmp_vault):
    _fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.ui.web import server

    server._reset_session()
    return TestClient(server.app), tmp_vault


def test_compare_endpoint_takes_two_selectors_and_quotes_the_links(client):
    api, vault = client
    vault.note("Math/A.md", "[[B]]\n\nThe bound leans on [[Loop]] for its proof.\n")
    vault.note("Math/B.md", "[[A]]")
    vault.note("Code/Loop.md", "[[Stack]]")
    vault.note("Code/Stack.md", "[[Loop]]")
    d = api.get("/compare", params={"a": "folder:Math/", "b": "folder:Code/"}).json()
    assert d["a"]["label"] == "Math/" and d["a"]["size"] == 2 and d["b"]["size"] == 2
    assert [(l["a"]["name"], l["b"]["name"], l["passage"]) for l in d["links"]] == [
        ("A", "Loop", "The bound leans on Loop for its proof.")]
    assert d["knn"] == [] and isinstance(d["a"]["terms"], list)
    bad = api.get("/compare", params={"a": "folder:Math/", "b": "moon:1"}).json()
    assert "error" in bad


def test_the_frame_and_the_legend_carry_the_comparison():
    js = (WEB / "graph-frame.js").read_text()
    assert re.search(r"let AB = null;", js) and '"silica-compare"' in js
    filt = js[js.index("function computeFilters("):js.index("function applyFilters(")]
    assert "AB.a.has(n.id) || AB.b.has(n.id)" in filt
    assert "l._ab = " in js[js.index('"silica-compare"'):]
    paint = js[js.index("function linkPaint("):js.index("// --- 3D: every link")]
    assert "l._ab" in paint
    ex = (WEB / "app-explore.js").read_text()
    assert 'id = "graph-compare"' in ex and '"/compare?a="' in ex and 'new CustomEvent("silica:compare"' in ex
    work = (WEB / "work.js").read_text()
    assert 'addEventListener("silica:compare"' in work and "function renderCompare()" in work
