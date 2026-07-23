"""Phase 4 tests — autolink: deterministic wikilink injector."""
from __future__ import annotations

import pytest
from silica.kernel.autolink import autolink, build_title_index


# ---------------------------------------------------------------------------
# Basic linking
# ---------------------------------------------------------------------------

def test_autolink_adds_wikilink_for_matching_title():
    body = "Neural Networks are powerful tools."
    new_body, added = autolink(body, ["Neural Networks"])
    assert "[[Neural Networks]]" in new_body
    assert "Neural Networks" in added


def test_autolink_case_insensitive():
    body = "We study neural networks."
    new_body, added = autolink(body, ["Neural Networks"])
    # Casing differs from the canonical title → alias-preserving link so the
    # body prose keeps its own casing (audit §3).
    assert "[[Neural Networks|neural networks]]" in new_body
    assert "Neural Networks" in added


def test_autolink_links_first_occurrence_only():
    body = "Neural Networks are great. Neural Networks are fun."
    new_body, added = autolink(body, ["Neural Networks"])
    assert new_body.count("[[Neural Networks]]") == 1
    assert "Neural Networks" in added


def test_autolink_no_match_returns_unchanged():
    body = "This note talks about attention mechanisms."
    new_body, added = autolink(body, ["Transformers"])
    assert new_body == body
    assert added == []


def test_autolink_multiple_titles():
    body = "Neural Networks and Backpropagation are key concepts."
    new_body, added = autolink(body, ["Neural Networks", "Backpropagation"])
    assert "[[Neural Networks]]" in new_body
    assert "[[Backpropagation]]" in new_body
    assert len(added) == 2


# ---------------------------------------------------------------------------
# Skip regions
# ---------------------------------------------------------------------------

def test_autolink_skips_frontmatter():
    body = "---\ntitle: Neural Networks\ntags: [AI]\n---\nNeural Networks are great."
    new_body, added = autolink(body, ["Neural Networks"])
    # The frontmatter title should NOT be linked, but the body occurrence should
    assert "[[Neural Networks]]" in new_body
    lines = new_body.split("\n")
    # Frontmatter lines should be unchanged
    assert lines[1] == "title: Neural Networks"


def test_autolink_skips_fenced_code():
    body = "Study Neural Networks.\n```python\n# Neural Networks example\npass\n```"
    new_body, added = autolink(body, ["Neural Networks"])
    # Only the first occurrence (before the code block) should be linked
    assert new_body.count("[[Neural Networks]]") == 1
    assert "# Neural Networks example" in new_body  # code unchanged


def test_autolink_never_links_fence_info_string_even_when_unbalanced():
    # An unbalanced fence elsewhere used to shift the sequential-pairing mask and
    # leave a later info string exposed: ```python → ```[[Python]] (audit finding 5).
    body = (
        "See ```unclosed fence with no partner\n\n"
        "Later:\n```python\nx = 1\n```\n"
    )
    new_body, added = autolink(body, ["Python"])
    assert "```python" in new_body          # info string intact
    assert "[[Python]]" not in new_body     # fence delimiter never linked


def test_autolink_skips_inline_code():
    body = "The `Neural Networks` module. Neural Networks are great."
    new_body, added = autolink(body, ["Neural Networks"])
    # Inline code should be skipped; plain text occurrence should be linked
    assert "`Neural Networks`" in new_body  # inline code unchanged
    assert "[[Neural Networks]]" in new_body


def test_autolink_skips_existing_wikilinks():
    body = "See [[Neural Networks]] for details. Neural Networks matter."
    new_body, added = autolink(body, ["Neural Networks"])
    # Already has [[Neural Networks]] — should not add a second one
    # (the plain-text occurrence after it is the second, not first)
    # Since [[Neural Networks]] is in a skip region, the plain text is the first non-skip match
    # But we still want idempotency: no double-link
    assert new_body.count("[[Neural Networks]]") >= 1
    # Added list should be empty since no NEW link was created in skip-free text
    # (the first occurrence is inside [[...]] which is skipped)
    assert added == []


def test_autolink_skips_math_display_block():
    body = "$$\nNeural Networks equation\n$$\nNeural Networks are great."
    new_body, added = autolink(body, ["Neural Networks"])
    assert "[[Neural Networks]]" in new_body
    assert new_body.count("[[Neural Networks]]") == 1


