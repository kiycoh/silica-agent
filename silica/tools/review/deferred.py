# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Writes the gate refused: one proposal per deferred bundle, the queue /review
listed before it listed anything else.

A yes is /anneal's mechanical retry of that bundle, no model; a no flushes the
bundle, as `/review --flush` did."""
from __future__ import annotations

from silica.kernel.review import Kind, Proposal, proposal_id, register


def _pending(vault: str) -> list[Proposal]:
    from silica.kernel.write.deferred import get_deferred_store

    store = get_deferred_store()
    out = []
    for row in store.list_all():
        bundle = store.get(row["content_hash"]) or {}
        ops = [o for o in bundle.get("rejected_ops") or [] if isinstance(o, dict)]
        reasons = [str(o["rejection_reason"]) for o in ops if o.get("rejection_reason")]
        out.append(Proposal(
            id=proposal_id("deferred", row["content_hash"]), kind="deferred",
            title=f"{len(ops)} write(s) from {row['source_path'] or 'a source'}",
            why=(reasons[0] if reasons else "refused by the write gate"),
            notes=tuple(dict.fromkeys(o["path"] for o in ops if o.get("path"))),
            preview="\n".join(f"{o.get('op', '?')} {o.get('path') or o.get('heading') or ''}"
                              f"{': ' + str(o['rejection_reason']) if o.get('rejection_reason') else ''}"
                              for o in ops[:12]),
            data={"hash": row["content_hash"]}))
    return out


def _apply(vault: str, props: list[Proposal]) -> dict[str, str | None]:
    from silica.router.anneal import retry_bundle

    out: dict[str, str | None] = {}
    for p in props:
        res = retry_bundle(p.data["hash"])
        if res.get("error"):
            out[p.id] = str(res["error"])
        elif res.get("written") or res.get("bundle_cleared"):
            # Some ops written is a yes that landed; the rest stay deferred and
            # list again under the same id, with what still refuses them.
            out[p.id] = None
        else:
            out[p.id] = "nothing became writable: the gate still refuses every write"
    return out


def _dismiss(vault: str, props: list[Proposal]) -> None:
    from silica.kernel.write.deferred import get_deferred_store

    store = get_deferred_store()
    for p in props:
        store.purge(p.data["hash"])


register(Kind("deferred", "Refused writes", _pending, _apply, dismiss=_dismiss))
