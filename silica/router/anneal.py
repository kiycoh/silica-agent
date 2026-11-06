# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Boundary annealing: what the gate deferred, retried once the vault has moved on.

Here and not under tools/ because the FSM runs it in its own `finally`, and the
router does not import the tool implementations. silica.tools.pipeline holds
the three tools that expose it. Every landing goes through `_land_recovered`,
whichever way in.
"""
from __future__ import annotations

import logging
from typing import Any

from silica.driver import DRIVER
from silica.kernel.write.ops import BulkResult, Op, OpType

logger = logging.getLogger(__name__)


def _link_recovered_writes(
    ops: list, target_dir: str, hub: str | None, source_path: str = ""
) -> None:
    """Give anneal-recovered writes the graph edges the FSM's AUTOLINK and
    HUB_UPDATE states would have added — the deferred retry path bypasses both,
    so recovered notes otherwise land as orphans with zero edges and no MOC
    membership (audit finding 2).

    Best-effort, mirroring the FSM's non-fatal stance: both passes only ADD
    links; neither can break a valid note.
    """
    import os
    from silica.kernel.link.autolink import build_title_index
    from silica.kernel.write.moc import hub_desc, merge_moc_section, moc_heading, moc_line

    hub_name = (hub or "").strip("[]")
    hub_l = hub_name.lower()
    written = [
        op for op in ops
        if op.op in (OpType.write, OpType.overwrite) and op.touched_ref()
        and os.path.splitext(os.path.basename(op.touched_ref()))[0].lower() != hub_l
    ]
    if not written:
        return

    # Inline autolink (what the FSM's AUTOLINK state does per chunk).
    try:
        title_index = build_title_index(DRIVER.list_files(target_dir or ""))
    except Exception as e:
        logger.warning("anneal: title-index build failed, recovered notes stay unlinked: %s", e)
        return
    for op in written:
        try:
            DRIVER.autolink_note(
                op.touched_ref(), candidates=title_index, title_index=title_index
            )
        except Exception as e:
            logger.debug("anneal: autolink skipped '%s' (non-fatal): %s", op.touched_ref(), e)

    # Hub-MOC membership (what the FSM's HUB_UPDATE state does per chunk):
    # same heading/merge helpers, so recovered bullets coalesce with the
    # section an in-flight chunk of the same source already created.
    if not hub_name:
        return
    from silica.kernel.write.moc import moc_target
    hub_path = moc_target(hub_name, target_dir)
    if not hub_path:  # staging note, never a MOC target
        return
    try:
        hub_note = DRIVER.read_note(hub_path)
    except Exception as e:
        logger.warning("anneal: hub '%s' not readable, MOC membership skipped: %s", hub_path, e)
        return
    try:
        entries = [(op.touched_ref(), hub_desc(op.snippet or op.content or "")) for op in written]
        source_name = os.path.splitext(os.path.basename(source_path))[0] or "deferred"
        sample = hub_note.content + " ".join(d for _, d in entries[:3])
        heading = moc_heading(source_name, sample)
        note_lines = [moc_line(p, d) for p, d in entries]
        DRIVER.overwrite(hub_path, merge_moc_section(hub_note.content, heading, note_lines))
        logger.info(
            "anneal: %d recovered note(s) autolinked and added to hub '%s' MOC",
            len(written), hub_name,
        )
    except Exception as e:
        logger.warning("anneal: hub MOC update failed (non-fatal): %s", e)


def _land_recovered(validated: list[Op], content_hash: str, bundle: dict) -> BulkResult:
    """The whole landing of ops recovered outside the FSM: snapshot, write,
    rollback on any failure, then the stages the FSM would have run. Both ways
    in share it because each skipped a stage in turn: 5 of 94 notes sat on disk
    with no inverse and no record on a 3-paper library gate (2026-08-18), so
    `/revert` walked past them and `check_renucleate` reported the source as
    never nucleated, and the old steer commit path skipped the edges as well.
    The boundary anneal runs in the FSM's `finally`, after CLEANUP.

    Edges before the record: the journal hashes what these two passes leave.
    """
    from silica.kernel.write import gate

    landing = gate.land_batch(validated, lint=False, partial_ok=False)
    if landing.failure is None:
        _link_recovered_writes(validated, bundle.get("target_dir", ""), bundle.get("hub"),
                               bundle.get("source_path", ""))
        gate.record_recovered(landing.txn, validated, content_hash, bundle)
    return landing.bulk


def retry_bundle(content_hash: str) -> dict[str, Any]:
    """silica_deferred_retry: one bundle through the mechanical retry."""
    from silica.kernel.write.deferred import get_deferred_store
    from silica.kernel.write.validate import validate_operations
    from silica.kernel.write.ops_io import parse_ops

    store = get_deferred_store()
    bundle = store.get(content_hash)
    if not bundle:
        return {"error": f"No deferred bundle found for hash {content_hash[:8]}…"}

    rejected_raw = bundle.get("rejected_ops", [])
    if not rejected_raw:
        # Nothing to retry is nothing to keep. The store drops such a bundle
        # when it opens (DeferredStore._sweep), but one that appears after
        # that sweep would otherwise sit in the queue: the validator's verdict
        # over zero ops read as "still rejected" and the file was never removed.
        store.remove(content_hash)
        return {"success": True, "written": 0, "still_deferred": 0,
                "bundle_cleared": True, "flagged_ops": [],
                "message": "Bundle carried no ops; removed"}
    target_dir = bundle.get("target_dir", "")
    hub = bundle.get("hub")

    try:
        ops = parse_ops(rejected_raw)
    except Exception as e:
        return {"error": f"Failed to parse deferred ops: {e}"}

    # Re-validate against the bundle's ORIGINAL payloads (persisted at defer
    # time) so grounding/heading/collision checks run with the same evidence
    # that rejected the ops — an empty list here used to admit them on strictly
    # weaker validation (audit finding 2). Old bundles without payloads keep
    # the previous behavior.
    payloads = bundle.get("payloads") or []
    validated, still_rejected = validate_operations(ops, payloads, target_dir, hub=hub)

    if not validated:
        return {
            "success": False,
            "message": "All deferred ops still rejected by the validator",
            "rejected": [
                {"path": r.op.path, "heading": r.op.heading, "reason": r.reason}
                for r in still_rejected
            ],
            "still_deferred": len(still_rejected),
        }

    try:
        result = _land_recovered(validated, content_hash, bundle)
    except Exception as e:
        return {"error": f"Deferred retry failed: {e}"}
    if not result.ok:
        return {"error": f"Deferred retry write failed: {[f.model_dump() for f in result.failed]}"}

    # Update or clear the deferred store
    if still_rejected:
        store.put(
            content_hash=content_hash,
            source_path=bundle.get("source_path", ""),
            target_dir=target_dir,
            hub=hub,
            rejected_ops=[r.op.model_dump() for r in still_rejected],
            rejection_reasons={
                (r.op.path or r.op.heading or "?"): r.reason for r in still_rejected
            },
            phase="RETRY",
            payloads=payloads,  # keep grounding evidence for the next retry
        )
    else:
        store.remove(content_hash)

    return {
        "success": True,
        "written": len(validated),
        "still_deferred": len(still_rejected),
        "bundle_cleared": len(still_rejected) == 0,
        # Soft-gate landings: this path has no judge and no run ledger, so the
        # caller that has them (the boundary anneal) settles these itself.
        "flagged_ops": [o.model_dump() for o in validated if o.review],
    }


def repair_bundle(content_hash: str, ops: list[dict]) -> dict[str, Any]:
    """submit_repaired_ops: the model's corrected ops through the same landing."""
    from silica.kernel.write.deferred import get_deferred_store
    from silica.kernel.text.sanitize import normalize_ops
    from silica.kernel.write.ops_io import parse_ops
    from silica.kernel.write.validate import validate_operations

    store = get_deferred_store()
    bundle = store.get(content_hash)
    if not bundle:
        return {"error": f"No deferred bundle for hash {content_hash[:8]}…"}
    if not ops:
        return {"error": "ops is empty — submit the corrected ops"}
    target_dir = bundle.get("target_dir", "")
    hub = bundle.get("hub")
    payloads = bundle.get("payloads") or []
    # Same post-processing the main distill path gets (sanitize_output →
    # normalize_ops): tool-arg transport guarantees well-formed JSON, not
    # well-escaped content — over-escaped LaTeX (`\\{a_c\\}`, `\\dots`) still
    # needs the per-site collapse, anchored on the bundle's own excerpts.
    anchor = "\n".join(
        c.get("inbox_excerpt") or ""
        for p in payloads if isinstance(p, dict)
        for b in p.get("batches", [])
        for c in b.get("concepts", [])
    ) or None
    fixed = parse_ops(normalize_ops(ops, verbatim_source=anchor))
    fixed = [op for op in fixed if op.op != OpType.skip]
    if not fixed:
        return {"error": "no usable ops after parsing — check the op schema"}
    # The bundle's ORIGINAL payloads, same as silica_deferred_retry (audit
    # finding 2): an empty list re-admits the fix on strictly weaker validation
    # — measured live, a hallucinated op with zero grounded facts sailed
    # through and landed in the vault.
    validated, still = validate_operations(fixed, payloads, target_dir, hub=hub)
    rejected = [{"path": r.op.path, "heading": r.op.heading, "reason": r.reason}
                for r in still]
    if not validated:
        return {"written": [], "rejected": rejected,
                "remaining": len([o for o in bundle.get("rejected_ops", [])
                                  if isinstance(o, dict)])}
    if not _land_recovered(validated, content_hash, bundle).ok:
        return {"error": "write failed; nothing was committed this call"}
    # written ops are dropped from the bundle by heading match only —
    # an op the model renamed stays parked and re-anneals (writes are idempotent
    # via block_present), which is the safe direction.
    renamed = [op.heading for op in validated
               if not store.remove_op(content_hash, op.heading)]
    result: dict[str, Any] = {
        "written": [op.heading for op in validated],
        "rejected": rejected,
        "remaining": len([o for o in
                          (store.get(content_hash) or {}).get("rejected_ops", [])
                          if isinstance(o, dict)]),
    }
    if renamed:
        # Named to the model, or it reads the unchanged `remaining` as a
        # failed write and resubmits the same op until the cap.
        result["renamed"] = renamed
    return result


