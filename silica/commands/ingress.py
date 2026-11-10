# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What brings a source in: /nucleate, /promote, /convert, /web-search, /fetch, /keep."""
from __future__ import annotations

import datetime
from dataclasses import dataclass

from silica.command_table import Call, Result
from silica.session.words import int_flag, positional, str_flag
from silica.sources import ingest


@dataclass(frozen=True)
class Words:
    """/convert's and /nucleate's words, read once: by the command, and by the
    browser's jobs (`ui/web/jobs.from_command`), which convert the files ahead
    of the command and carry `flags` to the /nucleate they offer. Read twice,
    a flag added to the command reached the jobs only if added there too, and
    four never did."""

    files: list[str]          # as typed: a path keeps its case
    flags: list[str]          # every word that is not a file, as typed
    target: str
    hub: str
    # Explicit lens override: tops the whole form ladder
    # (docs/specs/nucleation-forms.md), filing included.
    profile: str
    seen: str                 # the capture clock, the day the events happened, or ""
    bad_seen: str             # a --seen that is no date, said and ignored
    # On by default: the leaf lives in `sources/`, which every index built on
    # the vault listing leaves out (is_source_leaf: embeddings, the graph, the
    # driver's search), while the passage search behind silica_search_context
    # walks it and returns it (silica_core's recall/paths.py, probed 2026-11-04).
    # It is what makes a note's verbatim source reachable at all
    # (reliability_tier reads exactly that). --no-keep-sources opts out.
    keep_sources: bool


def read_words(words: list[str]) -> Words:
    """The command's words after its name, read as the command reads them."""
    args = ingest.rejoin_spaced_paths(words)
    seen, bad = str_flag(args, "--seen"), ""
    if seen:
        # Trust boundary: this string becomes the valid_from on every claim
        # of the run — a typo'd date would poison note_clock vault-wide.
        try:
            seen = datetime.date.fromisoformat(seen).isoformat()
        except ValueError:
            seen, bad = "", seen
    return Words(
        files=positional(args), flags=[a for a in args if a.startswith("-")],
        target=str_flag(args, "--target"), hub=str_flag(args, "--hub"),
        profile=str_flag(args, "--profile"), seen=seen, bad_seen=bad,
        keep_sources="--no-keep-sources" not in args,
    )


def nucleate(c: Call) -> Result:
    """/nucleate <file|folder|glob...> [--target=DIR] [--hub=H] [--profile=P]
    [--seen=YYYY-MM-DD] [--no-keep-sources] — ingest sources into vault notes.

    No arguments drains the write-ahead log instead. Fully inline: returns "" when
    it dispatched, or an agent-directed message when the target cannot be resolved
    from the arguments alone.
    """
    out = c.out
    w = read_words(c.args)
    if not w.files and not w.flags:
        return out.to_model(ingest.drain_wal(out))
    if w.bad_seen:
        out.print(f"  [yellow]--seen ignored: {w.bad_seen!r} is not YYYY-MM-DD[/]")
    return out.to_model(ingest.nucleate(
        w.files, target_dir=w.target, hub=w.hub, seen=w.seen, profile=w.profile,
        keep_sources=w.keep_sources, user_input=c.raw, console=out, ask=c.ask and c.ask.text,
    ))


def promote(c: Call) -> Result:
    """/promote [<key>]: session memory into a note, through the gate."""
    return c.out.to_model(ingest.promote(positional(c.args), c.out))


def convert(c: Call) -> Result:
    """/convert <file...> [--target=DIR] — document to inbox notes, no distillation."""
    out = c.out
    w = read_words(c.args)
    if not w.files:
        return out.refused(f"/convert requires at least one file path. Usage: {c.row.synopsis}")
    from silica.sources.convert import convert
    for f in w.files:
        try:
            paths = convert(f, dest_dir=w.target)
            out.print(
                f"  Converted {f} → [bold]{len(paths)}[/] note(s): {', '.join(paths)}"
            )
        except ValueError as e:
            out.print(f"  [yellow]Skipped {f}: {e}[/]")
    return out


def web_search(c: Call) -> Result:
    """/web-search "<concept>" [--max-searches=N] — research the web into an inbox note."""
    args, out = c.args, c.out
    from rich.markup import escape

    from silica.sources.web_research import web_research, DEFAULT_MAX_SEARCHES
    max_searches = int_flag(args, "--max-searches=", DEFAULT_MAX_SEARCHES)
    concept = " ".join(positional(args)).strip()
    if not concept:
        return out.refused(f"/web-search requires a concept. Usage: {c.row.synopsis}")

    with c.progress(out) as renderer:
        try:
            note_rel = web_research(
                concept, max_searches=max_searches,
                tool_progress_callback=renderer,
            )
            # escape(), not markup=False: a note title Rich reads as a tag gets
            # silently eaten (the user is told a path that is not the file's
            # name), and a URL carrying `[/x]` raises MarkupError straight out
            # of the except that exists to report the failure. Escaping only the
            # interpolated value keeps the styling on the rest of the line.
            out.print(
                f"  Findings → [bold]{escape(note_rel)}[/]"
                "  (review, then /nucleate to bring it in)"
            )
        except Exception as e:  # missing key, no findings, convergence guard, network
            out.print(f"  [yellow]web-search failed: {escape(str(e))}[/]")
    return out


def fetch(c: Call) -> Result:
    """/fetch <url> — one page (or video transcript) into an inbox note."""
    args, out = c.args, c.out
    from rich.markup import escape

    from silica.sources.web_research import fetch_to_inbox
    url = " ".join(args).strip()
    if not url:
        return out.refused(f"/fetch requires a URL. Usage: {c.row.synopsis}")

    try:
        note_rel = fetch_to_inbox(url)
        out.print(
            f"  Fetched → [bold]{escape(note_rel)}[/]"
            "  (review, then /nucleate to bring it in)"
        )
    except Exception as e:  # SSRF guard, bot wall, missing yt-dlp, network
        out.print(f"  [yellow]fetch failed: {escape(str(e))}[/]")
    return out


def keep(c: Call) -> Result:
    """/keep — save the last web answer as an inbox note."""
    out = c.out
    from rich.markup import escape

    from silica.sources.web_research import keep_last

    try:
        note_rel = keep_last()
        out.print(
            f"  Kept → [bold]{escape(note_rel)}[/]"
            "  (review, then /nucleate to bring it in)"
        )
    except Exception as e:  # empty slot, name collision, write refused
        out.print(f"  [yellow]keep failed: {escape(str(e))}[/]")
    return out
