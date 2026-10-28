# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scoping the graph to one part of the vault: a folder or a structural area (a
semantic zone too until ADR-0136). A visibility predicate in the frame beside
the focus dim, never a second build: positions stay global so the part is seen
where it sits."""
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"
FRAME_JS = WEB / "graph-frame.js"
EXPLORE_JS = WEB / "app-explore.js"


@pytest.fixture
def client(tmp_vault):
    _fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.ui.web import server

    server._reset_session()
    return TestClient(server.app), tmp_vault


def test_shape_no_longer_runs_the_semantic_partition(client):
    """ADR-0136 pruned every reader of the zones but the atlas's fallback and
    Zotero, so /shape ships the areas alone."""
    api, vault = client
    vault.note("Math/A.md", "[[B]]")
    vault.note("Math/B.md", "[[A]]")
    s = api.get("/shape").json()
    assert "zones" not in s and "zone_bridges" not in s
    assert all(set(n) == {"path", "size", "area"} for n in s["notes"])
    assert {n["path"] for n in s["notes"]} == {"Math/A.md", "Math/B.md"}


def test_the_frame_hides_what_the_scope_does_not_name():
    js = FRAME_JS.read_text()
    assert re.search(r"let SCOPE = null;", js)
    body = js[js.index("function inScope("):js.index("function computeFilters(")]
    assert '"folder"' in body and '"area"' in body and '"zone"' not in body
    assert "startsWith(SCOPE.prefix)" in body and "n.group === SCOPE.id" in body
    filt = js[js.index("function computeFilters("):js.index("function applyFilters(")]
    assert "!inScope(n)" in filt
    assert '"silica-scope"' in js


def test_the_legend_offers_the_scope_and_replays_it_on_reload():
    js = EXPLORE_JS.read_text()
    assert 'id = "graph-scope"' in js or 'id="graph-scope"' in js
    assert '"silica-scope"' in js and "function replayGraphScope()" in js
    load = js[js.index('$("#graph-frame").addEventListener("load"'):]
    load = load[:load.index("});")]
    assert "replayGraphScope()" in load
    for group in ("Folders", "Areas"):
        assert f'"{group}"' in js, f"the scope picker lists {group}"
    assert '"Zones"' not in js


def test_an_edge_goes_when_either_of_its_notes_is_hidden():
    """Both renderers filter links on linkVisibility alone and never look at
    the nodes', so scoping to a folder hid its outsiders and kept every line to
    them: 1385 wikilinks drawn around 171 visible notes. The edge pass reads
    the ends after the node pass (and after the highlight's far-end rule, which
    unhides the notes it keeps)."""
    js = FRAME_JS.read_text()
    filt = js[js.index("function computeFilters("):js.index("function applyFilters(")]
    edge_pass = filt[filt.rindex("RAW_EDGES.forEach(e => {"):]
    assert "NODE_BY_ID[e.from]" in edge_pass and "NODE_BY_ID[e.to]" in edge_pass
    assert "a._hidden" in edge_pass and "b._hidden" in edge_pass
    assert filt.index("n._hidden = ") < filt.rindex("RAW_EDGES.forEach(e => {")
