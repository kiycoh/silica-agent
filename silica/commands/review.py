# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What looks over the vault: /review, /anneal, /curate, /aliases, /enrich, /dedup, /report, /organize."""
from __future__ import annotations

import json

from silica.command_table import Call, Result
from silica.session.words import int_flag, positional, str_flag


def review(c: Call) -> Result:
    """/review [--flush=<hash>] — the deferred store: write bundles parked
    by a gate, listed or flushed. Not the study queue: that is /learn."""
    args, out = c.args, c.out
    from silica.kernel.write.deferred import get_deferred_store
    store = get_deferred_store()
    flush_hash = str_flag(args, "--flush")
    if flush_hash:
        removed = store.remove(flush_hash)
        if removed:
            out.print(f"  Flushed bundle [bold]{flush_hash[:12]}[/] from review queue.")
        else:
            out.print(f"  [yellow]No bundle with hash {flush_hash[:12]} found.[/]")
        return out
    items = store.list_all()
    if not items:
        out.print("  Review queue is empty.")
    else:
        out.print(f"  [bold]Review queue — {len(items)} bundle(s):[/]")
        for item in items:
            import datetime as _dt
            ts = _dt.datetime.fromtimestamp(item["timestamp"]).strftime("%Y-%m-%d %H:%M")
            out.print(
                f"  · [bold]{item['content_hash'][:12]}[/]  {item['source_path']}  "
                f"({item['rejected_count']} op(s))  {ts}"
            )
        out.print("  Use [bold]/review --flush=<hash>[/] to discard a bundle.")
    return out


def anneal(c: Call) -> Result:
    """/anneal [--steer] [--limit=N] — sweep the deferred queue.

    The counterpart to /review, which only looks. Kept a direct command and not
    an agent turn: the sweep is mechanical (re-validate, write what now passes),
    so routing it through the model would only add a round-trip. --steer is the
    one leg that calls a model: a bounded repair loop per bundle that still fails.
    """
    args, out = c.args, c.out
    from silica.commands import answered
    from silica.router import anneal as annealing

    steer = any(p == "--steer" for p in args)
    limit = int_flag(args, "--limit=", 0)
    scope = f"{limit} bundle(s)" if limit else "every bundle"
    out.print(f"  Annealing {scope}{' with escalation' if steer else ''}…")
    res = answered(annealing.sweep_deferred, steer=steer, limit=limit)
    if "error" in res:
        out.print(f"  [yellow]{res['error']}[/]")
        return out

    if not res.get("bundles"):
        out.print("  Review queue is empty — nothing to anneal.")
        return out

    for row in res.get("results", []):
        mark = "cleared" if row.get("cleared") else f"{row['still_deferred']} left"
        line = f"  · [bold]{row['content_hash']}[/]  {row['written']} written, {mark}"
        if row.get("error"):
            line += f"  [yellow]{row['error']}[/]"
        out.print(line)
    flagged = f", {res['flagged']} landed flagged for review" if res.get("flagged") else ""
    out.print(
        f"  [bold]{res['bundles']}[/] bundle(s): {res['written']} op(s) written, "
        f"{res['still_deferred']} still deferred{flagged}."
    )
    # A sweep that wrote nothing and cleared nothing is the case worth naming:
    # silence there reads as success when the queue is untouched.
    if not res["written"] and res["still_deferred"] and not steer:
        out.print("  [dim]Nothing became writable. Try [bold]/anneal --steer[/].[/]")
    return out


