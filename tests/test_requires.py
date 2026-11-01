# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A written `requires:` (ADR-0145): parsed from the frontmatter, resolved to one
note or skipped, put over RefD's prerequisites, walked by /path, named in the
recall header, and written by silica_require with a restore point."""
from __future__ import annotations

import networkx as nx
import pytest

from silica.kernel.recall.requires import resolve
from silica.kernel.report.learner import with_written
from silica.kernel.write.frontmatter import requires_in


def test_requires_in_reads_every_spelling_of_a_link_once():
    assert requires_in({"requires": ["[[Gradient descent]]", "[[Loss|the loss]]",
                                     "[[Chain rule#Proof]]", "Bare name", "[[Loss]]"]}) == [
        "Gradient descent", "Loss", "Chain rule", "Bare name"]
    assert requires_in({"requires": [["Unquoted"]]}) == ["Unquoted"]  # requires: [[Unquoted]]
    assert requires_in({"requires": [[["Block"]]]}) == ["Block"]      # - [[Block]]
    assert requires_in({"requires": "[[One]]"}) == ["One"]
    assert requires_in({"requires": 3}) == [] and requires_in(None) == []


def test_a_name_resolves_by_path_name_or_alias_and_a_shared_name_is_skipped():
    paths = ["ML/Backprop.md", "Math/Chain rule.md", "ML/Gradient descent.md", "A/Intro.md",
             "B/Intro.md", "ML/Adam.md"]
    requires = {"ML/Backprop.md": ["Chain rule", "GD", "Intro", "Nowhere", "Backprop"],
                "ML/Adam.md": ["ML/Gradient descent"]}
    assert resolve(requires, {"ML/Gradient descent.md": ["GD"]}, paths) == {
        "ML/Backprop.md": ["Math/Chain rule.md", "ML/Gradient descent.md"],
        "ML/Adam.md": ["ML/Gradient descent.md"],
    }


def test_the_written_list_replaces_the_inferred_one_and_drops_the_reverse_edge():
    inferred = {"backprop": ["adam"], "chain rule": ["backprop"], "adam": ["sgd"]}
    out = with_written(inferred, {"backprop": ["chain rule"]})
    assert out == {"backprop": ["chain rule"], "adam": ["sgd"]}
    assert with_written(inferred, {}) is inferred


def test_prerequisites_map_carries_what_the_person_wrote(tmp_vault, monkeypatch):
    from silica.kernel.recall.cooccurrence import cooccur_key
    from silica.kernel.report import learner

    tmp_vault.note("ML/Backprop.md", '---\nrequires: ["[[Chain rule]]"]\n---\n\n# Backprop\n')
    tmp_vault.note("Math/Chain rule.md", "# Chain rule\n")
    learner._prereq_memo.clear()
    got = learner.prerequisites_map()
    assert got[cooccur_key("ML/Backprop.md")] == [cooccur_key("Math/Chain rule.md")]


def test_reading_path_prefers_and_names_a_written_prerequisite():
    from silica.kernel.recall.mindmap import reading_path

    g = nx.Graph()
    g.add_edges_from([("a.md", "c.md"), ("c.md", "b.md")])
    assert [leg for _n, leg in reading_path("a.md", "b.md", graph=g, cooccur_store=None,
                                            weighted=True)] == ["start", "wikilink", "wikilink"]
    path = reading_path("a.md", "b.md", graph=g, cooccur_store=None, weighted=True,
                        requires={"b.md": ["a.md"]})
    assert path == [("a.md", "start"), ("b.md", "required by")]
    back = reading_path("b.md", "a.md", graph=g, cooccur_store=None, weighted=True,
                        requires={"b.md": ["a.md"]})
    assert back == [("b.md", "start"), ("a.md", "requires")]


def test_the_recall_header_says_what_a_note_builds_on(tmp_vault):
    from silica.kernel.recall.perception import NoteBlock, Perception, _read_dated_body

    tmp_vault.note("ML/Backprop.md", '---\nrequires: ["[[Chain rule]]", "[[Gradient descent]]"]\n---\n\nbody\n')
    *_rest, requires = _read_dated_body("ML/Backprop")
    assert requires == ["Chain rule", "Gradient descent"]
    p = Perception(query="q", blocks=[NoteBlock(path="ML/Backprop", date="", evidence="", body="body",
                                                excerpt="body", builds_on=", ".join(requires))])
    assert "| builds-on: Chain rule, Gradient descent" in p.render()


# -- silica_require -------------------------------------------------------------

@pytest.fixture
def vault(tmp_vault):
    tmp_vault.note("ML/Backprop.md", "---\ntags: [ml]\n---\n\n# Backprop\n\nbody\n")
    tmp_vault.note("Math/Chain rule.md", "# Chain rule\n")
    tmp_vault.note("A/Intro.md", "# Intro\n")
    tmp_vault.note("B/Intro.md", "# Intro\n")
    return tmp_vault


def _requires(rel: str) -> list[str]:
    from pathlib import Path

    from silica.config import CONFIG
    from silica.kernel.write import frontmatter

    data, _raw, _body = frontmatter.split((Path(CONFIG.vault_path) / rel).read_text())
    return frontmatter.requires_in(data)


def test_silica_require_writes_one_entry_once_and_undo_takes_it_back(vault):
    from silica.cli import _handle_direct_shortcut
    from silica.tools.notes import silica_require

    res = silica_require("ML/Backprop", "Chain rule")
    assert res["requires"] == ["Chain rule"] and res["checkpoint_ok"]
    assert _requires("ML/Backprop.md") == ["Chain rule"]
    assert silica_require("ML/Backprop", "Math/Chain rule")["changed"] is False
    # a name two notes share is written as the path
    assert silica_require("ML/Backprop", "A/Intro")["requires"] == ["Chain rule", "A/Intro"]

    _handle_direct_shortcut("/undo ML/Backprop.md", [])
    assert _requires("ML/Backprop.md") == ["Chain rule"]


def test_silica_require_clears_the_key_with_its_last_entry_and_refuses_itself(vault):
    from silica.tools.notes import silica_require

    silica_require("ML/Backprop", "Chain rule")
    assert silica_require("ML/Backprop", "Chain rule", clear=True)["requires"] == []
    from pathlib import Path

    from silica.config import CONFIG
    assert "requires" not in (Path(CONFIG.vault_path) / "ML/Backprop.md").read_text()
    assert "error" in silica_require("ML/Backprop", "ML/Backprop")
    assert "error" in silica_require("ML/Backprop", "Nowhere")


def test_an_entry_already_there_is_no_write_wherever_it_sits(vault):
    from silica.tools.notes import silica_require

    silica_require("ML/Backprop", "Chain rule")
    silica_require("ML/Backprop", "A/Intro")
    assert silica_require("ML/Backprop", "Chain rule")["changed"] is False
    assert _requires("ML/Backprop.md") == ["Chain rule", "A/Intro"]


# -- Explore (B2) --------------------------------------------------------------

def test_the_graph_flags_a_written_requires_on_its_wikilink_and_keys_it_apart(tmp_vault):
    from silica.kernel.recall.graph_export import build_graph_data
    from silica.ui.web.graph_view import render_html

    tmp_vault.note("ML/Backprop.md", '---\nrequires: ["[[Chain rule]]"]\n---\n\n# Backprop\n\nsee [[Adam]]\n')
    tmp_vault.note("Math/Chain rule.md", "# Chain rule\n")
    tmp_vault.note("ML/Adam.md", "# Adam\n")
    nodes, edges = build_graph_data()
    req = [e for e in edges if e.get("requires")]
    assert [(e["from"], e["to"], e["type"]) for e in req] == [
        ("ML/Backprop.md", "Math/Chain rule.md", "EXTRACTED")]   # one edge, toward the prerequisite
    assert all(not e.get("requires") for e in edges if e["to"] == "ML/Adam.md")
    html = render_html(nodes, edges, communities=[], lib_js="// x")
    assert 'id="cb-requires" checked' in html and "requires" in html


def test_the_chevrons_are_stroked_in_screen_pixels_and_draw_no_head():
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "silica/ui/web/static/graph-frame.js").read_text()
    body = js[js.index("function drawRequires"):js.index("function paintNodeArea")]
    assert "ctx.stroke()" in body and "ctx.fill(" not in body   # open chevrons, never a head
    assert "1 / (k || 1)" in body                                # 16 and 4 screen px at any zoom
    # the GL layer draws plain lines only: a requires stays on the canvas painter
    assert "!l.requires && !libOwnsLink(l)" in js


def test_slash_path_names_the_requires_leg(tmp_vault, monkeypatch):
    from rich.console import Console

    from silica.cli import _handle_direct_shortcut

    console = Console(record=True, width=200)
    monkeypatch.setattr("silica.cli.CONSOLE", console)
    tmp_vault.note("ML/Backprop.md", '---\nrequires: ["[[Chain rule]]"]\n---\n\n# Backprop\n')
    tmp_vault.note("Math/Chain rule.md", "# Chain rule\n")
    _handle_direct_shortcut('/path "Chain rule" "Backprop"', [])
    assert "required by" in console.export_text()
