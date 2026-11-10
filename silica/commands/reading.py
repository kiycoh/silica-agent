# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What the vault says, read and printed: /find, /path, /contested, /plans, /agenda, /impact, /stale, /migrate."""
from __future__ import annotations

import json
import logging
import shlex

from silica.command_table import Call, Result
from silica.commands import UNREADABLE
from silica.config import CONFIG
from silica.session.words import int_flag, positional, str_flag

logger = logging.getLogger(__name__)


# /contested scan memo: (epoch, rows) — the scan parses every note's
# frontmatter, so it runs once per file-state epoch.
_CONTESTED_SCAN_MEMO: dict[str, tuple[str, list]] = {}


def find(c: Call) -> Result:
    """/find <query> [--k=N] [--min-trust=T] [--lifecycle=L] — semantic search,
    printed in the terminal. The two epistemic flags (spec M2) filter AFTER the
    search, so the printed count says what they cost."""
    args, out = c.args, c.out
    k = int_flag(args, "--k=", 5)
    min_trust = str_flag(args, "--min-trust").strip().lower()
    life = str_flag(args, "--lifecycle").strip().lower()
    query = " ".join(positional(args))
    if not query:
        out.print(f"  Usage: {c.row.synopsis}")
        return out
    from silica.kernel.recall.search import facade_search

    # The core and not the tool: the tool's payload drops `score` for the
    # model (graph._without_score), and this list shows it to the person.
    result = ""
    try:
        parsed = facade_search(query, k=k)
        result = json.dumps(parsed)
        results = parsed.get("results", [])
        if results and (min_trust or life):
            results, dropped = _epistemic_filter(results, min_trust, life)
            if dropped:
                out.print(f"  [dim]{dropped} result(s) filtered by "
                          f"{'--min-trust=' + min_trust if min_trust else ''}"
                          f"{' ' if min_trust and life else ''}"
                          f"{'--lifecycle=' + life if life else ''}[/]")
        if results:
            out.print(f"  Results for [bold]{query}[/] (top {len(results)}):")
            for r in results:
                score = r.get("score", 0.0)
                path = r.get("path", r.get("name", "?"))
                out.print(f"    [{score:.3f}] {path}")
        elif "error" in parsed:
            out.print(f"  [yellow]{parsed['error']}[/]")
        else:
            out.print(f"  No results for '{query}'.")
    except Exception:
        # `result` is what the search answered, or "" when it raised: as it came.
        out.print(result)
    return out


def _epistemic_filter(results: list[dict], min_trust: str, life: str) -> tuple[list[dict], int]:
    """`(kept, dropped)` of search rows under a trust floor and/or a lifecycle,
    read off each note's frontmatter (contested.trust / .lifecycle). A row
    whose note cannot be read is dropped: an unreadable note has no trust."""
    from silica.driver import DRIVER
    from silica.kernel.base import frontmatter
    from silica.kernel.write.contested import TRUST_ORDER, lifecycle, trust

    floor = TRUST_ORDER.get(min_trust, 0)
    kept: list[dict] = []
    for r in results:
        ref = r.get("path") or r.get("name") or ""
        try:
            content = DRIVER.read_note(ref if ref.endswith(".md") else ref + ".md").content or ""
        except UNREADABLE:
            continue
        data, _raw, _body = frontmatter.split(content)
        if TRUST_ORDER.get(trust(content), 0) < floor:
            continue
        if life and lifecycle(data) != life:
            continue
        kept.append(r)
    return kept, len(results) - len(kept)