def curate(c: Call) -> Result:
    """/curate [folder] [--apply] — the curation pass over a folder."""
    args, out = c.args, c.out
    from silica.commands import answered
    from silica.tools import curate as curation

    apply = any(p == "--apply" for p in args)
    folder = " ".join(positional(args))
    scope = folder or "(vault)"
    if apply:
        out.print(f"  Curate on [bold]{scope}[/] — applying via the worker seam…")
    else:
        out.print(f"  Curate on [bold]{scope}[/] — dry-run (nothing is written)…")
    res = answered(curation.silica_curate, apply=apply, folder=folder)
    if "error" in res:
        out.print(f"  [yellow]{res['error']}[/]")
        return out

    total = res.get("total", 0)
    counts = res.get("counts", {})
    if total == 0:
        out.print("  Nothing to do — the vault is coherent.")
        return out

    breakdown = ", ".join(f"{v} {k}" for k, v in counts.items())
    if apply:
        # Real outcomes (execution["outcome_counts"], derived from the
        # dispatch batch's per-item status + the mechanical autolink's
        # actual links-added count) — NOT the planned counts above, which
        # would report "Applied N" even when e.g. every dedup came back
        # a distinct verdict and nothing was actually merged.
        outcome = res.get("execution", {}).get("outcome_counts", {})
        dispatched = sum(outcome.values())
        outcome_breakdown = ", ".join(f"{v} {k}" for k, v in outcome.items()) or "no changes"
        out.print(f"  Applied — dispatched [bold]{dispatched}[/] → outcomes: {outcome_breakdown}")
    else:
        out.print(f"  Plan — [bold]{total}[/] item(s): {breakdown}")
        for it in res.get("items", []):
            pair = f" ↔ {it['partner']}" if it.get("partner") else ""
            out.print(f"  · [bold]{it['kind']}[/]  {it['target']}{pair}")
        out.print('  Run [bold]/curate --apply[/] to execute, or ask e.g. "apply only dedup".')
    return out


def aliases(c: Call) -> Result:
    """/aliases [note] — alias coverage, and what is missing."""
    args, out = c.args, c.out
    from silica.commands import answered
    from silica.tools import aliases as consolidation

    apply = any(p == "--apply" for p in args)
    folder = " ".join(positional(args))
    scope = folder or "(vault)"
    mode = "applying" if apply else "dry-run (nothing is written)"
    out.print(f"  Alias consolidation on [bold]{scope}[/] — {mode}…")
    res = answered(consolidation.silica_aliases, apply=apply, folder=folder)
    if "error" in res:
        out.print(f"  [yellow]{res['error']}[/]")
        return out
    groups = res.get("groups", {})
    if not groups:
        out.print("  No alias groups survived the gate.")
        return out
    for canonical, variants in sorted(groups.items()):
        out.print(f"  · [bold]{canonical}[/] ← {', '.join(variants)}")
    dropped = res.get("dropped", 0)
    if dropped:
        out.print(f"  ({dropped} proposed variant(s) dropped by the ambiguity gate)")
    if apply:
        written = res.get("written", {})
        n = sum(written.values())
        out.print(f"  Applied — [bold]{n}[/] alias(es) written into {len(written)} note(s).")
        for it in res.get("skipped", []):
            out.print(f"  [yellow]skipped {it['note']}: {it['reason']}[/]")
    else:
        out.print("  Run [bold]/aliases --apply[/] to write them into frontmatter.")
    return out


def _chunk_by_json_size(items: list, max_bytes: int = 4000) -> list[list]:
    """Greedily pack items into chunks whose JSON size stays under max_bytes.
    Each chunk becomes one ledger task / one batch-tool call."""
    chunks: list[list] = []
    cur: list = []
    size = 0
    for it in items:
        s = len(json.dumps(it))
        if size + s > max_bytes and cur:
            chunks.append(cur)
            cur, size = [it], s
        else:
            cur.append(it)
            size += s
    if cur:
        chunks.append(cur)
    return chunks


