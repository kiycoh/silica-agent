"""frontmatter.split contract: what counts as a usable property block.

The bug that motivated these: a note whose frontmatter is a YAML *sequence*
parses fine, so split() handed the list back as `data`, and the first
`data.get(...)` downstream raised AttributeError. mark_contested, clear_contested,
contested_refs, reliability_tier, mark_superseded_by, resolve_contested,
notetype.derive_type, codedocs and graph_report all took that path.
"""
from __future__ import annotations

import pytest

from silica.kernel.write import frontmatter as fm


def test_mapping_frontmatter_parses_to_a_dict():
    data, raw, body = fm.split("---\ntags: [x]\n---\n\nbody\n")
    assert data == {"tags": ["x"]} and raw == "tags: [x]" and body.strip() == "body"


def test_empty_frontmatter_is_an_empty_mapping_not_a_failure():
    data, raw, _ = fm.split("---\n\n---\n\nbody\n")
    assert data == {} and raw is not None


def test_no_frontmatter_gives_none_data_and_none_raw():
    # the pair (None, None) is how callers tell "no block" from "unusable block"
    assert fm.split("just a body\n") == (None, None, "just a body\n")


@pytest.mark.parametrize("block", ["- a\n- b", "just a bare scalar", "false"])
def test_non_mapping_frontmatter_is_unusable_but_preserved(block):
    data, raw, body = fm.split(f"---\n{block}\n---\n\nbody\n")
    assert data is None            # not a property block
    assert raw == block            # ...but never discarded
    assert body.strip() == "body"


def test_broken_yaml_is_unusable_but_preserved():
    data, raw, _ = fm.split("---\naliases: [\n---\n\nbody\n")
    assert data is None and raw is not None


# ---------------------------------------------------------------------------
# The downstream writers inherit the guard they already had
# ---------------------------------------------------------------------------

SEQ_NOTE = "---\n- a\n- b\n---\n\nbody\n"


def test_contested_writers_leave_a_non_mapping_note_untouched():
    from silica.kernel.write import contested as c

    assert c.mark_contested(SEQ_NOTE, "flagged: x") == SEQ_NOTE
    assert c.clear_contested(SEQ_NOTE) == SEQ_NOTE
    assert c.mark_superseded_by(SEQ_NOTE, "Winner.md") == SEQ_NOTE
    assert c.resolve_contested(SEQ_NOTE, resolved_by="user", valid_to="2026-08-04") == SEQ_NOTE
    assert c.contested_refs(SEQ_NOTE) == []


def test_reliability_tier_ranks_a_non_mapping_note_lowest():
    from silica.kernel.write.contested import TIER_DISTILLED, reliability_tier

    # a parse accident must never win a contest
    assert reliability_tier(SEQ_NOTE) == TIER_DISTILLED


def test_add_alias_leaves_a_non_mapping_note_untouched():
    assert fm.add_alias(SEQ_NOTE, "AI") == SEQ_NOTE
    assert fm.aliases_of(SEQ_NOTE) == []


def test_derive_type_survives_a_non_mapping_note():
    from silica.kernel.write.notetype import derive_type

    assert derive_type("Some/Note.md", SEQ_NOTE)  # a type, not an AttributeError