def path(c: Call) -> Result:
    """/path <noteA> <noteB> — the shortest link path between two notes."""
    raw_input, out = c.raw, c.out
    from silica.kernel.recall.resolve import note_resolver
    from silica.kernel.graph.mindmap import reading_path
    try:
        toks = shlex.split(raw_input.strip())[1:]  # honours quoted titles with spaces
    except ValueError:
        # Not the row's usage: after an unbalanced quote, the form that quotes them.
        out.print('  Unbalanced quotes. Usage: /path "<note A>" "<note B>"')
        return out
    endpoints = positional(toks)
    if len(endpoints) != 2:
        out.print(f"  Usage: {c.row.synopsis}")
        return out
    resolve = note_resolver()
    src, dst = resolve(endpoints[0]), resolve(endpoints[1])
    for given, got in zip(endpoints, (src, dst)):
        if got is None:
            out.print(f"  Note not found: '{given}'")
    if src is None or dst is None:
        return out
    if src == dst:
        out.print("  Both resolve to the same note — nothing to walk.")
        return out
    # Weighted: a reading path wants the most coherent chain, not the fewest
    # hops — A/B on a live vault: weakest-link 0.87→0.97 for +0.14 hops.
    from silica.kernel.recall.requires import written_requires

    try:
        requires = written_requires(CONFIG.vault_path)
    except Exception:
        requires = {}  # an unreadable walk: the path runs on links and co-occurrence, as before
    path = reading_path(src, dst, weighted=True, requires=requires)
    if path is None:
        out.print(
            f"  No path between [bold]{src}[/] and [bold]{dst}[/] — "
            "not connected (try /map on each to see its neighborhood)."
        )
        return out
    out.print(f"  Reading path — {len(path) - 1} step(s):")
    for i, (node, leg) in enumerate(path):
        if leg != "start":
            out.print(f"        [dim]↓ {leg}[/]")
        out.print(f"    {i + 1}. [bold]{node}[/]")
    return out


def contested(c: Call) -> Result:
    """/contested — notes flagged with an unresolved contradiction."""
    out = c.out
    from silica.driver import DRIVER
    from silica.kernel.base.layout import vault_epoch
    from silica.kernel.write.contested import CONTESTED_KEY, CONTRADICTIONS_KEY

    # The scan frontmatter-parses every note, so it runs once per
    # file-state epoch; between vault changes a repeat costs a stat walk.
    epoch = vault_epoch()
    hit = _CONTESTED_SCAN_MEMO.get("scan") if epoch else None
    found: list[tuple[str, list[str]]] | None = (
        hit[1] if hit is not None and hit[0] == epoch else None)
    if found is None:
        found = []
        for ref in DRIVER.list_files(""):
            try:
                props = DRIVER.props_of(ref.path)
            except UNREADABLE:
                continue  # attachments / unreadable frontmatter — not contested
            if props and props.get(CONTESTED_KEY):
                contras = [str(x) for x in (props.get(CONTRADICTIONS_KEY) or [])]
                found.append((ref.path, contras))
        if epoch:
            _CONTESTED_SCAN_MEMO["scan"] = (epoch, found)
    if not found:
        out.print("  No contested notes — no unresolved contradictions.")
        return out
    out.print(f"  {len(found)} contested note(s):")
    for note_path, contras in sorted(found):
        out.print(f"  · [bold]{note_path}[/]")
        for line in contras:
            out.print(f"      conflicts with: {line}")
    out.print(
        "  Ask the agent to resolve one with silica_flag_note(clear=True, "
        "ref=…), passing a `conflicts with` line above verbatim; the losing "
        "claim is filed under `## Superseded` instead of being deleted."
    )
    return out


def plans(c: Call) -> Result:
    """/plans — the plan ledger: what is queued, running, done."""
    out = c.out
    from pathlib import Path

    from rich.markup import escape

    from silica.kernel import plans as plans_mod
    if not CONFIG.vault_path:
        out.print("  No vault configured; /plans needs a .silica vault.")
        return out
    vault = Path(CONFIG.vault_path)
    counts = plans_mod.status_counts(vault)
    if not counts:
        out.print("  No plans found under plans/.")
        return out
    summary = ", ".join(f"[bold]{n}[/] {s}" for s, n in sorted(counts.items()))
    out.print(f"  Plans: {summary}")
    for note_path, data in plans_mod.iter_plan_notes(vault):
        status = str(data.get("status") or "?").strip()
        # escape() keeps the literal [status] bracket from being parsed as
        # rich markup (otherwise [todo] is swallowed as an unknown tag).
        out.print(f"    {escape(f'[{status}] {note_path.stem}')}")
    return out


