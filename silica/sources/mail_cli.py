# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/mail: mail threads from a file the user exported (ADR-0090 "GUI and TUI").

Out of cli.py for the reason zotero_cli.py is: its usage strings start with a
slash, and cli.py's slash literals are read as commands by the registry parity
test. A dry run until `--apply`, the `/zotero file` pattern: the user's hand
on the file and on the flag is the confirmation (ADR-0090 point 5).
"""
from __future__ import annotations

import os
import re

from rich.markup import escape

USAGE = ("  Usage: /mail · /mail import <file.mbox|file.eml> [--label=X] [--me=ADDR] [--all] "
         "[--limit=N] [--apply]")


# A flag and its value, quoted or not: `--label="Category Promotions"` is one
# value, where a whitespace split cut it in two and the second half joined the
# file name ("file not found", 2026-10-12).
_FLAG_RE = re.compile(r"""(?<!\S)--([\w-]+)(?:=("[^"]*"|'[^']*'|\S*))?""")


def _parse(raw: str) -> tuple[str, str, dict[str, str]]:
    """(verb, file, flags). The file is every word after the verb, joined: a
    Takeout export is "All mail Including Spam and Trash.mbox", and asking the
    user to quote it would make the one file every Gmail user has the awkward one."""
    flags: dict[str, str] = {}

    def take(m: re.Match) -> str:
        value = m.group(2) or ""
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        flags[m.group(1)] = value
        return " "

    rest = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""
    words = _FLAG_RE.sub(take, rest).split()
    verb = words[0] if words else ""
    file = " ".join(words[1:]).strip("'\"")
    return verb, os.path.expanduser(file), flags


def run(raw: str, console) -> None:
    from silica.sources import mail

    verb, file, flags = _parse(raw)
    if verb == "":
        try:
            _status(console)
        except ValueError as exc:
            console.print(f"  [yellow]Mail: {escape(str(exc))}[/]")
        return
    if verb != "import" or not file:
        console.print(USAGE)
        return
    me = tuple(a for a in (flags.get("me") or "").split(",") if a.strip())
    try:
        limit = mail.parse_limit(flags.get("limit"))
        if me:
            # Kept on the user's word, dry run or not: typing the address is
            # the confirmation the prefill asks for (point 4).
            me = mail.remember_addresses(me)
        p = mail.plan(file, me=me or None, label=flags.get("label", ""),
                      all_="all" in flags, limit=limit)
    except (OSError, ValueError) as exc:
        console.print(f"  [yellow]Mail: {escape(str(exc))}[/]")
        return
    _show(p, file, limit, console)
    if "apply" not in flags:
        return
    if p.guessed and not p.takeout:
        console.print(f"  Confirm your address first: /mail import {escape(file)} "
                      f"--me={escape(','.join(p.me)) or 'you@example.org'} --apply")
        return
    _apply(p, console)


def _show(p, file: str, limit: int, console) -> None:
    from silica.sources import mail

    who = ("by the Sent label (Takeout)" if p.takeout
           else f"from {escape(', '.join(p.me)) or 'no address'}")
    # escape() on everything a file or a stranger named: "[/x]" in a subject
    # is a MarkupError, and "[link=...]" a live terminal link.
    console.print(f"  {escape(p.file.name)} · {p.in_file} threads in the file · "
                  f"{p.took_part} you took part in, {who}")
    if p.guessed and not p.takeout and p.me:
        guess = escape(p.me[0])
        console.print(f"  Your address, guessed from the file: {guess}. Confirm it with "
                      f"--me={guess} (or give yours, comma-separated); it is kept for this vault.")
    console.print(f"  {len(p.write)} would be written ({p.grown} grew since the last import), "
                  f"{p.present} already in the vault, {p.over_limit} over the limit of {limit}")
    for t in p.write:
        first = t.mails[0]
        when = first.date.date().isoformat() if first.date else "undated"
        console.print(f"    {when}  {escape(first.subject[:60] or 'no subject')}  "
                      f"· {len(t.mails)} message(s)  → {escape(mail.note_path(t))}")
    hint = "--apply writes them in one run, undone by /revert"
    if p.added_by_all:
        hint += f" · --all adds {p.added_by_all} thread(s) you took no part in"
    console.print(f"  [dim]{hint}[/]")


def _apply(p, console) -> None:
    from silica.sources import mail

    out = mail.apply(p)
    for subject, why in out["failed"]:
        console.print(f"  [yellow]{escape(subject[:60])}: {escape(why)}[/]")
    console.print(f"  Wrote {out['written']} note(s) under sources/mail · undo: /revert")


def _status(console) -> None:
    from silica.sources import mail

    state = mail.load_state()
    me = state.get("me") or []
    console.print(f"  Your addresses: {escape(', '.join(me))}" if me else
                  "  Your addresses: none yet; /mail import <file> guesses one for you to confirm")
    threads, runs = mail.imported_threads(), state.get("imports") or []
    line = f"  {len(threads)} thread{'s' if len(threads) != 1 else ''} imported in {len(runs)} run(s)"
    if runs:
        last = runs[-1]
        line += (f"; last {last.get('at', '')}: {escape(str(last.get('file', '')))}, "
                 f"{last.get('written', 0)} written")
    console.print(line)
    g6 = mail.gate6()
    console.print(f"  [dim]Gate 6: {g6['nucleated']} of the first {g6['threads']} imported "
                  f"thread(s) carry a PDF you then nucleated; attachments get their own ADR at "
                  f"{mail.GATE6_FIRES} of {mail.GATE6_WINDOW}[/]")