def sweep_deferred(steer: bool = False, limit: int = 0) -> dict[str, Any]:
    """silica_anneal: every deferred bundle, then the bounded repair loop with `steer`."""
    from silica.kernel.write.deferred import get_deferred_store

    store = get_deferred_store()
    bundles = store.list_all()
    if limit:
        bundles = bundles[:limit]
    swept: list[dict[str, Any]] = []
    for b in bundles:
        h = b["content_hash"]
        res = retry_bundle(h)
        row: dict[str, Any] = {
            "content_hash": h[:8],
            "written": res.get("written", 0),
            "still_deferred": res.get("still_deferred", 0),
            "cleared": res.get("bundle_cleared", False),
            "flagged_ops": res.get("flagged_ops") or [],
        }
        if res.get("error"):
            row["error"] = res["error"]
        if steer and row["still_deferred"]:
            row["steer"] = _steer_bundle(h)
        swept.append(row)
    return {
        "bundles": len(swept),
        "written": sum(r["written"] for r in swept)
        + sum(r.get("steer", {}).get("written", 0) for r in swept),
        "still_deferred": sum(r["still_deferred"] for r in swept)
        - sum(r.get("steer", {}).get("written", 0) for r in swept),
        "flagged": sum(len(r["flagged_ops"]) for r in swept),
        "flagged_ops": [o for r in swept for o in r["flagged_ops"]],
        "results": swept,
    }