def agenda(c: Call) -> Result:
    """/agenda [days] — dated commitments coming up."""
    args, out = c.args, c.out
    from rich.markup import escape

    from silica.tools.events import silica_agenda

    arg = " ".join(args).strip().casefold()
    # "week" is the default window spelled out; a date moves the start.
    start = "today" if arg in ("", "today", "week") else arg
    res = silica_agenda(start=start, days=7)
    if res.get("error"):
        out.print(f"  [yellow]{escape(res['error'])}[/]")
        return out
    out.print(escape(res["text"]))
    return out


def impact(c: Call) -> Result:
    """/impact [<git-range>] — which notes a code change touches."""
    args, out = c.args, c.out
    from pathlib import Path
    from silica.kernel.code.codegraph import compute_impact
    vault = CONFIG.vault_path
    if not vault:
        out.print("  No vault configured; /impact needs a vault inside a git repo.")
        return out
    range_spec = args[0] if (len(args) + 1) > 1 else None
    entries = compute_impact(Path(vault), range_spec)
    if entries is None:
        out.print("  No git repo — impact analysis unavailable.")
        return out
    if not entries:
        scope = range_spec or "working tree vs HEAD"
        out.print(f"  No supported source files changed ({scope}).")
        return out
    for e in entries:
        out.print(f"  · [bold]{e.path}[/] — {e.change_level} (fan-in {e.fan_in})")
        for d in e.details[:4]:
            out.print(f"      {d}")
        if e.notes:
            out.print(f"      documents: {', '.join(e.notes)}")
        if e.neighbor_notes:
            out.print(f"      1-hop neighbors documented by: {', '.join(e.neighbor_notes)}")
    return out


def stale(c: Call) -> Result:
    """/stale [--all] — code notes whose source moved on, attested notes whose
    body moved on, gate-written notes edited by hand since.
    /stale --stamp [--write] — derive documents:/code_ref for unbound notes (ADR-0038)."""
    args, out = c.args, c.out
    from pathlib import Path
    vault = CONFIG.vault_path
    if not vault:
        out.print("  No vault configured; /stale needs a .silica vault in a git repo.")
        return out
    if "--stamp" in args:
        folder = next((a for a in args if not a.startswith("--")), "")
        return _stale_stamp(Path(vault), out, folder=folder, write="--write" in args)
    # One all-clear for the whole command, and only when every lane is empty:
    # a per-lane "nothing here" read as a verdict above the next lane's list.
    shown = _stale_code_lane(Path(vault), out, show_all="--all" in args)
    shown += _print_epistemic_lanes(vault, out)
    if not shown:
        out.print("  Nothing stale: every documents: note matches its code_ref, every "
                  "attestation matches its body, no gate-written note edited by hand.")
    return out


