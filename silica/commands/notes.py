# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What is done to one note or one run: /verify, /check, /changes, /undo, /delete, /trash, /revert."""
from __future__ import annotations

from pathlib import Path

from silica.command_table import Call, Result
from silica.config import CONFIG
from silica.session import check as check_wiring
from silica.session.words import positional, unquoted


def _quoted_flag(args: list[str], flag: str) -> tuple[str | None, list[str]]:
    """(`--flag value` or `--flag "two words"`, the args without it). A
    double-quoted name arrives whole (`direct_args`); a single-quoted one
    still arrives in pieces and is joined back here; None when the flag is
    absent, "" when it has no value."""
    for i, a in enumerate(args):
        if a == flag or a.startswith(flag + "="):
            head = a[len(flag) + 1:] if a.startswith(flag + "=") else (args[i + 1] if i + 1 < len(args) else "")
            j = i + 1 if a.startswith(flag + "=") else i + 2
            words = [head]
            if head[:1] in "\"'" and head and not (len(head) > 1 and head.endswith(head[0])):
                while j < len(args) and not words[-1].endswith(head[0]):
                    words.append(args[j])
                    j += 1
            value = " ".join(words).strip().strip("\"'").strip()
            if head.startswith("--"):
                value, j = "", j - 1
            return value, args[:i] + args[j:]
    return None, list(args)


def verify(c: Call) -> Result:
    """/verify <note> [--clear] — a person vouches for a note (OKF §5.2), bound
    to the body as it is now; --clear withdraws every human entry. The only
    writer of a `human:` attestation: the write tools refuse the key (spec M4)."""
    args, out = c.args, c.out
    import datetime
    import getpass

    from silica.config import hosted
    from silica.driver import DRIVER
    from silica.kernel.write.checkpoints import push_landed
    from silica.kernel.write.notetype import clear_verified, stamp_verified

    # --as "<name>" signs with that name. A stamp never reads or writes a
    # /check's state, nor a check the stamp (ADR-0110 point 1).
    signer, rest = _quoted_flag(args, "--as")
    name = " ".join(positional(rest))
    if not name or signer == "":
        out.print('  Usage: /verify <note> [--clear] [--as "<name>"]')
        return out
    try:
        nc = DRIVER.read_note(name)
    except Exception as e:
        out.print(f"  [yellow]Cannot read '{name}': {e}[/]")
        return out
    path, prior = nc.ref.path or name, nc.content or ""
    # On the server the login is the tenant's service user, which names no
    # one: there the stamp says `owner` unless the person signs (ADR-0110).
    login = signer.strip() if signer else ("owner" if hosted() else getpass.getuser())
    who, today = f"human:{login}", datetime.date.today().isoformat()
    try:
        new = clear_verified(prior) if "--clear" in args else stamp_verified(prior, who, today)
    except ValueError as e:
        out.print(f"  [yellow]{e}[/]")
        return out
    if new == prior:
        out.print(f"  {path}: nothing to change.")
        return out
    from silica.kernel.write.preserve import Loss

    DRIVER.overwrite(path, new, loss=Loss.PERSON)  # the person's own attestation
    push_landed(path, prior, new)  # /undo reaches it; the edited lane knows it
    if "--clear" in args:
        out.print(f"  {path}: human attestations cleared.")
    else:
        out.print(f"  {path}: verified by {who} at {today}.")
    return out


def check(c: Call) -> Result:
    """/check <note> — read a note the person wrote against the vault, block
    by block, and print the findings by grade (ADR-0110): the rules first,
    then Jev's verdict on each block they did not settle. It never edits the
    note."""
    args, out = c.args, c.out
    import time

    from silica import metrics
    from silica.config import CONFIG
    from silica.driver import DRIVER
    from silica.kernel.check import run as check
    from silica.kernel.base.manifest import active_write_dir

    name = " ".join(positional(args))
    if not name:
        out.print("  Usage: /check <note>")
        return out
    try:
        path = DRIVER.read_note(name).ref.path or name
    except Exception as e:
        out.print(f"  [yellow]Cannot read '{name}': {e}[/]")
        return out
    path = path if path.endswith(".md") else path + ".md"
    started = time.monotonic()
    search, dense, floor = check_wiring.legs()
    try:
        report = check.check_note(path, CONFIG.vault_path, write_dir=active_write_dir(),
                                  search=search, dense=dense, dense_floor=floor, judge=check_wiring.judge(cite=False))
    except Exception as e:
        metrics.emit("check", outcome=metrics.outcome_of(e), ms=metrics.ms_since(started))
        out.print(f"  [yellow]The check failed: {e}[/]")
        return out
    counts = report["counts"]
    metrics.emit("check", outcome="ok", ms=metrics.ms_since(started), blocks=report["blocks"], cached=report["cached"],
                 error=counts["error"], warning=counts["warning"], nit=counts["nit"])
    out.print(check.render(report), markup=False, highlight=False)
    return out