def _steer_bundle(content_hash: str) -> dict[str, Any]:
    """A bounded repair loop over a bundle's still-failing ops.

    Spec: docs/specs/anneal-steer-loop.md; ADR-0031 carries the decision
    against the one-shot and the rejected alternatives (forced single tool
    call; generalizing the seam to every FSM state). The one-shot asked the
    escalation model for a perfect JSON array in free text and threw the
    validator's verdicts away: measured 2026-08-23, 1 of 24 ops recovered,
    with the prose plan eating the parse and a stray ===SILICA-BODY=== marker
    landing in a body as the next run's rejection. Now the model corrects ops
    by calling submit_repaired_ops, whose result carries the per-op verdicts
    (PDDL-INSTRUCT: the verdict is the feedback), and iterates inside the same
    turn. Ops leave the bundle only by being written, so written is the op
    count before minus after, and partial progress survives a dead loop.
    """
    import os

    import orjson as _orjson

    from silica.agent.constraints import AgentConstraints
    from silica.agent.loop import run_agent
    from silica.config import CONFIG
    from silica.kernel.write.deferred import get_deferred_store

    store = get_deferred_store()
    bundle = store.get(content_hash)
    if not bundle:
        return {"status": "gone"}
    ops = [o for o in bundle.get("rejected_ops", []) if isinstance(o, dict)]
    if not ops:
        return {"status": "empty"}
    target_dir = bundle.get("target_dir", "")
    hub = bundle.get("hub")
    payloads = bundle.get("payloads") or []
    # The heading gate downstream only admits headings named in the payloads,
    # but the model never saw that list — it re-conceptualized freely and lost
    # the whole retry to mechanical rejections (17 of 55 deferrals on the
    # 2026-08-05 run were RETRY-phase 'Heading not present in payload concepts').
    allowed_headings = sorted({
        c.get("name")
        for p in payloads if isinstance(p, dict)
        for b in p.get("batches", [])
        for c in b.get("concepts", [])
        if c.get("name")
    })
    file_reasons = bundle.get("rejection_reasons", {})
    feedback = [
        {
            "op": o,
            "rejected_because": o.get("rejection_reason")
            or file_reasons.get(o.get("path") or o.get("heading") or "?", "unknown"),
        }
        for o in ops
    ]
    hub_line = f"\nHUB: {hub}" if hub else ""
    headings_line = (
        "\nALLOWED HEADINGS (every op's \"heading\" MUST be one of these, "
        "verbatim — never invent a new one):\n"
        + "\n".join(f"- {h}" for h in allowed_headings) + "\n"
    ) if allowed_headings else ""
    prompt = (
        "You are repairing note-write operations that a validation gate rejected.\n"
        f"TARGET_DIR: {target_dir}{hub_line}{headings_line}\n"
        "Each op below is echoed with the exact reason it was rejected. Fix ONLY\n"
        "what the reason requires — keep the content otherwise identical.\n"
        "Submit corrections by calling submit_repaired_ops with content_hash\n"
        f'"{content_hash}" and the corrected ops in the same op schema. Bodies\n'
        "are plain strings: real line breaks, single backslashes, never\n"
        "double-escaped.\n"
        "The result lists per-op verdicts: ops under \"written\" are done and\n"
        "must never be resubmitted; correct the ops under \"rejected\" and\n"
        "resubmit only those. When nothing is left, or a remaining op is\n"
        "unfixable, reply with a one-line summary instead of a call.\n\nREJECTED OPS:\n"
        + _orjson.dumps(feedback, option=_orjson.OPT_INDENT_2).decode()
    )
    before = len(ops)
    error = None
    summary = ""
    try:
        summary = run_agent(
            [{"role": "user", "content": prompt}],
            # Reproduces get_provider(role="escalation")'s fallback chain: the
            # field is already ensure_prefix-ed to the litellm string call_llm
            # resolves endpoints from, unset falls back to the router model.
            model=CONFIG.distill_escalation_model or CONFIG.model,
            constraints=AgentConstraints(
                tools=("submit_repaired_ops",),
                # One iteration = one LLM call, and a run that hits the cap
                # pays one extra tool-less landing call, so 4 caps the spend
                # at 5 calls per bundle (vs 1 for the old one-shot) — and only
                # for bundles that already failed the mechanical pass.
                max_iterations=int(os.getenv("ANNEAL_STEER_ITERATIONS", "4")),
            ),
        )
    except Exception as e:
        # Endpoint failures (broken tool-calling included) land here; whatever
        # earlier iterations already wrote is on disk and counted below, the
        # rest of the bundle stays parked and /anneal --steer is rerunnable.
        error = str(e)[:200]
    # Same isinstance filter as `before`: a corrupt non-dict entry must not
    # skew the delta (a negative `written` would even read as committed,
    # since any non-zero int is truthy).
    after = len([
        o for o in (store.get(content_hash) or {}).get("rejected_ops", [])
        if isinstance(o, dict)
    ])
    written = before - after
    row: dict[str, Any] = {
        "status": "error" if error else ("committed" if written else "no_fix"),
        "written": written,
        "still_rejected": after,
    }
    # The model's closing report ("op X is unfixable because...") is the only
    # trace of WHY ops stayed deferred; "(silica: ...)" sentinels mean there
    # was no real summary (cancelled, or a cap landing that said nothing).
    if summary and not summary.startswith("(silica:"):
        row["summary"] = summary[:300]
    if error:
        row["error"] = error
    return row