def _epistemic_lanes(vault: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(attested notes whose body moved on, gate-written notes edited outside
    Silica) for `/stale` (spec M4). Same walk rules as the OKF census: hidden
    folders skipped."""
    from pathlib import Path

    from silica.kernel.write.notetype import attestation_drift
    from silica.kernel.write.undo_journal import edited_since_write

    root = Path(vault)
    drifted: list[tuple[str, str]] = []
    for f in sorted(root.rglob("*.md")):
        rel = f.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        try:
            at = attestation_drift(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if at:
            drifted.append((rel.as_posix(), at))
    try:
        edited = edited_since_write(vault)
    except Exception as e:  # a corrupt or locked journal must not hide the code lane
        logger.warning("/stale: edited-since-write lane skipped: %s", e)
        edited = []
    return drifted, edited


def _print_epistemic_lanes(vault: str, out: Result) -> int:
    """Prints the two epistemic lanes; returns how many notes they named."""
    drifted, edited = _epistemic_lanes(vault)
    for rel, at in drifted:
        out.print(f"  · [bold]{rel}[/] — attested {at}, body changed since (re-run /verify or /verify --clear)")
    for rel in edited:
        out.print(f"  · [bold]{rel}[/] — edited outside Silica since the gate wrote it")
    return len(drifted) + len(edited)


def _stale_code_lane(vault, out: Result, *, show_all: bool) -> int:
    """Prints the code lane; returns how many notes it named."""
    from pathlib import Path

    from silica.kernel.code import codedocs
    # /stale is the manual refresh valve: drop the cache, recompute, rewrite.
    codedocs.invalidate_snapshot(Path(vault))
    stale = codedocs.snapshot(Path(vault))
    by_note: dict[str, list] = {}
    for sd in stale:
        by_note.setdefault(sd.note_path, []).append(sd)
    shown = 0
    for note_path, docs in sorted(by_note.items()):
        level, details = codedocs.note_verdict(docs)
        if level != codedocs.CHANGE_STRUCTURAL and not show_all:
            continue
        shown += 1
        out.print(f"  · [bold]{note_path}[/] — {level}")
        for sd in docs:
            n = len(sd.intervening)
            out.print(
                f"      documents [bold]{sd.code_path}[/] — {n} new commit(s) "
                f"since {sd.recorded_ref[:8]}"
            )
        for d in details[:6]:
            out.print(f"      {d}")
    if not shown:
        hidden = len(by_note)
        if hidden and not show_all:
            out.print(
                f"  No structural staleness. {hidden} note(s) have cosmetic-only "
                "changes — use [bold]/stale --all[/] to list them."
            )
        return 0
    out.print("  Run [bold]/nucleate <path>[/] to regenerate, or edit and re-badge.")
    return shown


def _stale_stamp(vault, out: Result, folder: str, write: bool) -> Result:
    """List (and with `write`, apply) derived bindings. The write is a yes
    given per run and per folder: detection may start by itself, a stamp may
    not (ADR-0038)."""
    from silica.kernel.code import derive_documents as dd
    props = dd.propose(vault, folder=folder)
    if not props:
        where = f" under {folder}" if folder else ""
        out.print(f"  Nothing to stamp{where}: every note that names source already carries documents:.")
        return out
    for p in props:
        more = f" +{len(p.documents) - 4}" if len(p.documents) > 4 else ""
        out.print(f"  · [bold]{p.note_path}[/] ← {', '.join(p.documents[:4])}{more}"
                  f"  @ {p.code_ref[:8]} ({'+'.join(p.lanes)})")
    if write:
        n = dd.apply(vault, props)
        out.print(f"  Stamped {n} note(s). Run [bold]/stale[/] for the drift since each code_ref.")
    else:
        out.print(f"  {len(props)} proposal(s), nothing written. "
                  "Add [bold]--write[/] to stamp them.")
    return out


def migrate(c: Call) -> Result:
    """/migrate [folder] [--write]: notes Silica wrote in an older shape,
    rewritten through today's writer (ADR-0039). A list by default; the write
    is a yes per run and per folder and lands as one revertible run."""
    args, out = c.args, c.out
    from pathlib import Path

    from silica.kernel.write import migrate
    vault = CONFIG.vault_path
    if not vault:
        out.print("  No vault configured; /migrate needs one.")
        return out
    folder = next((a for a in args if not a.startswith("--")), "")
    todo = written = 0
    # One shape after the other, the listing of the second taken after the
    # first has written: a note the first rewrites can then carry the second.
    # One journal run per shape, so /revert peels them back one at a time.
    for m in migrate.MIGRATIONS:
        plans = migrate.propose(Path(vault), folder=folder, migration=m)
        if not plans:
            continue
        out.print(f"  [bold]{m.name}[/]")
        for p in plans:
            tag = f"  skipped: {p.skip}" if p.skip else ""
            out.print(f"  · [bold]{p.path}[/]  {p.hits} block(s){tag}")
        todo += sum(1 for p in plans if not p.skip)
        if "--write" in args:
            n, run_id = migrate.apply(Path(vault), plans, migration=m)
            written += n
            if run_id:
                out.print(f"  Migrated {n} note(s) (run {run_id[:8]}). [bold]/revert[/] undoes it.")
    if not todo:
        where = f" under {folder}" if folder else ""
        out.print(f"  Nothing to migrate{where}: no note carries an older shape.")
    elif "--write" not in args:
        out.print(f"  {todo} note(s) to rewrite, nothing written. "
                  "Add [bold]--write[/] to migrate them.")
    return out
