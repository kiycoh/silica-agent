"""Where a frontmatter block ends: one table, every grammar, every shape on disk.

A row is a note a vault really can hold. FM_RE's column is which notes parse, so a
change there is a decision about the write gate and never a refactor.
"""
from __future__ import annotations

import pytest

from silica.kernel.base import frontmatter
from silica.kernel.base.frontmatter import (
    FM_RE,
    FM_RE_SCAN,
    FM_RE_TEMPLATE,
    FM_RE_TOLERANT,
    block,
    body_of,
)
from silica.kernel.write import notetype, templates

SHAPES = {
    #  name          text                                     gate   templ  toler  scan
    "lf":           ("---\na: 1\n---\nbody\n",                 True,  True,  True,  True),
    "crlf":         ("---\r\na: 1\r\n---\r\nbody\r\n",         True,  False, True,  True),
    "eof_close":    ("---\na: 1\n---",                         False, False, True,  True),
    "dots_close":   ("---\na: 1\n...\nbody\n",                 False, False, True,  True),
    "lead_blank":   ("\n---\na: 1\n---\nbody\n",               False, False, False, True),
    "open_space":   ("--- \na: 1\n---\nbody\n",                True,  True,  True,  True),
    "close_space":  ("---\na: 1\n--- \nbody\n",                True,  True,  True,  True),
    "empty":        ("---\n---\nbody\n",                       False, False, True,  True),
    "empty_blank":  ("---\n\n---\nbody\n",                     True,  True,  True,  True),
    "unterminated": ("---\na: 1\nbody\n",                      False, False, False, False),
    "rule_only":    ("---\n\nbody\n",                          False, False, False, False),
    "heavy_rule":   ("---\na: 1\n-----\nbody\n",               False, False, False, False),
    "none":         ("body\n",                                 False, False, False, False),
}


@pytest.mark.parametrize("name", SHAPES)
def test_each_grammar_reads_each_shape_as_the_table_says(name):
    text, gate, template, tolerant, scan = SHAPES[name]
    assert bool(FM_RE.match(text)) is gate
    assert bool(FM_RE_TEMPLATE.match(text)) is template
    assert bool(FM_RE_TOLERANT.match(text)) is tolerant
    assert bool(FM_RE_SCAN.match(text)) is scan


def test_the_gate_keeps_its_body_bytes():
    assert frontmatter.split("---\na: 1\n---\n\n\nbody\n")[2] == "body\n"   # \s* takes the blanks
    assert body_of("---\na: 1\n---\n\n\nbody\n") == "body\n"
    assert body_of("---\na: 1\n---\n\n\nbody\n", FM_RE_TOLERANT) == "\n\nbody\n"
    assert body_of("no block\n") == "no block\n"


def test_a_tolerant_group_holds_the_yaml():
    assert FM_RE_TOLERANT.match("---\ndate: 2026-01-02\n...\nbody").group(1) == "date: 2026-01-02\n"
    assert FM_RE_TOLERANT.match("---\n---\nbody").group(1) == ""


def test_block_is_the_yaml_between_the_fences():
    for text in ("---\na: 1\n---\nbody\n", "--- \na: 1\n--- \nbody\n"):
        start, end = block(text)
        assert text[start:end] == "a: 1"
    assert block("---\na: 1\nbody\n") is None and block("body\n") is None


# A rule further down used to be the block's end for every stamper.
RULE_BELOW = "---\na: 1\n--- \n\nbody\n\n---\n\nmore\n"


@pytest.mark.parametrize("stamped", [
    lambda c: templates.ensure_ai_flag(c),
    lambda c: templates.upsert_props(c, {"k": "v"}),
    lambda c: templates.stamp_sources(c, "s.pdf"),
    lambda c: templates.stamp_citation(c, {"doi": "10.1/x"}),
    lambda c: templates.stamp_documents(c, ["a.py"]),
    lambda c: notetype.stamp_type("n.md", c),
])
def test_a_stamp_lands_in_the_block_and_not_in_the_prose(stamped):
    out = stamped(RULE_BELOW)
    data, _raw, body = frontmatter.split(out)
    assert out != RULE_BELOW and data["a"] == 1 and len(data) > 1
    assert body == "body\n\n---\n\nmore\n"


def test_a_stamp_keeps_the_fences_it_found():
    out = templates.stamp_sources("--- \na: 1\n--- \nbody\n", "s.pdf")
    assert out.startswith("--- \na: 1\n") and "\n--- \nbody\n" in out
    assert frontmatter.split(out)[0]["sources"] == ["s.pdf"]


def test_a_crlf_note_is_stamped_and_keeps_its_lines():
    note = "---\r\na: 1\r\n---\r\nbody\r\n"
    out = templates.ensure_ai_flag(note)
    assert frontmatter.split(out)[0] == {"a": 1, "AI": True}
    assert out.startswith("---\r\na: 1\r\n") and out.endswith("\r\nbody\r\n")


def test_a_crlf_note_gets_no_second_block():
    out = templates.ensure_system_floor("---\r\na: 1\r\n---\r\nbody\r\n")
    assert out.count("a: 1") == 1 and frontmatter.split(out)[0]["a"] == 1
    assert not FM_RE.match(frontmatter.split(out)[2])


@pytest.mark.parametrize("stamp", [
    lambda c: templates.ensure_ai_flag(c),
    lambda c: templates.ensure_hub_link(c, "Hub"),
    lambda c: templates.ensure_system_floor(c),
    lambda c: templates.upsert_props(c, {"k": "v"}),
    lambda c: notetype.stamp_type("n.md", c),
])
def test_an_unterminated_block_is_left_for_the_lint(stamp):
    note = "---\na: 1\nbody\n"
    assert stamp(note) == note


def test_a_verified_stamp_keeps_a_crlf_block_whole():
    out = notetype.stamp_verified("---\r\na: 1\r\n---\r\nbody\r\n", "human:me", "2026-11-06")
    data, _raw, body = frontmatter.split(out)
    assert data["a"] == 1 and data["verified"][0]["by"] == "human:me"
    assert not FM_RE.match(body)
