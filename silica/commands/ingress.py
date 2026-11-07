# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What brings a source in: /nucleate, /promote, /convert, /web-search, /fetch, /keep."""
from __future__ import annotations

from silica.command_table import Call, Result
from silica.session.words import int_flag, positional, str_flag
from silica.sources import ingest


def nucleate(c: Call) -> Result:
    """/nucleate <file|folder|glob...> [--target=DIR] [--hub=H] [--profile=P]
    [--seen=YYYY-MM-DD] [--no-keep-sources] — ingest sources into vault notes.

    No arguments drains the write-ahead log instead. Fully inline: returns "" when
    it dispatched, or an agent-directed message when the target cannot be resolved
    from the arguments alone.
    """
    out = c.out
    args = ingest.rejoin_spaced_paths(c.args)
    if not args:
        return out.to_model(ingest.drain_wal(out))
    seen = str_flag(args, "--seen")  # capture clock: the day the events happened
    if seen:
        # Trust boundary: this string becomes the valid_from on every claim
        # of the run — a typo'd date would poison note_clock vault-wide.
        import datetime as _dt
        try:
            seen = _dt.date.fromisoformat(seen).isoformat()
        except ValueError:
            out.print(f"  [yellow]--seen ignored: {seen!r} is not YYYY-MM-DD[/]")
            seen = ""
    return out.to_model(ingest.nucleate(
        positional(args),  # preserve original case
        target_dir=str_flag(args, "--target"),
        hub=str_flag(args, "--hub"),
        seen=seen,
        # Explicit lens override: tops the whole form ladder
        # (docs/specs/nucleation-forms.md), filing included.
        profile=str_flag(args, "--profile"),
        # On by default: the leaf lives in `sources/`, which every index built on
        # the vault listing leaves out (is_source_leaf: embeddings, the graph, the
        # driver's search), while the passage search behind silica_search_context
        # walks it and returns it (kernel/recall/paths.py:235, probed 2026-11-04).
        # It is what makes a note's verbatim source reachable at all
        # (reliability_tier reads exactly that). --no-keep-sources opts out.
        keep_sources="--no-keep-sources" not in args,
        user_input=c.raw,
        console=out,
        ask=c.ask and c.ask.text,
    ))


def promote(c: Call) -> Result:
    """/promote [<key>]: session memory into a note, through the gate."""
    return c.out.to_model(ingest.promote(positional(c.args), c.out))


def convert(c: Call) -> Result:
    """/convert <file...> [--target=DIR] — document to inbox notes, no distillation."""
    out = c.out
    args = ingest.rejoin_spaced_paths(c.args)
    files = positional(args)
    target_dir = str_flag(args, "--target")
    if not files:
        return out.refused("/convert requires at least one file path. Usage: /convert <file...> [--target=DIR]")
    from silica.sources.convert import convert
    for f in files:
        try:
            paths = convert(f, dest_dir=target_dir)
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
        return out.refused('/web-search requires a concept. Usage: /web-search "<concept>" [--max-searches=N]')

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
        return out.refused("/fetch requires a URL. Usage: /fetch <url>")

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