def test_autolink_skips_heading_lines():
    body = "# Neural Networks\n\nNeural Networks are powerful."
    new_body, added = autolink(body, ["Neural Networks"])
    # Heading line should be unchanged
    assert new_body.startswith("# Neural Networks\n")
    # Body paragraph should be linked
    assert "[[Neural Networks]]" in new_body


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_autolink_idempotent():
    body = "Neural Networks are powerful tools for learning."
    new_body1, added1 = autolink(body, ["Neural Networks"])
    new_body2, added2 = autolink(new_body1, ["Neural Networks"])
    assert new_body1 == new_body2
    assert added2 == []  # second pass adds nothing


# ---------------------------------------------------------------------------
# Candidates (embedding-prioritized subset)
# ---------------------------------------------------------------------------

def test_autolink_candidates_restricts_linking():
    body = "Neural Networks and Backpropagation are important."
    # candidates only has Neural Networks → only that gets linked
    new_body, added = autolink(
        body,
        title_index=["Neural Networks", "Backpropagation"],
        candidates=["Neural Networks"],
    )
    assert "[[Neural Networks]]" in new_body
    assert "[[Backpropagation]]" not in new_body
    assert added == ["Neural Networks"]


def test_autolink_candidates_empty_list_no_links():
    body = "Neural Networks are great."
    new_body, added = autolink(body, ["Neural Networks"], candidates=[])
    # Empty candidates → no titles to process
    assert new_body == body
    assert added == []


# ---------------------------------------------------------------------------
# Word-boundary matching
# ---------------------------------------------------------------------------

def test_autolink_whole_word_only():
    """'Net' should not match inside 'Network'."""
    body = "Neural Networks is not just a Net."
    new_body, added = autolink(body, ["Net"])
    # 'Net' appears as a whole word → should be linked
    assert "[[Net]]" in new_body
    # 'Networks' should NOT become '[[Net]]works'
    assert "[[Net]]works" not in new_body


def test_autolink_does_not_link_single_char_title():
    body = "The A in AI stands for artificial."
    new_body, added = autolink(body, ["A"])
    assert new_body == body
    assert added == []


# ---------------------------------------------------------------------------
# Longest-first ordering
# ---------------------------------------------------------------------------

def test_autolink_longer_title_takes_precedence():
    """'Deep Learning' should be linked as a unit, not 'Learning' separately."""
    body = "Deep Learning is a subset of Machine Learning."
    new_body, added = autolink(body, ["Deep Learning", "Learning"])
    assert "[[Deep Learning]]" in new_body
    # The standalone 'Learning' in 'Machine Learning' may or may not be linked
    # — the important thing is Deep Learning is handled as a unit


# ---------------------------------------------------------------------------
# Empty / edge inputs
# ---------------------------------------------------------------------------

def test_autolink_empty_body():
    new_body, added = autolink("", ["Neural Networks"])
    assert new_body == ""
    assert added == []


def test_autolink_empty_title_index():
    body = "Neural Networks are great."
    new_body, added = autolink(body, [])
    assert new_body == body
    assert added == []


# ---------------------------------------------------------------------------
# build_title_index — disambiguation
# ---------------------------------------------------------------------------

def test_build_title_index_deduplicates():
    """Two refs with the same name → dropped (ambiguous)."""
    from unittest.mock import MagicMock

    ref_a = MagicMock()
    ref_a.name = "Neural Networks"
    ref_b = MagicMock()
    ref_b.name = "Neural Networks"  # duplicate
    ref_c = MagicMock()
    ref_c.name = "Backpropagation"

    index = build_title_index([ref_a, ref_b, ref_c])
    assert "Neural Networks" not in index
    assert "Backpropagation" in index


def test_build_title_index_unique_titles_kept():
    from unittest.mock import MagicMock

    refs = []
    for name in ("A", "B", "C"):
        r = MagicMock()
        r.name = name
        refs.append(r)

    index = build_title_index(refs)
    assert sorted(index) == ["A", "B", "C"]


def test_build_title_index_sorted():
    from unittest.mock import MagicMock

    refs = []
    for name in ("Zig", "Alpha", "Middle"):
        r = MagicMock()
        r.name = name
        refs.append(r)

    index = build_title_index(refs)
    assert index == sorted(index)


# ---------------------------------------------------------------------------
# Regression tests for structural bugs (reported post Phase 4)
# ---------------------------------------------------------------------------

def test_autolink_no_self_link():
    """A note must never wikilink to itself (self_title excluded)."""
    body = "DDS è un middleware. Il Data Distribution Service è usato in ROS."
    new_body, added = autolink(body, ["DDS", "ROS"], self_title="DDS")
    assert "[[DDS]]" not in new_body, "self-link must not be emitted"
    assert "[[ROS]]" in new_body, "other titles must still be linked"
    assert "DDS" not in added
    assert "ROS" in added


