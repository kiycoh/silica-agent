"""Unit tests for silica.kernel.text.media — strip_images and section images."""
from __future__ import annotations

import textwrap

from silica.kernel.text.media import (
    strip_images,
    images_for_section,
    append_section_images,
)


# ---------------------------------------------------------------------------
# strip_images — Obsidian-flavor embeds
# ---------------------------------------------------------------------------

class TestStripOFMImages:
    def test_bare_jpg(self):
        text = "Before\n![[images/a1e8022c.jpg]]\nAfter"
        result = strip_images(text)
        assert "![[" not in result
        assert "Before" in result
        assert "After" in result

    def test_bare_png(self):
        assert "![[" not in strip_images("![[screenshot.png]]")

    def test_bare_gif(self):
        assert "![[" not in strip_images("![[anim.gif]]")

    def test_bare_webp(self):
        assert "![[" not in strip_images("![[photo.webp]]")

    def test_bare_svg(self):
        assert "![[" not in strip_images("![[icon.svg]]")

    def test_size_hint(self):
        """![[file.png|300]] — Obsidian size hint must be stripped too."""
        result = strip_images("![[diagram.png|300]]")
        assert "![[" not in result

    def test_size_hint_wh(self):
        result = strip_images("![[diagram.png|200x400]]")
        assert "![[" not in result

    def test_path_with_subdirs(self):
        raw = "![[attachments/2024/screenshot.jpeg]]"
        assert strip_images(raw).strip() == ""

    def test_uppercase_extension(self):
        raw = "![[Photo.JPG]]"
        assert "![[" not in strip_images(raw)

    def test_multiple_embeds(self):
        raw = "Title\n![[a.jpg]]\nText\n![[b.png|100]]\nEnd"
        result = strip_images(raw)
        assert "![[" not in result
        assert "Title" in result
        assert "Text" in result
        assert "End" in result


# ---------------------------------------------------------------------------
# strip_images — Standard Markdown images
# ---------------------------------------------------------------------------

class TestStripMarkdownImages:
    def test_empty_alt(self):
        raw = "![](images/a1e8022c.jpg)"
        assert "![]" not in strip_images(raw)
        assert strip_images(raw).strip() == ""

    def test_with_alt_text(self):
        raw = "![Figure 1](images/fig1.png)"
        result = strip_images(raw)
        assert "!["not in result

    def test_remote_url(self):
        raw = "![logo](https://example.com/logo.png)"
        assert strip_images(raw).strip() == ""

    def test_mixed_with_text(self):
        raw = "Some text.\n![](img.jpg)\nMore text."
        result = strip_images(raw)
        assert "Some text." in result
        assert "More text." in result
        assert "!["  not in result

    def test_inline_in_paragraph(self):
        raw = "See the ![diagram](diag.png) below for details."
        result = strip_images(raw)
        assert "See the" in result
        assert "below for details." in result
        assert "![" not in result


# ---------------------------------------------------------------------------
# strip_images — things that must NOT be stripped
# ---------------------------------------------------------------------------

class TestStripPreservation:
    def test_wikilink_untouched(self):
        raw = "[[NeuralNetwork]] is connected to [[Backprop]]."
        assert strip_images(raw) == raw

    def test_plain_text_untouched(self):
        raw = "The quick brown fox jumps over the lazy dog."
        assert strip_images(raw) == raw

    def test_hyperlink_not_image(self):
        """[text](url) without leading ! must not be stripped."""
        raw = "[See docs](https://example.com/docs)"
        assert strip_images(raw) == raw

    def test_markdown_bold_untouched(self):
        raw = "**Bold text** and *italic*."
        assert strip_images(raw) == raw

    def test_empty_string(self):
        assert strip_images("") == ""

    def test_blank_line_collapse(self):
        """Multiple blank lines left by removed embeds are collapsed to one."""
        raw = "Line A\n\n![[a.jpg]]\n\n![[b.png]]\n\nLine B"
        result = strip_images(raw)
        assert "Line A" in result
        assert "Line B" in result
        # Should not have 3+ consecutive newlines
        assert "\n\n\n" not in result


# ---------------------------------------------------------------------------
# images_for_section — section-scoped image embeds for a concept
# ---------------------------------------------------------------------------

_SRC = textwrap.dedent("""\
    # Doc

    ## Architecture

    The system uses a pipeline.

    ![[images/arch.png]]

    ## Storage

    Uses LanceDB.

    ![Figure 2](images/db.png)
    ![remote](https://example.com/logo.png)
""")