def _seed_batch_ledger(out: Result, cap: str, payloads: list[dict], *, kind: str, label: str) -> Result:
    """Create a remediation Run whose tasks each invoke `cap` with one payload and
    hand back what the agent is asked, with the event that opens the REPL's batch
    panel on it. Shared by /enrich and /dedup — the async, resumable,
    progress-tracked path."""
    from silica.kernel.progress import PlanStep, Run
    from silica.obs.events import BatchRunStartEvent

    run = Run.new(
        mode="analyst",
        user_request=f"{kind} {label}",
        checkpoints=[PlanStep(id="remediate", kind="gate", objective=cap)],
        inputs={"scope": label},
    )
    for i, payload in enumerate(payloads):
        run.add_payload_task(cap, payload, f"Batch {i + 1} of {len(payloads)}")
    run.save()
    out.batch = BatchRunStartEvent(run_id=run.run_id, kind=kind, label=label, total=len(payloads))
    from silica.obs import narration as _narr_mod
    # Left running on purpose: a seeded ledger IS pending until executed, and
    # a projection showing it running is the truth, not a leak.
    _narr_mod.NARRATOR.span_open(
        "run", f"run-{run.run_id}", f"{kind} {label}: {len(payloads)} batch(es)",
        {"run_id": run.run_id, "kind": kind, "total": len(payloads)})
    return out.to_model(
        f"A ledger for /{kind} has been created with {len(payloads)} chunk(s) "
        f"(run_id={run.run_id}). Use `silica_ledger_next` with this run_id to execute them."
    )


def enrich(c: Call) -> Result:
    """/enrich [folder] — seed a batch ledger over a folder's notes."""
    args, out = c.args, c.out
    from silica.driver import DRIVER
    from silica.kernel.base.layout import in_folder

    folder = next(iter(positional(args)), "")

    # list_files(folder) pre-filters loosely (startswith); in_folder tightens
    # it so /enrich Foo never leaks into a sibling FooBar/ folder.
    paths = [r.path for r in DRIVER.list_files(folder=folder) if in_folder(r.path, folder)]
    if not paths:
        return out.refused(f"no files found in '{folder}'.")

    payloads = [{"note_paths": chunk} for chunk in _chunk_by_json_size(paths)]
    return _seed_batch_ledger(out, "silica_enrich_batch", payloads, kind="enrich", label=folder or "vault")


def dedup(c: Call) -> Result:
    """/dedup [folder] — seed a batch ledger over the near-duplicate pairs found."""
    args, out = c.args, c.out
    from silica.kernel.recall import duplicates

    folder = " ".join(positional(args))

    pairs, err = duplicates.scan_pairs(folder)
    if err:
        out.print(f"  [yellow]{err}[/]")
        return out
    if not pairs:
        out.print(f"  No near-duplicate pairs in [bold]{folder or '(vault)'}[/].")
        return out

    payloads = [{"pairs": chunk} for chunk in _chunk_by_json_size(pairs)]
    return _seed_batch_ledger(out, "silica_dedup_pairs", payloads, kind="dedup", label=folder or "vault")


def report(c: Call) -> Result:
    """/report [folder] [--top-k=N] [--embeddings] [--cooccurrence] — structural audit."""
    args, out = c.args, c.out
    folder = ""
    top_k = int_flag(args, "--top-k=", 10)
    with_embeddings = False
    # Off by default like --embeddings: the co-occurrence delta runs a
    # per-note expanded ranking, the report's other expensive pass. Without
    # it stale links and missing hubs are never computed at all, so the
    # escalate rules that read them can never fire.
    with_cooccurrence = False

    for arg in args:
        if arg.startswith("--folder="):
            folder = arg[len("--folder="):]
        elif arg in ("--embeddings", "--with-embeddings"):
            with_embeddings = True
        elif arg in ("--cooccurrence", "--with-cooccurrence"):
            with_cooccurrence = True
        elif not arg.startswith("-"):
            folder = arg

    scope_desc = f"scoped to `{folder}`" if folder else "on the whole vault"
    embed_note = " Also propose missing links via the embedding index." if with_embeddings else ""
    if with_cooccurrence:
        embed_note += (" Also read the co-occurrence delta: autolink candidates, stale links"
                       " and missing hubs.")

    return out.to_model(f"Run a structural vault audit {scope_desc}.{embed_note}\n"
        f"Call `silica_vault_report` with "
        f"folder={json.dumps(folder)}, top_k={top_k}, "
        f"with_embeddings={'true' if with_embeddings else 'false'}, "
        f"with_cooccurrence={'true' if with_cooccurrence else 'false'}.\n"
        f"Then STOP. Write a short, human-readable brief in chat from the returned `digest` "
        f"(totals, top hubs, and how many fixes are available: auto / propose / issues), and "
        f"point the user to the GRAPH_REPORT.md that was written.\n"
        f"Do NOT run the steering loop, do NOT call `silica_ledger_next`, and do NOT apply any "
        f"autolinks, corrections, renames, or deletions. Instead, ask the user whether they want "
        f"to apply the changes. Only if they explicitly say yes, resume the run (`run_id`) and "
        f"follow the steering loop.")