def test_autolink_self_link_case_insensitive():
    """Self-title exclusion is case-insensitive."""
    body = "HAL layers abstract the hardware. See also Linux."
    new_body, added = autolink(body, ["HAL", "Linux"], self_title="hal")
    assert "[[HAL]]" not in new_body
    assert "[[Linux]]" in new_body


def test_autolink_self_link_not_excluded_when_none():
    """When self_title is None (default), no exclusion is applied."""
    body = "PWM controls duty cycle."
    new_body, added = autolink(body, ["PWM"])
    # Without self_title, the title IS linked (previous behavior preserved)
    assert "[[PWM]]" in new_body


# ---------------------------------------------------------------------------
# Content-corruption regressions (audit 2026-07-23 §2 — incomplete skip mask)
# Each case ran `autolink()` real and produced corruption before the fix.
# ---------------------------------------------------------------------------

def test_autolink_skips_bare_url():
    body = "See https://example.com/page for more."
    new_body, added = autolink(body, ["example", "page"])
    assert new_body == body
    assert added == []


def test_autolink_skips_markdown_link_text():
    body = "Read [intro to Neural Networks](http://u) today."
    new_body, added = autolink(body, ["Neural Networks"])
    assert new_body == body  # link text is not prose to link


def test_autolink_skips_url_inside_markdown_link():
    body = "Read [the docs](https://x.com/page) now."
    new_body, added = autolink(body, ["page"])
    assert new_body == body


def test_autolink_does_not_kill_inline_tag():
    body = "This note is about #Python and its ecosystem."
    new_body, added = autolink(body, ["Python"])
    assert "#Python" in new_body            # tag survives intact
    assert "#[[Python]]" not in new_body    # never rewritten into the tag


def test_autolink_skips_indented_code_block():
    body = "Prose here.\n\n    import Python\n    Python.run()\n\nMore prose."
    new_body, added = autolink(body, ["Python"])
    assert "    import Python" in new_body
    assert "[[Python]]" not in new_body


def test_autolink_skips_unclosed_fence_to_eof():
    # A fence opened and never closed leaves its body exposed to matching
    # unless masked to EOF (audit finding 6).
    body = "intro\n```python\nresult = Python.run()\n"
    new_body, added = autolink(body, ["Python"])
    assert new_body == body
    assert added == []


def test_autolink_skips_crlf_frontmatter():
    # Windows line endings must not defeat frontmatter masking — backlink_pass
    # rewrites pre-existing USER notes, which may be CRLF (audit finding 7).
    body = "---\r\ntitle: Neural Networks\r\ntags: [ai]\r\n---\r\nNeural Networks rock."
    new_body, added = autolink(body, ["Neural Networks"])
    assert "title: Neural Networks" in new_body            # frontmatter untouched
    assert "[[Neural Networks]]" in new_body               # body occurrence linked
    assert new_body.count("[[Neural Networks]]") == 1


def test_autolink_skips_html_attribute():
    body = 'Diagram: <img alt="Neural Networks flow" src="x.png"> below.'
    new_body, added = autolink(body, ["Neural Networks"])
    assert new_body == body


# ---------------------------------------------------------------------------
# Link-coherence regressions (audit 2026-07-23 §3)
# ---------------------------------------------------------------------------

def test_autolink_preserves_body_casing_as_alias():
    body = "we love neural networks a lot"
    new_body, added = autolink(body, ["Neural Networks"])
    assert "[[Neural Networks|neural networks]]" in new_body
    # And it stays idempotent through the alias
    again, added2 = autolink(new_body, ["Neural Networks"])
    assert again == new_body
    assert added2 == []


def test_autolink_exact_casing_uses_plain_link():
    body = "Neural Networks are powerful."
    new_body, added = autolink(body, ["Neural Networks"])
    assert "[[Neural Networks]]" in new_body
    assert "|" not in new_body  # no needless alias when casing matches


def test_autolink_path_qualified_link_blocks_duplicate():
    # [[topics/Python]] already links the note — do not add a bare [[Python]].
    body = "See [[topics/Python]]. Python is great."
    new_body, added = autolink(body, ["Python"])
    assert new_body.count("[[") == 1
    assert added == []


def test_build_title_index_case_insensitive_dedup():
    # Foo and foo are ambiguous under IGNORECASE matching → both dropped.
    index = build_title_index(["Foo", "foo", "Bar"])
    assert index == ["Bar"]