def changes(c: Call) -> Result:
    """/changes — every note this session wrote to, with its line tally."""
    out = c.out
    from rich.markup import escape

    from silica.kernel.write import session_changes

    rows = session_changes.rows()
    if not rows:
        out.print("  Nothing changed this session.")
        return out
    # Single letters, not words: the point of the column is that the eye skips it
    # unless something is unusual, and `git status`'s letters are already learned.
    letter = {"created": "A", "modified": "M", "deleted": "D", "moved": "R"}
    # Padding is computed on the plain text and applied by hand: rich markup adds
    # characters that f-string alignment would count, so `:>` would stagger the
    # columns by the length of whichever colour tag happens to be on the row.
    width = max(len(r["path"]) for r in rows)
    add_w = max(len(f"+{r['added']}") for r in rows)
    del_w = max(len(f"-{r['removed']}") for r in rows)
    total_add = total_del = 0
    for r in rows:
        total_add += r["added"]
        total_del += r["removed"]
        plus = f"+{r['added']}" if r["added"] else ""
        minus = f"-{r['removed']}" if r["removed"] else ""
        cells = (f"{' ' * (add_w - len(plus))}[green]{plus}[/] "
                 f"[red]{minus}[/]{' ' * (del_w - len(minus))}")
        origin = f"  [dim]← {escape(r['from'])}[/]" if r["from"] else ""
        out.print(
            (f"  [dim]{letter.get(r['kind'], '?')}[/] [bold]{escape(r['path'])}[/]"
             f"{' ' * (width - len(r['path']))}  {cells}{origin}").rstrip()
        )
    out.print(
        f"  [dim]{len(rows)} note(s), +{total_add} -{total_del} — "
        f"/undo <note-path> takes one back, /revert the whole run.[/]"
    )
    return out


