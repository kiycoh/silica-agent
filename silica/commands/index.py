# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/embed, /cooccur, /lexical, /wiki, /graph, /map: what is built from the vault."""
from __future__ import annotations

import json

from silica.command_table import Call, Result
from silica.config import CONFIG
from silica.session.words import positional


_REFRESH = {
    "/embed": ("silica_embed_refresh", ""),
    "/cooccur": ("silica_cooccurrence_refresh", " (co-occurrence)"),
    "/lexical": ("silica_lexical_refresh", " (lexical)"),
}


def _refresh(c: Call, cmd: str) -> Result:
    """/embed|/cooccur|/lexical [folder] [--force] — rebuild one index.

    One body, three commands: `_REFRESH` maps each to its tool and its label.
    """
    from silica.tools import TOOLS

    args, out = c.args, c.out
    tool, label = _REFRESH[cmd]
    folder = ""
    for part in args:
        if part.startswith("--folder="):
            folder = part[len("--folder="):]
        elif not part.startswith("-"):
            folder = part
    result = TOOLS[tool].run(folder=folder, force="--force" in args)
    try:
        parsed = json.loads(result)
        if "error" in parsed:
            out.print(f"  [red]Error:[/] {parsed['error']}")
        else:
            out.print(
                f"  Indexed: [bold]{parsed.get('indexed', '?')}[/] / "
                f"{parsed.get('total_notes', '?')} notes{label}"
            )
        if parsed.get("read_errors"):
            out.print(f"  [yellow]Read errors:[/] {parsed['read_errors']}")
    except Exception:
        # A tool's answer that is not the JSON above: said as it came, since a
        # refresh that ran and cannot be read back is still worth showing.
        out.print(result)
    return out


def embed(c: Call) -> Result:
    return _refresh(c, "/embed")


def cooccur(c: Call) -> Result:
    return _refresh(c, "/cooccur")


def lexical(c: Call) -> Result:
    return _refresh(c, "/lexical")


def wiki(c: Call) -> Result:
    """/wiki [folder] — prose pass over the staged code notes."""
    args, out = c.args, c.out
    folder = next(iter(positional(args)), "") or None
    overview_only = "--overview-only" in args
    force = "--force" in args
    from silica.capabilities.codewiki import run_wiki
    result = run_wiki(CONFIG, folder=folder,
                      overview_only=overview_only, force=force)
    if result["status"] == "no_repo":
        out.print("  [yellow]wiki: vault is not inside a git repo, nothing to describe.[/]")
    elif result["status"] == "empty":
        out.print("  [yellow]wiki: no supported source files found "
                  "(code lane parses Python/TypeScript/JavaScript only).[/]")
    elif result["status"] == "error":
        out.print(f"  [yellow]wiki: {result.get('reason', 'error')}[/]")
    else:
        out.print(
            f"  wiki: {len(result['written'])} note(s) written, "
            f"{len(result['skipped'])} up-to-date"
            + (f", {result['parse_errors']} file(s) not analyzable" if result["parse_errors"] else "")
        )
        for fail in result.get("failed", []):
            out.print(f"  [red]wiki: write failed:[/] {fail['path']}: {fail['reason']}")
    return out


def graph(c: Call) -> Result:
    """/graph [output.html] [folder] — export the vault graph and open it."""
    args, out = c.args, c.out
    from silica.tools.graph import graph_export

    output_path = "graph.html"
    folder = ""
    named = positional(args)
    if named:
        output_path = named[0]
    if len(named) > 1:
        folder = named[1]
    # The user's own command keeps any path; the model's tool is the one held
    # to the vault.
    result = graph_export(output_path=output_path, folder=folder)
    out.print(f"  Graph written to: [bold]{result.get('path', output_path)}[/]")
    return out


def mind_map(c: Call) -> Result:
    """/map <note> [--force] — the association field around one note."""
    args, out = c.args, c.out
    from silica.tools import TOOLS

    force = "--force" in args
    note = " ".join(positional(args)).strip()
    if not note:
        out.print("  Usage: /map <note> [--force]")
        return out
    result = TOOLS["silica_mindmap"].run(note_path=note, force=force)
    try:
        parsed = json.loads(result)
        if parsed.get("skipped"):
            out.print(
                f"  [yellow]Map already present[/] ({parsed['skipped']}), not "
                "overwritten. Use [bold]/map <note> --force[/] to regenerate."
            )
        elif "error" in parsed:
            out.print(f"  [red]{parsed['error']}[/]")
        else:
            out.print(
                f"  Map written: [bold]{parsed.get('path', '?')}[/] "
                f"({parsed.get('nodes', '?')} nodes, {parsed.get('edges', '?')} edges)"
            )
    except Exception:
        # Not the JSON the tool answers with: said as it came.
        out.print(result)
    return out
