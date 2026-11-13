# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A command's lines as the chat's prose (ADR-0178): term.console.markdown."""
from __future__ import annotations

import pytest
from rich.markup import escape

from silica.command_table import Result
from silica.term.console import markdown
from silica.ui.web.render import linkify

NOTES = {"Inbox/Attention Is All You Need (arXiv 1706.03762)", "Concepts/AI/Transformer"}


def resolve(ref: str):
    ref = ref[:-3] if ref.endswith(".md") else ref
    return ref + ".md" if ref in NOTES else None


def said(*prints) -> Result:
    out = Result()
    for p in prints:
        args, style = (p if isinstance(p, tuple) else ((p,), {}))
        out.print(*args, **style)
    return out


def md(*prints) -> str:
    return markdown(said(*prints).lines, resolve)


def html(*prints) -> str:
    return linkify(md(*prints), resolve, remote_images=False)


def test_nothing_said_is_done():
    assert md() == "Done."


def test_a_line_is_prose_not_a_code_block():
    out = md("  Nothing changed this session.")
    assert out == "Nothing changed this session."
    assert "<pre" not in html("  Nothing changed this session.")


def test_lines_keep_their_breaks():
    # The renderer joins a single newline into one paragraph; the lines a command
    # printed one under the other must stay under each other.
    page = html("  first line", "  second line")
    assert "first line<br />" in page and "second line" in page


@pytest.mark.parametrize("hostile, absent", [
    ("_a_ and *b*", "<em>"),
    ("$x$", "<math"),
    ("![](http://x.test/i.png)", "<img"),
    ('<img src=x onerror="alert(1)">', "<img"),
    ("<b>raw</b>", "<b>"),
    ("~~gone~~", "<del"),
    # The bare address may still be made a link, as in any answer; the words
    # around it never become one.
    ("[a link](http://x.test)", ">a link</a>"),
])
def test_what_a_title_or_an_error_carries_stays_text(hostile, absent):
    """A fetched title, a file name, an exception: the fence made them inert, and
    escaping is what does it now. Interpolated as the handlers do, through
    rich's escape."""
    page = html(f"  fetch failed: {escape(hostile)}")
    assert absent not in page


@pytest.mark.parametrize("hidden", ["%%a comment%% tail", "<!-- a comment --> tail", "a lone %% tail"])
def test_nothing_a_comment_mark_would_hide_is_lost(hidden):
    page = html(f"  fetch failed: {escape(hidden)}")
    assert "tail" in page and "comment" in page.replace("a lone", "comment")


@pytest.mark.parametrize("sugar, words", [("[[Note]]", "Note"), ("#tag", "#tag"), ("==marked==", "marked")])
def test_the_renderers_own_sugar_keeps_its_words(sugar, words):
    # Run after the escapes are undone, so no escape stops it (see console._ALWAYS);
    # what it draws is inert and still says the words.
    assert words in html(f"  fetch failed: {escape(sugar)}")


@pytest.mark.parametrize("start", ["# heading", "> quote", "- item", "+ item", "1. item", "1) item", "| a | b |"])
def test_a_line_start_is_never_a_block(start):
    page = html("  first", f"  {start}")
    for tag in ("<h1", "<blockquote", "<li", "<ol", "<table"):
        assert tag not in page, (start, page)


def test_a_file_name_reads_clean_in_the_markdown():
    """The copy button copies the Markdown: an underscore inside a word and an
    `=` in a flag emphasise nothing, so they carry no backslash."""
    out = md("  my_notes_v2.md: add --me=you@example.org")
    assert "my_notes_v2.md" in out and "--me=you@example.org" in out


def test_a_bold_note_path_is_a_note_link():
    page = html("  Fetched → [bold]Inbox/Attention Is All You Need (arXiv 1706.03762).md[/]")
    assert 'data-path="Inbox/Attention Is All You Need (arXiv 1706.03762).md"' in page


