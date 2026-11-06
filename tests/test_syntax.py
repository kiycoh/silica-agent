"""Markdown's grammar, one table: each shape on disk against each named regex.

A column is a reader's need. Two columns differ on purpose; a row that moves here
moves what a link means to that reader, so it is a decision and not a cleanup.
"""
from __future__ import annotations

import pytest

from silica.kernel.base import syntax as s

CASES = {
    #  name              text                     WIKILINK    LINK       MENTION    SPAN       CELL
    "plain":            ("[[Note]]",              "Note",     "Note",    "Note",    "Note",    "Note"),
    "alias":            ("[[Note|shown]]",        "Note",     "Note",    "Note",    "Note",    "Note"),
    "anchor":           ("[[Note#Head]]",         "Note",     "Note",    "Note",    "Note",    "Note"),
    "block_ref":        ("[[Note#^abc]]",         "Note",     "Note",    "Note",    "Note",    "Note"),
    "caret":            ("[[Note^abc]]",          "Note^abc", "Note^abc", "Note^abc", "Note",  "Note^abc"),
    "self_anchor":      ("[[#Head]]",             None,       None,      None,      "",        ""),
    "embed":            ("![[pic.png]]",          "pic.png",  None,      None,      "pic.png", "pic.png"),
    "line_break":       ("[[No\nte]]",            None,       None,      None,      "No\nte",  None),
    "bracket":          ("[[word_map['k']]]",     "word_map['k'", "word_map['k'", None, "word_map['k'", "word_map['k'"),
    "escaped_pipe":     ("[[Note\\|shown]]",      "Note\\",   "Note\\",  "Note\\",  "Note\\",  "Note"),
    "empty":            ("[[]]",                  None,       None,      None,      "",        ""),
}


@pytest.mark.parametrize("name", CASES)
def test_each_grammar_reads_each_link_as_the_table_says(name):
    text, wikilink, link, mention, span, cell = CASES[name]

    def got(regex, group):
        m = regex.search(text)
        return m.group(group) if m else None

    assert got(s.WIKILINK_RE, 2) == wikilink
    assert got(s.LINK_RE, 1) == link
    assert got(s.MENTION_RE, 1) == mention
    assert got(s.WIKILINK_SPAN_RE, 2) == span
    assert got(s.WIKILINK_CELL_RE, 2) == cell


def test_wikilink_groups_are_embed_target_anchor_alias():
    m = s.WIKILINK_RE.search("x ![[Note#Head|shown]] y")
    assert m.groups() == ("!", "Note", "#Head", "shown")


def test_the_span_grammar_keeps_each_part_as_written():
    assert s.WIKILINK_SPAN_RE.search("![[a#b|c]]").groups() == ("!", "a", "#b", "|c")


def test_a_link_in_a_code_span_does_not_run_to_the_next_line():
    # `[[` in inline code once paired with a `]]` paragraphs below.
    text = "Type `[[` to link.\n\nA later [[Workspaces]] link."
    assert s.WIKILINK_RE.findall(text) == [("", "Workspaces", "", "")]
    assert s.WIKILINK_NAME_RE.findall(text) == ["Workspaces"]


def test_flattening_a_link_three_ways():
    link = "[[Note#Head|shown]]"
    assert s.WIKILINK_FLAT_RE.sub(r"\1", link) == "Note#Head"
    assert s.WIKILINK_SHOWN_RE.sub(r"\2", link) == "shown"
    assert s.WIKILINK_INNER_RE.sub(r"\1", "!" + link) == "Note#Head|shown"


def test_an_image_is_either_spelling_and_leaves_its_title_out():
    assert s.IMAGE_RE.search("![[a.png|200]]").group(1) == "a.png"
    assert s.IMAGE_RE.search('![alt](a.png "title")').group(2) == "a.png"


def test_a_heading_carries_its_level_and_its_title():
    m = s.HEADING_RE.search("text\n## A title  \nmore")
    assert m.groups() == ("##", "A title")


@pytest.mark.parametrize("level, end", [(3, 10), (2, 18), (1, 23)])
def test_a_section_ends_at_a_heading_of_its_level_or_above(level, end):
    text = "## A\nbody\n### B\nx\n## C\n"
    assert s.section_end(text, level, 4) == end


def test_a_section_with_nothing_after_it_runs_to_the_end():
    assert s.section_end("## A\nbody\n", 2, 4) == 10


def test_inline_math_is_strict_for_a_reader_that_keeps_prices():
    assert s.INLINE_MATH_RE.search("costs $5 and $6")
    assert not s.INLINE_MATH_STRICT_RE.search("costs $5 and $6")
    assert s.INLINE_MATH_STRICT_RE.search("a $x + y$ b")


def test_a_note_name_refuses_more_than_a_file_does():
    assert not s.FILE_UNSAFE_RE.search("a#b[c]") and s.FILE_UNSAFE_RE.search("a/b")
    assert s.NAME_UNSAFE_RE.sub(" ", 'a#b:c').split() == ["a", "b", "c"]