def undo(c: Call) -> Result:
    """/undo [note-path]: revert the last write to one note, bring back a
    note the chat or /delete deleted, or move back a note the chat moved."""
    raw_input, out = c.raw, c.out
    from silica.driver import DRIVER
    from silica.kernel.write.checkpoints import get_checkpoint_store
    from silica.kernel.link.sweep import revert_and_sweep
    from silica.kernel.write.undo_journal import USER_DELETE_SOURCE, get_undo_journal

    store = get_checkpoint_store()
    journal = get_undo_journal()
    vault = (CONFIG.vault_path or "").strip() or None

    def _on_disk(p: str) -> bool:
        root = Path(vault) if vault else None
        return root is None or (root / p).exists() or (root / f"{p}.md").exists()

    # Everything after the command IS the path: `args` is a plain whitespace
    # split, so `/undo silica/Verdetto reranker.md` used to look up
    # "silica/Verdetto" and report nothing to undo. Note names with spaces are
    # the common case in a vault, and the web GUI's per-note revert sends this.
    def _written_since(p: str, t: float) -> bool:
        return any((store.latest_at(q) or 0.0) > t for q in (p, f"{p}.md"))

    rest = raw_input.strip().split(maxsplit=1)
    deleted = moved = None
    note_path: str | None
    if len(rest) > 1:
        note_path = unquoted(rest[1])
        if store.depth(note_path) == 0:
            # The write card sends the tool's own argument, often a bare name,
            # while the store keys a patch on the path it resolved to: unresolved,
            # the card's revert said "nothing to undo" over a patch that had
            # landed (2026-10-26).
            try:
                resolved = DRIVER.read_note(note_path).ref.path
            except Exception:
                # No note answers to the name: a deleted note is looked up by
                # the name as given, just below.
                resolved = ""
            if resolved and store.depth(resolved):
                note_path = resolved
        if not _on_disk(note_path):
            deleted = journal.last_delete(vault, note_path)
        # A patch the note took after it landed is undone before the move.
        move = None if deleted else journal.last_chat_move(vault, note_path)
        if move and not _written_since(move[2], move[3]):
            moved = move
    else:
        # The newest action wins, a write, a delete or a move: a delete and a
        # move live in the undo journal, not in the checkpoint store.
        last_write = store.most_recent()
        newest = last_write[1] if last_write else None
        last_delete = journal.last_delete(vault)
        if last_delete and not _on_disk(last_delete[1]) and (
                newest is None or last_delete[2] >= newest):
            deleted, newest = last_delete, last_delete[2]
        last_move = journal.last_chat_move(vault)
        if last_move and (newest is None or last_move[3] >= newest):
            deleted, moved = None, last_move
        note_path = deleted[1] if deleted else (last_write[0] if last_write else None)
    if moved:
        res = revert_and_sweep(moved[0])
        if res["reverted"]:
            out.print(f"  Moved back: [bold]{moved[2]}[/] → {moved[1]}")
        else:
            why = (res["skipped"] or res["errors"] or [{"reason": "unknown"}])[0]
            out.print(f"  [yellow]Not moved back:[/] {moved[2]} "
                      f"({why.get('reason') or why.get('error')})")
        return out
    if deleted:
        user_deleted = (journal.run_info(deleted[0]) or {}).get("source") == USER_DELETE_SOURCE
        res = revert_and_sweep(deleted[0])
        if res["reverted"]:
            # user:/delete is /delete and the tree's row menu alike (ADR-0123):
            # naming the command misnamed a delete the person made with a click
            how = "you deleted it" if user_deleted else "deleted from the chat"
            out.print(f"  Restored: [bold]{deleted[1]}[/] ({how})")
        else:
            why = (res["skipped"] or res["errors"] or [{"reason": "unknown"}])[0]
            out.print(f"  [yellow]Not restored:[/] {deleted[1]} "
                      f"({why.get('reason') or why.get('error')})")
        return out
    if not note_path:
        out.print("  Nothing to undo — no patches recorded in this session.")
        return out

    try:
        on_disk = DRIVER.read_note(note_path).content
    except Exception:
        on_disk = None  # nothing on disk to keep: the undo writes the note back
    since = store.changed_since(note_path, on_disk)
    # Read before the undo: written over blind, an edit made in Obsidian after
    # Silica's write was lost for good (ADR-0144, reproduced 2026-11-01).
    content = store.undo(note_path, current=on_disk)
    if content is None:
        out.print(f"  [yellow]Nothing to undo for[/] {note_path} (already at original).")
        return out
    kept = {"hand": "; your edits made after it are taken back too and kept in the note's history",
            "run": "; a later run's changes to it are taken back too and kept in the note's history",
            }.get(since or "", "")

    if store.absent_floor(note_path):
        # The chat created this note: before it there was nothing, so undo
        # removes it rather than leave a 0-byte note that keeps taking links.
        # Removed with a restore point, so a second /undo brings it back: on
        # hosted t06 (2026-10-22) a write card's revert removed a 21k-char note
        # the turn had written and nothing in the app could restore it.
        from silica.kernel.write.gate import delete_with_restore_point

        try:
            res = delete_with_restore_point(
                note_path, DRIVER.read_note(note_path).content, USER_DELETE_SOURCE)
        except Exception as exc:
            res = {"error": str(exc)}
        if "error" in res:
            out.print(f"  [red]Undo failed:[/] {res['error']}")
            return out
        store.clear(note_path)
        out.print(f"  Undone: [bold]{note_path}[/] removed (the chat created it); "
                  f"/undo {note_path} brings it back")
        return out
    from silica.kernel.write.preserve import Loss

    try:
        DRIVER.overwrite(note_path, content, loss=Loss.RESTORE)
        try:
            store.settle(note_path, DRIVER.read_note(note_path).content)  # what landed, stamp included
        except Exception:
            pass  # the undo landed; an unsettled top only labels the next write as an outside edit
        depth = store.depth(note_path)
        remaining = max(0, depth - 1)
        out.print(f"  Undone: [bold]{note_path}[/]{kept}  [dim]({remaining} undo step(s) remaining)[/]")
    except Exception as exc:
        out.print(f"  [red]Undo failed:[/] {exc}")
    return out


