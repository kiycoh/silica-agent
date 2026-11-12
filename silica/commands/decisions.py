# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/decide, /outcome, /decisions: the ledger of decisions and what came of them (ADR-0167)."""
from __future__ import annotations

from silica.command_table import Call, Result
from silica.commands import UNREADABLE


def prose(args: list[str], flags: tuple[str, ...]) -> tuple[list[str], dict[str, str]]:
    """(the words before any flag, each flag's words). A flag's value runs to
    the next flag, so `--why costs went up` needs no quotes: a decision is
    prose, and the words after a flag belong to it, not to the decision."""
    head: list[str] = []
    vals: dict[str, list[str]] = {}
    cur = head
    for a in args:
        name, eq, rest = a.partition("=")
        if name in flags:
            cur = vals.setdefault(name, [])
            if eq:
                cur.append(rest)
            continue
        cur.append(a)
    return head, {k: " ".join(v).strip() for k, v in vals.items()}


def decide(c: Call) -> Result:
    """/decide <decision> [--why ...] [--expect ...] [--review YYYY-MM-DD] [--target DIR]:
    the person's words as a decision note, with no model in between, as
    /remember keeps a line."""
    out = c.out
    from rich.markup import escape

    from silica.kernel.calendar import decisions

    words, opts = prose(c.args, ("--why", "--expect", "--review", "--target"))
    text = " ".join(words).strip()
    if not text:
        out.print(f"  Usage: {c.row.synopsis}")
        return out
    try:
        res = decisions.create(text, why=opts.get("--why", ""), expect=opts.get("--expect", ""),
                               review=opts.get("--review", ""), target=opts.get("--target", ""))
    except (ValueError, FileExistsError) as e:
        out.print(f"  [yellow]{escape(str(e))}[/]")
        return out
    review = f", review on {opts['--review']}" if opts.get("--review") else ""
    out.print(f"  Decided: [bold]{escape(res['path'])}[/]{escape(review)}. "
              f"[dim]/outcome adds what came of it; /undo {escape(res['path'])} takes it back.[/]")
    return out


def outcome(c: Call) -> Result:
    """/outcome <note> <what happened> [--status kept|changed|reversed]: one
    dated line under the decision's Outcomes."""
    out = c.out
    from rich.markup import escape

    from silica.kernel.calendar import decisions

    words, opts = prose(c.args, ("--status",))
    if len(words) < 2:
        out.print(f"  Usage: {c.row.synopsis}")
        return out
    note, what = words[0], " ".join(words[1:])
    try:
        res = decisions.outcome(note, what, opts.get("--status", "").lower())
    except UNREADABLE as e:
        out.print(f"  [yellow]Cannot read '{escape(note)}': {escape(str(e))}[/]")
        return out
    except ValueError as e:
        out.print(f"  [yellow]{escape(str(e))}[/]")
        return out
    status = opts.get("--status")
    out.print(f"  Outcome added to [bold]{escape(res['path'])}[/]"
              f"{escape(f', now {status.lower()}') if status else ''}. [dim]/undo {escape(res['path'])} takes it back.[/]")
    return out


def listing(c: Call) -> Result:
    """/decisions [open|due|all]: the ledger, due ones first."""
    out = c.out
    from pathlib import Path

    from rich.markup import escape

    from silica.config import CONFIG
    from silica.kernel.calendar import decisions

    which = (c.args[0].lower() if c.args else "open")
    if which not in ("open", "due", "all"):
        out.print(f"  Usage: {c.row.synopsis}")
        return out
    rows = decisions.scan(Path(CONFIG.vault_path), which)
    if not rows:
        out.print({"open": "  No open decisions.", "due": "  No decision is due for review.",
                   "all": "  No decisions yet: /decide writes one."}[which])
        return out
    out.print(f"  {len(rows)} decision{'s' if len(rows) != 1 else ''} ({which}):")
    for r in rows:
        when = f" · review {r['review_on']}{' (due)' if r['due'] else ''}" if r["review_on"] else ""
        n = len(r["outcomes"])
        out.print(f"  · [bold]{escape(r['path'])}[/] {escape('[' + r['status'] + ']')} · decided {escape(r['decided'])}"
                  f"{escape(when)} · {n} outcome{'s' if n != 1 else ''}")
        if r["expected"]:
            out.print(f"      expected: {escape(' '.join(r['expected'].split())[:160])}")
        if r["outcomes"]:
            last = r["outcomes"][-1]
            out.print(f"      last outcome: {escape((last['date'] + ': ' if last['date'] else '') + last['text'][:160])}")
    return out
