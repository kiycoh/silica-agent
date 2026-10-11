# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ensure_hub_link adds the hub to `related:` without a second key or broken YAML.

Found 2026-10-11 (ADR-0093 probe): a one-line `related:` value (null, `[]`, a
single link) got a second `related:` key appended, which PyYAML reads as the
last one only, on 170 of 730 test-vault notes; a flush-left list got an
indented item spliced in and the frontmatter stopped parsing (5 of 284).
"""
from __future__ import annotations

import pytest
import yaml

from silica.kernel.write.frontmatter import split
from silica.kernel.write.templates import ensure_hub_link


def _related(note: str) -> list:
    data, raw, _ = split(note)
    assert data is not None, f"frontmatter no longer parses:\n{raw}"
    assert raw.count("\nrelated:") + raw.startswith("related:") == 1, raw
    return data["related"]


@pytest.mark.parametrize("value", ["", " []", " null", ' "[[A]]"', " [[[A]]]"])
def test_a_one_line_related_gets_the_hub_in_one_key(value):
    note = f"---\ntitle: T\nrelated:{value}\n---\n\nbody\n"
    out = ensure_hub_link(note, "Hub")
    rel = _related(out)
    assert "[[Hub]]" in rel
    if "A" in value:
        assert "[[A]]" in rel or [["A"]] in rel  # an unquoted [[A]] is a nested list to YAML
    assert out.endswith("\n---\n\nbody\n")


def test_related_as_the_last_key_gets_the_hub_in_one_key():
    out = ensure_hub_link("---\ntitle: T\nrelated:\n---\nbody\n", "Hub")
    assert _related(out) == ["[[Hub]]"]


def test_a_flush_left_list_still_parses_with_the_hub_added():
    note = '---\nrelated:\n- "[[A]]"\n- "[[B]]"\ntags: [x]\n---\nbody\n'
    out = ensure_hub_link(note, "Hub")
    assert _related(out) == ["[[A]]", "[[B]]", "[[Hub]]"]
    assert yaml.safe_load(split(out)[1])["tags"] == ["x"]


def test_an_indented_list_keeps_its_items_and_the_keys_after_it():
    note = '---\nrelated:\n  - "[[A]]"\ntags: [x]\n---\nbody\n'
    out = ensure_hub_link(note, "Hub")
    assert _related(out) == ["[[A]]", "[[Hub]]"]
    assert yaml.safe_load(split(out)[1])["tags"] == ["x"]


def test_frontmatter_that_does_not_parse_is_left_alone():
    note = "---\ntitle: [unclosed\nrelated:\n---\nbody\n"
    assert ensure_hub_link(note, "Hub") == note
