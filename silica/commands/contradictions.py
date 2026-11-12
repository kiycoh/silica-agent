# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/contradictions: what the source checks found set against each other, proposed and never written (ADR-0166)."""
from __future__ import annotations

from silica.command_table import Call, Result
from silica.session.words import positional


def _short(text: str, n: int = 140) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= n else flat[:n - 1].rstrip() + "…"


def contradictions(c: Call) -> Result:
    """/contradictions [folder] [--check]: the pairs the source checks under
    `folder` found contradicting each other, read from their cache with no
    model. `--check` first checks the notes under it that were never checked,
    with the judge /check uses; a note checked before keeps its verdicts, and
    /check or the drawer brings it up to date after an edit."""
    out = c.out
    from rich.markup import escape

    from silica.config import CONFIG
    from silica.kernel import publish
    from silica.kernel.base.layout import iter_notes
    from silica.kernel.check import contradictions as cx
    from silica.kernel.check.run import read_cache
    from silica.session import check as check_wiring

    vault = CONFIG.vault_path
    if not vault:
        out.print("  No vault configured.")
        return out
    folder = " ".join(positional(c.args)).strip().strip("/")
    notes = [rel for rel, _f in iter_notes(vault, under=folder) if not publish.never(rel)]
    fresh = [rel for rel in notes if not read_cache(vault, rel)]
    where = folder or "the vault"
    if "--check" in c.args:
        for n, rel in enumerate(fresh, 1):
            try:
                report = check_wiring.run(rel)
            except Exception as e:
                # One note the model or the index fails on costs its own verdicts;
                # the sweep goes on, and the line says which and why.
                out.print(f"  [yellow]{n}/{len(fresh)} {escape(rel)}: the check failed: {escape(str(e))}[/]")
                continue
            hit = sum(f["kind"] == "contradicted" for f in report["findings"])
            out.print(f"  {n}/{len(fresh)} {escape(rel)}: {report['blocks']} blocks"
                      f"{f', {hit} contradicted' if hit else ''}")
        fresh = []
    found = cx.proposals(vault, folder)
    if not found:
        out.print(f"  No contradiction proposed under {escape(where)}.")
    else:
        out.print(f"  {len(found)} contradiction{'s' if len(found) != 1 else ''} proposed under {escape(where)}, "
                  "nothing written:")
        for p in found:
            out.print(f"  · [bold]{escape(p['path'])}:{p['line']}[/] {escape(_short(p['mine']))}")
            out.print(f"      against {escape(p['other_path'])}:{p['their_line']} {escape(_short(p['their']))}")
        out.print("  Set them side by side in the note's drawer in the app, where each is settled: "
                  "this note holds, the other holds, both hold when, or keep it open.")
    if fresh:
        out.print(f"  [dim]{len(fresh)} of {len(notes)} notes under {escape(where)} were never checked: "
                  f"/contradictions {escape(folder + ' ' if folder else '')}--check checks them, one model call "
                  "per block the rules do not settle.[/]")
    return out