def test_a_bold_word_that_is_no_note_stays_bold():
    # A folder, a query, a count: a link to nothing would draw as "(no such note)".
    page = html("  1 file → [bold]Concepts/AI[/]")
    assert "<strong>Concepts/AI</strong>" in page and "note-link" not in page


def test_a_yellow_line_is_a_warning_box():
    page = html("  [yellow]fetch failed: the page answered 403[/]")
    assert 'class="callout callout-warning"' in page
    assert ">Warning<" in page and "fetch failed: the page answered 403" in page


def test_a_yellow_line_that_opens_in_bold_titles_its_box():
    page = html("  [yellow][bold]Not done.[/bold] This video could not be read.[/]")
    assert ">Not done<" in page and "This video could not be read." in page
    assert ">Warning<" not in page


def test_a_red_line_is_a_failure_box():
    page = html("  [red]Undo failed:[/] the file is locked")
    assert 'class="callout callout-failure"' in page and "the file is locked" in page


def test_a_dim_line_is_plain_prose():
    assert md("  [dim]2 runs: /revert takes back the most recent[/]").startswith("2 runs")


def test_a_command_name_is_a_chip_and_a_path_is_not():
    out = md("  Read it, then /nucleate turns it into notes; see /home/me/x and https://a.test/fetch")
    assert "`/nucleate`" in out
    assert "`/home`" not in out and "`/fetch`" not in out


def test_backticks_a_message_wrote_stay_code():
    msg = 'install it with `python -m pip install -U "yt-dlp[default]"`.'
    page = html(f"  [yellow]{escape(msg)}[/]")
    assert '<code>python -m pip install -U &quot;yt-dlp[default]&quot;</code>' in page


def test_lines_set_in_from_the_one_above_are_a_list():
    page = html("  Results for [bold]attention[/] (top 2):",
                "    [bold]Concepts/AI/Transformer.md[/] 0.81",
                "    [bold]Concepts/AI/Missing.md[/] 0.74")
    assert page.count("<li>") == 2 and 'data-path="Concepts/AI/Transformer.md"' in page


def test_a_layout_the_handler_declared_stays_a_block():
    digest = "PLAN  [3 checkpoints]\n  map(extract)  sweep(link)\n✓ f0_c0   ⟳ f0_c1"
    out = md(((digest,), {"markup": False, "highlight": False}))
    assert out.startswith("```text\n") and "PLAN  [3 checkpoints]\n  map(extract)" in out


def test_one_line_without_markup_is_prose_with_its_brackets():
    out = html((("  moved: [rerank] coupling +0.12",), {"markup": False}))
    assert "<pre" not in out and "[rerank]" in out


def test_a_long_line_in_a_block_is_not_cut_at_the_terminal_width():
    long = "x" * 200 + "\nsecond"
    assert ("x" * 200) in md(((long,), {"markup": False}))


def test_a_mark_that_spans_two_lines_keeps_both():
    """Parsed line by line, the second line's closing tag raised, after the
    command had run and before its answer was stored."""
    out = md("  [dim]first line\nsecond line[/]", "  [yellow]one\ntwo[/]")
    assert "first line\\\nsecond line" in out and "> two" in out


def test_an_address_is_linked_whole():
    page = html("  read https://en.wikipedia.org/wiki/Attention_(machine_learning) 1.2s")
    assert 'href="https://en.wikipedia.org/wiki/Attention_(machine_learning)"' in page


def test_an_address_hides_no_markdown():
    page = html("  read " + escape("https://x.test/![a](https://evil.test/i.png)*b*") + " done")
    assert "<img" not in page and "<em>" not in page and "evil.test/i.png%5D" not in page.split("href")[0]


def test_a_bold_word_that_names_a_note_by_title_is_not_linked():
    def by_title(ref):
        return "Notes/All.md" if ref.lower() in ("all", "notes/all", "notes/all.md") else None
    out = markdown(said("  [bold]all[/] passes over 3 kinds", "  see [bold]Notes/All.md[/]").lines, by_title)
    assert out.startswith("**all**") and "[[Notes/All]]" in out
