"""Unit tests for render_tree() — the vault file tree emitter."""
from __future__ import annotations

from silica.ui.web.graph_view import render_tree


def _node(nid: str, node_type: str = "note") -> dict:
    return {"id": nid, "label": nid.split("/")[-1], "type": node_type, "path": nid}


def test_nested_folders_become_details():
    html = render_tree([_node("a/b/c.md"), _node("a/d.md"), _node("e.md")])
    assert '<div id="file-tree">' in html
    # the summary carries the note count under the folder (audit P19)
    assert '<summary>a<span class="tree-n">2</span></summary>' in html
    assert '<summary>b<span class="tree-n">1</span></summary>' in html
    # top-level folder is open, nested folder is not
    assert "<details open><summary>a<" in html
    assert "<details><summary>b<" in html


def test_notes_render_as_tree_note_buttons_with_data_id():
    """A note row must be a real button, not a div with a click handler.

    The tree is the primary route into a note. As a div it was unreachable by
    keyboard and reported to the accessibility tree as `generic`, so a whole
    vault's worth of notes sat outside the tab order.
    """
    html = render_tree([_node("a/c.md")])
    # the leaf drops its `.md` (audit P19): the drawer states the path
    assert '<button type="button" class="tree-note" data-id="a/c.md">c</button>' in html
    assert '<div class="tree-note"' not in html


def test_the_rail_variant_renders_notes_as_links_to_their_url():
    """With actions (the rail), a leaf is an <a href="#chat?note=…"> so the URL
    is the state (audit P7): a middle click opens the note in a new tab."""
    html = render_tree([_node("a/c d.md")], actions=True)
    assert '<a class="tree-note" href="#chat?note=a%2Fc%20d.md" data-id="a/c d.md">c d</a>' in html


def test_ghost_nodes_excluded():
    html = render_tree([_node("real.md"), _node("Ghost", node_type="ghost")])
    assert "real.md" in html
    assert "Ghost" not in html


def test_empty_path_excluded():
    n = {"id": "x", "label": "x", "type": "note", "path": ""}
    html = render_tree([n])
    assert "tree-note" not in html


def test_folders_sorted_before_notes_within_a_level():
    # at root: folder "z" must appear before note "a.md"
    html = render_tree([_node("a.md"), _node("z/inner.md")])
    assert html.index("<summary>z<") < html.index("a.md")


def test_notes_sorted_case_insensitively():
    html = render_tree([_node("Beta.md"), _node("alpha.md")])
    assert html.index("alpha.md") < html.index("Beta.md")


def test_names_html_escaped():
    html = render_tree([_node("a & b.md")])
    assert "a &amp; b.md" in html


def test_empty_input_does_not_crash_and_has_no_leaves():
    html = render_tree([])
    assert html == '<div id="file-tree"></div>'


def test_all_ghost_input_has_no_leaves():
    html = render_tree([_node("g", node_type="ghost")])
    assert "tree-note" not in html


def test_the_rail_offers_plan_this_folder_and_the_frame_does_not():
    """ADR-0081 §17: a folder row of the rail carries Learn's Plan this folder,
    with its full path; the frame's tree has no Learn tab to send it to."""
    html = render_tree([_node("a/b/c.md")], actions=True)
    assert 'class="tree-plan" data-folder="a/"' in html and 'data-folder="a/b/"' in html
    assert "tree-plan" not in render_tree([_node("a/b/c.md")])