def delete(c: Call) -> Result:
    """/delete <path>: delete a note as the user's own act (ADR-0092). The
    model's silica_delete refuses a note other notes link to; this is where
    the user deletes it, with no model in between, the way /remember writes
    past the memory gate. Journaled under user:/delete, so /undo brings it
    back; it prints each link the delete left pointing to no note and each
    note it left with no incoming link."""
    raw_input, out = c.raw, c.out
    from rich.markup import escape

    from silica.driver import DRIVER
    from silica.kernel.write.gate import delete_with_restore_point
    from silica.kernel.write.undo_journal import USER_DELETE_SOURCE

    # Everything after the command is the path, as for /undo: note names
    # with spaces are the common case.
    rest = raw_input.strip().split(maxsplit=1)
    ref = unquoted(rest[1]) if len(rest) > 1 else ""
    if not ref:
        out.print("  Usage: /delete <path>  [dim](the note to delete; /undo brings it back)[/]")
        return out
    try:
        nc = DRIVER.read_note(ref)
        path = nc.ref.path or ref
        res = delete_with_restore_point(path, nc.content, USER_DELETE_SOURCE)
    except Exception as exc:
        res = {"error": str(exc)}
    if "error" in res:
        err = res["error"].removeprefix("not deleted: ")
        out.print(f"  [yellow]Not deleted:[/] {escape(ref)} ({escape(err)})", soft_wrap=True)
        return out
    # soft_wrap: a vault path is long, and a link cut across two lines in
    # the GUI's code block reads as two entries.
    out.print(f"  Deleted: [bold]{escape(path)}[/]", soft_wrap=True)
    if dangling := res["dangling"]:
        out.print(f"  {len(dangling)} link{'s' if len(dangling) != 1 else ''} now point"
                  f"{'' if len(dangling) != 1 else 's'} to no note:")
        for d in dangling:
            out.print(f"    {escape(d['note'])} → {escape('[[' + d['link'] + ']]')}",
                      soft_wrap=True)
    if orphaned := res["orphaned"]:
        out.print(f"  {len(orphaned)} note{'s' if len(orphaned) != 1 else ''} left with "
                  "no incoming link:")
        for o in orphaned:
            out.print(f"    {escape(o)}", soft_wrap=True)
    out.print(f"  [dim]In the trash: /undo {escape(path)} brings it back.[/]", soft_wrap=True)
    return out