class TestImagesForSection:
    def test_ofm_embed_normalized_to_basename(self):
        assert images_for_section(_SRC, "Architecture") == ["![[arch.png]]"]

    def test_markdown_image_normalized_skips_remote(self):
        # db.png is local → kept; the https logo → dropped (would point nowhere).
        assert images_for_section(_SRC, "Storage") == ["![[db.png]]"]

    def test_no_matching_heading_returns_empty(self):
        assert images_for_section(_SRC, "Nonexistent") == []

    def test_concept_substring_of_heading_matches(self):
        src = "## Message provenance and tracking\n![[p.png]]\n"
        assert images_for_section(src, "Message provenance") == ["![[p.png]]"]

    def test_dedup_same_image_twice(self):
        src = "## S\n![[a.png]]\ntext\n![[images/a.png]]\n"
        assert images_for_section(src, "S") == ["![[a.png]]"]

    def test_no_images_in_section(self):
        assert images_for_section("## S\nplain text only\n", "S") == []


class TestAppendSectionImages:
    def test_appends_after_snippet(self):
        out = append_section_images("distilled body.", _SRC, "Architecture")
        assert out.startswith("distilled body.")
        assert out.rstrip().endswith("![[arch.png]]")

    def test_noop_when_no_section_images(self):
        assert append_section_images("body", _SRC, "Nonexistent") == "body"

    def test_handles_empty_snippet(self):
        out = append_section_images("", _SRC, "Architecture")
        assert out.strip() == "![[arch.png]]"


def test_append_section_images_lands_before_the_relations_block():
    # The figure of the section landed after `## Relations` (5% of the notes
    # of the 14-lecture run, 2026-09-06): it belongs to the body.
    from silica.kernel.text.media import append_section_images
    src = "## Fisher\ntesto\n![[fig.jpg]]\n## Altro\n"
    snippet = "> claim\n\ncorpo\n\n## Relations\n- applies [[X]]: w\n"
    out = append_section_images(snippet, src, "Fisher")
    assert out.index("![[fig.jpg]]") < out.index("## Relations")
    assert out.rstrip().endswith("- applies [[X]]: w")
    assert append_section_images("corpo", src, "Fisher") == "corpo\n\n![[fig.jpg]]"


def test_append_section_images_skips_embeds_the_snippet_already_carries():
    # An overwrite body is the whole rewritten note, and the model usually keeps
    # the figure it was given: re-attaching blindly stacked the same embed twice.
    src = "## Fisher\ntesto\n![[fig.jpg]]\n![[tab.png]]\n## Altro\n"
    kept = "corpo\n\n![[fig.jpg]]\n"
    out = append_section_images(kept, src, "Fisher")
    assert out.count("![[fig.jpg]]") == 1
    assert "![[tab.png]]" in out
    assert append_section_images("corpo\n\n![[fig.jpg]]\n![[tab.png]]", src, "Fisher") == "corpo\n\n![[fig.jpg]]\n![[tab.png]]"


def test_attach_section_images_reaches_overwrite_ops(monkeypatch):
    # Collision-enrich rewrites carry `content`, not `snippet`, and were skipped
    # outright: 144 notes of the study vault embed figures, so every enriched
    # note lost the figure of the section it was distilled from (2026-09-09).
    from types import SimpleNamespace
    from silica.kernel.write.ops import Op, OpType
    from silica.router import orchestrator as orch
    from silica.router.states.write import _attach_section_images

    src = "## Fisher\ntesto\n![[fig.jpg]]\n## Altro\n"
    monkeypatch.setattr(orch, "DRIVER", SimpleNamespace(read_note=lambda p: SimpleNamespace(content=src)))
    fsm = SimpleNamespace(_chunk_flat_to_fi_ci={}, _current_chunk_idx=0, _file_chunks={}, inbox_file="Inbox/lez.md")
    ops = [
        Op(op=OpType.overwrite, heading="Fisher", source_basename="lez.md", path="Corso/Fisher.md", content="corpo nuovo"),
        Op(op=OpType.overwrite, heading="Fisher", source_basename="lez.md", path="Corso/Fisher.md", content="corpo\n\n![[fig.jpg]]"),
        Op(op=OpType.write, heading="Fisher", source_basename="lez.md", path="Corso/Fisher.md", snippet="corpo", hub="Hub"),
    ]
    _attach_section_images(fsm, ops)
    assert ops[0].content == "corpo nuovo\n\n![[fig.jpg]]"
    assert ops[1].content == "corpo\n\n![[fig.jpg]]"      # already there: not stacked
    assert ops[2].snippet == "corpo\n\n![[fig.jpg]]"
