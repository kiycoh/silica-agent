# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Three composer defects ADR-0093 found on real notes (2026-10-11), each
pinned on a synthetic note of the shape that tripped it. The fourth,
ensure_hub_link, is in test_ensure_hub_link_frontmatter.py.

- stamp_sources (and stamp_citation) on a block that does not parse read no
  prior list and replaced `sources:` (1 of 284 agent notes).
- frontmatter.dump re-emitted the body with lstrip(), so a first body line
  indented by one space lost it (2 of 730 notes).
- the sweep unlinked `[[X| alias]]` to `alias.strip()`, gluing two words
  ("asintoticamente efficienti" became "asintoticamenteefficienti").
"""
from __future__ import annotations

from silica.kernel.link.sweep import _unlink_body
from silica.kernel.write import frontmatter, templates

# `title: [unclosed` is a YAML error: the block is present and unusable.
_BROKEN = '---\ntitle: [unclosed\nsources:\n  - "Old one.md"\n---\n\nBody.\n'


def test_stamp_sources_leaves_an_unparseable_block_alone():
    assert frontmatter.split(_BROKEN)[0] is None
    assert templates.stamp_sources(_BROKEN, "New one.md") == _BROKEN


def test_stamp_citation_leaves_an_unparseable_block_alone():
    assert templates.stamp_citation(_BROKEN, {"doi": "10.1/x"}) == _BROKEN


def test_stamp_sources_still_unions_on_a_parseable_block():
    note = '---\ntitle: T\nsources:\n  - "Old one.md"\n---\n\nBody.\n'
    data, _, _ = frontmatter.split(templates.stamp_sources(note, "New one.md"))
    assert data["sources"] == ["Old one.md", "New one.md"]


def test_dump_keeps_the_first_body_line_indent():
    out = frontmatter.dump({"title": "T"}, "\n\n code-ish first line\nsecond\n")
    assert out == "---\ntitle: T\n---\n\n code-ish first line\nsecond\n"


def test_dump_still_drops_the_leading_blank_lines():
    assert frontmatter.dump({"a": 1}, "\n\n\nBody\n") == "---\na: 1\n---\n\nBody\n"


def test_sweep_unlinks_to_the_alias_as_written():
    body, stripped, _ = _unlink_body("asintoticamente[[Ghost| efficienti]] davvero\n",
                                     lambda t: "")
    assert body == "asintoticamente efficienti davvero\n"
    assert stripped == ["Ghost"]


def test_sweep_relink_keeps_the_alias_as_written():
    body, _, relinked = _unlink_body("a[[Ahura Mazda| god]] b\n",
                                     lambda t: "Ahura-Mazda")
    assert body == "a[[Ahura-Mazda| god]] b\n"
    assert relinked == ["Ahura-Mazda"]


def test_sweep_without_alias_still_unlinks_to_the_target():
    body, _, _ = _unlink_body("See [[Ghost]] and [[Ghost|]] here\n", lambda t: "")
    assert body == "See Ghost and Ghost here\n"