def trash(c: Call) -> Result:
    """/trash [restore <path> | erase <path> | empty]: the notes deleted from
    this vault, by the user or the chat, that nothing has brought back
    (ADR-0128). restore brings one note back, not the run it was deleted in;
    erase and empty delete for good what the trash holds."""
    raw_input, out = c.raw, c.out
    from datetime import datetime as _dt

    from rich.markup import escape

    from silica.kernel.base.notekey import note_key
    from silica.kernel.write.gate import restore_from_trash
    from silica.kernel.write.undo_journal import USER_DELETE_SOURCE, get_undo_journal

    journal = get_undo_journal()
    items = journal.trashed((CONFIG.vault_path or "").strip() or None)
    rest = raw_input.strip().split(maxsplit=2)
    verb = rest[1].lower() if len(rest) > 1 else ""
    if verb in ("", "empty") and not items:
        out.print("  The trash is empty.")
        return out
    if not verb:
        out.print(f"  Trash: {len(items)} note{'s' if len(items) != 1 else ''}")
        for t in items:
            when = _dt.fromtimestamp(t["deleted_at"]).strftime("%Y-%m-%d %H:%M")
            who = "you" if t["source"] == USER_DELETE_SOURCE else "the chat"
            out.print(f"    {escape(t['path'])}  [dim]{when}, {who}[/]", soft_wrap=True)
        out.print("  [dim]/trash restore <path> brings one back, /trash erase <path> "
                  "or /trash empty deletes for good.[/]")
        return out
    if verb == "empty":
        for t in items:
            journal.leave_trash(t["run"], t["path"])
        out.print(f"  Deleted for good: {len(items)} note{'s' if len(items) != 1 else ''}.")
        return out
    if verb not in ("restore", "erase") or len(rest) < 3:
        out.print("  Usage: /trash [restore <path> | erase <path> | empty]")
        return out
    # Everything after the verb is the path, as for /undo. A path deleted
    # twice is two items; the newest delete is the one meant.
    want = note_key(unquoted(rest[2]))
    item = next((t for t in items if note_key(t["path"]) == want), None)
    if item is None:
        out.print(f"  [yellow]Not in the trash:[/] {escape(unquoted(rest[2]))}", soft_wrap=True)
        return out
    if verb == "erase":
        journal.leave_trash(item["run"], item["path"])
        out.print(f"  Deleted for good: [bold]{escape(item['path'])}[/]", soft_wrap=True)
        return out
    why = restore_from_trash(item, store=journal)
    if why:
        out.print(f"  [yellow]Not restored:[/] {escape(item['path'])} ({escape(why)})",
                  soft_wrap=True)
    else:
        out.print(f"  Restored: [bold]{escape(item['path'])}[/]", soft_wrap=True)
    return out


def revert(c: Call) -> Result:
    """/revert [run-id | --source <file>] — undo a journalled run, or every run
    that derived notes from one source."""
    args, raw_input, out = c.args, c.raw, c.out
    from rich.markup import escape

    from silica.kernel.write.undo_journal import get_undo_journal
    vault = CONFIG.vault_path.strip() or None
    tokens = list(args) or raw_input.strip().split()[1:]
    # The source is the rest of the line: lecture files carry spaces
    # ("CorsoA/Lezione 1.md"), and one token cut every such name at its first.
    source = ""
    for i, t in enumerate(tokens):
        if t == "--source" or t.startswith("--source="):
            source = " ".join([t.split("=", 1)[1], *tokens[i + 1:]]) if "=" in t else " ".join(tokens[i + 1:])
            break
    source = source.strip().strip("\"'")
    if source:
        return _revert_by_source(source, vault, out)
    parts_split = raw_input.strip().split(maxsplit=1)
    given = unquoted(parts_split[1]) if len(parts_split) > 1 else ""
    if not given:
        run_id = _latest_revertable(vault)
        if not run_id:
            out.print("  Nothing to revert — no runs recorded for this vault.")
            return out
        return _revert_one(run_id, out)
    found = get_undo_journal().match_runs(given, vault)
    ledgers = {ledger for _run, ledger in found}
    if any(run == given for run, _l in found):
        runs = [given]
    elif found and len(ledgers) == 1 and (next(iter(ledgers)) or "").startswith(given.lower()):
        # The id /status prints is the ledger's, and one ledger run can hold
        # several journal runs: newest first, the only sound replay order.
        runs = [run for run, _l in found]
    elif len(found) == 1:
        runs = [found[0][0]]
    elif found:
        out.print(f"  '{escape(given)}' names {len(found)} runs; give more of the id:")
        for run, ledger in found[:10]:
            out.print(f"    {run}  [dim](run {(ledger or '')[:8]})[/]")
        return out
    else:
        # Not a journal run: a Zotero write's run, which has no inverse in the
        # journal and is taken back by _revert_zotero alone, keeps working as
        # it did when every id passed straight through.
        runs = [given]
    for run_id in runs:
        _revert_one(run_id, out)
    return out