def organize(c: Call) -> Result:
    """/organize [intent] [--scope=DIR] [--file=TAXONOMY] [--apply] [--merge]
    [--move-uncategorized] — agent-directed taxonomy build and move plan."""
    args, out = c.args, c.out
    intent_parts = positional(args)
    scope = str_flag(args, "--scope")
    taxonomy_file = str_flag(args, "--file")
    apply_now = "--apply" in args
    merge = "--merge" in args
    move_uncat = "--move-uncategorized" in args

    # Both organizer tools filter with `ref.path.startswith(scope)` over
    # vault-relative paths, so an absolute --scope matches zero notes and the
    # whole run reports success on an empty plan. Normalize here, where the
    # user types the path, and refuse a scope outside the vault out loud.
    if scope:
        from silica.kernel.base.layout import to_vault_relative
        try:
            scope = to_vault_relative(scope, ensure_md=False)
        except ValueError as exc:
            return out.refused(f"/organize --scope is not usable: {exc}")

    # Re-join intent (handles both quoted and unquoted multi-word)
    intent = " ".join(intent_parts).strip('"\'')
    run_extra = ", move_uncategorized=true" if move_uncat else ""

    if taxonomy_file:
        dry = "false" if apply_now else "true"
        scope_str = f", scope={json.dumps(scope)}" if scope else ""
        msg = (
            f"Run the vault organizer using the existing taxonomy file {json.dumps(taxonomy_file)}.\n"
            f"Call `silica_run_organizer` with taxonomy_path={json.dumps(taxonomy_file)}{scope_str}, "
            f"dry_run={dry}{run_extra}.\n"
        )
        if not apply_now:
            msg += (
                "Show the move plan to the user and ask for confirmation. "
                "If confirmed, call `silica_run_organizer` again with dry_run=false."
            )
    elif intent:
        scope_str = f", scope={json.dumps(scope)}" if scope else ""
        merge_str = ", merge=true" if merge else ""
        dry_note = (
            f"Then call `silica_run_organizer` with dry_run=true{run_extra} to preview the moves. "
            "Show the plan to the user and ask for confirmation before executing."
        ) if not apply_now else (
            f"Then call `silica_run_organizer` with dry_run=false{run_extra} to execute the moves."
        )
        msg = (
            f"Organize the vault based on the user's intent: {json.dumps(intent)}.\n"
            f"Step 1: Call `silica_generate_taxonomy` with user_intent={json.dumps(intent)}{scope_str}{merge_str}.\n"
            f"Step 2: Show the generated taxonomy to the user and ask if it looks correct.\n"
            f"Step 3: {dry_note}"
        )
    else:
        msg = (
            "Help me organize my vault. "
            "Ask me to describe how I want to group my notes, "
            "then call `silica_generate_taxonomy` with my answer, "
            "show me the taxonomy, and run `silica_run_organizer` with dry_run=true to preview."
        )
    return out.to_model(msg)