def _revert_by_source(source: str, vault: str | None, out: Result) -> Result:
    """Every journalled run the ledger attributes to `source`, newest first,
    scoped to that source's notes; what the journal does not hold is named,
    never guessed at (legacy runs predate the join and stay /revert <run-id>)."""
    from rich.markup import escape

    from silica.kernel.link.sweep import sweep_survivors
    from silica.kernel.write.gate import revert_source

    res = revert_source(source, vault=vault, after_run=sweep_survivors)
    if res.get("ambiguous"):
        out.print(f"  {len(res['ambiguous'])} sources are named {escape(source)}; "
                  "nothing reverted. Name one:", soft_wrap=True)
        for k in res["ambiguous"]:
            out.print(f"    /revert --source {escape(k)}", soft_wrap=True)
        return out
    runs = res["runs"]
    reverted = sum(len(r["reverted"]) for r in runs)
    skipped = sum(len(r["skipped"]) for r in runs)
    errors = sum(len(r["errors"]) for r in runs)
    left = sum(u["notes"] for u in res["unrevertable"])
    line = (f"  Revert by source {source}: {len(runs)} run(s), {reverted} writes "
            f"reverted, {skipped} skipped, {errors} errors.")
    if res["unrevertable"]:
        line += (f" {len(res['unrevertable'])} run(s) not in the undo journal: "
                 f"{left} note(s) left in place (/revert <run-id> still applies "
                 f"to journalled runs).")
    if not runs and not res["unrevertable"]:
        line = f"  Nothing to revert — the ledger has no record of {source}."
    out.print(line)
    try:
        from silica.kernel.write.run_log import append_log_line, format_revert_event
        for r in runs:
            append_log_line(
                format_revert_event(source, len(r["reverted"]), len(r["skipped"])),
                r["run_id"],
            )
    except Exception:
        pass  # the journal is a courtesy, never a failure path
    return out


def _revert_one(run_id: str, out: Result) -> Result:
    """One run taken back, journal and Zotero, and the line that says what it was."""
    from silica.kernel.link.sweep import revert_and_sweep
    from silica.kernel.write.undo_journal import get_undo_journal

    # Name WHAT is being reverted: the journal's run ids live in a different
    # id-space than the progress ledger's (log.md), so a bare id told the
    # user nothing about which run they were about to undo.
    info = None
    try:
        info = get_undo_journal().run_info(run_id)
    except Exception:
        pass
    source = (info or {}).get("source") or ""
    when = ""
    if info and info.get("started_at"):
        from datetime import datetime as _dt
        when = _dt.fromtimestamp(info["started_at"]).strftime("%Y-%m-%d %H:%M")
    label = f" ({source}, started {when})" if source and when else ""
    res = revert_and_sweep(run_id)
    _revert_zotero(run_id, out)
    stale = len(res.get("stale", []))
    line = (
        f"  Revert {run_id[:8]}…{label}: {len(res['reverted'])} writes reverted, "
        f"{len(res['skipped'])} skipped (modified), "
        f"{stale} stale (vault changed), {len(res['errors'])} errors."
    )
    out.print(line)
    # log.md narrated the run's writes; it must narrate the take-back too.
    try:
        from silica.kernel.write.run_log import append_log_line, format_revert_event
        append_log_line(
            format_revert_event(source, len(res["reverted"]), len(res["skipped"])),
            run_id,
        )
    except Exception:
        pass  # the journal is a courtesy, never a failure path
    return out


def _latest_revertable(vault: str | None) -> str | None:
    """The newest run with anything to undo. A run that only wrote to Zotero
    (/zotero file --apply) has no inverse in the undo journal, which holds
    vault files only, so the journal alone would skip it (ADR-0055 decision 8)."""
    from silica.kernel.write.undo_journal import get_undo_journal
    from silica.sources import zotero_write

    journal = get_undo_journal()
    newest = journal.last_active_run(vault=vault)
    zotero_run = zotero_write.last_run()
    if not zotero_run:
        return newest
    if not newest:
        return zotero_run
    started = (journal.run_info(newest) or {}).get("started_at") or 0
    return zotero_run if zotero_write.run_started(zotero_run) > started else newest


def _revert_zotero(run_id: str, out: Result) -> None:
    from silica.sources import zotero, zotero_write

    if not zotero_write.has_run(run_id):
        return
    try:
        res = zotero_write.revert(zotero.client(), run_id)
    except zotero.ZoteroError as exc:
        out.print(f"  [yellow]Zotero writes of this run not reverted: {exc}[/]")
        return
    out.print(f"  Zotero: {res['undone']} write(s) undone.")
    for key, why in res["left"]:
        out.print(f"  [yellow]left alone[/]  {key}: {why}")
