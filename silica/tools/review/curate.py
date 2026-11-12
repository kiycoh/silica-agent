# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/curate's plan, waiting: links to add, orphans to connect, duplicates to merge.

The report behind the plan is minutes on a large vault, so the plan is listed as
the last dry run left it and never recomputed by a listing. A link is a
mechanical write, previewed before it is offered: a pair whose notes never name
each other has nothing to link and is not offered. An orphan or a duplicate is
the leashed worker's to judge, through /curate --apply's own path, so its yes
spends the worker model."""
from __future__ import annotations

from silica.kernel.report.curator import CurationItem, CurationPlan
from silica.kernel.review import Kind, Proposal, offer, offered, proposal_id, register
from silica.kernel.text.note_parts import clean_name


def _link(src: str, dst: str, titles: list[str]) -> tuple[str, str] | None:
    """`src`'s text with its first mention of `dst` linked, or None when
    `src` does not name it or is not Silica's to write."""
    from silica.driver import DRIVER
    from silica.kernel.base.layout import is_inbox_path
    from silica.kernel.base.manifest import active_write_dir, within
    from silica.kernel.link.autolink import autolink

    root = active_write_dir()
    # The guards silica_backlink keeps (kernel.link.autolink.backlink_pass):
    # the inbox is staging, and a vault that reads a source tree writes only
    # under its write dir.
    if is_inbox_path(src) or (root and not within(src, root)):
        return None
    try:
        body = DRIVER.read_note(src).content or ""
    except Exception:
        return None  # gone or unreadable since the report: nothing to link in it
    after, added = autolink(body, titles, candidates=[clean_name(dst)], self_title=clean_name(src))
    return (body, after) if added else None


def _changed(before: str, after: str) -> str:
    return "\n".join(b.strip()[:200] for a, b in zip(before.splitlines(), after.splitlines()) if a != b)


def offer_plan(vault: str, plan: CurationPlan) -> None:
    """The dry run's plan as proposals of the three kinds, in place of the last."""
    from silica.driver import DRIVER
    from silica.kernel.link.autolink import build_title_index

    if not vault:
        return
    titles = build_title_index(DRIVER.list_files()) if plan.by_kind("autolink") else []
    links = []
    for it in plan.by_kind("autolink"):
        for src, dst in ((it.target, it.partner), (it.partner, it.target)):
            if (pair := _link(src, dst, titles)) is not None:
                links.append(Proposal(
                    id=proposal_id("autolink", src, dst), kind="autolink",
                    title=f"Link {clean_name(dst)} from {clean_name(src)}", why=it.reason,
                    notes=(src, dst), preview=_changed(*pair), data={"src": src, "dst": dst}))
                break
    offer(vault, "autolink", links)
    offer(vault, "orphan", [Proposal(
        id=proposal_id("orphan", it.target), kind="orphan",
        title=f"Connect {clean_name(it.target)}", why="nothing links to it",
        notes=(it.target,), data={"target": it.target}) for it in plan.by_kind("orphan")])
    offer(vault, "duplicate", [Proposal(
        id=proposal_id("duplicate", *sorted((it.target, it.partner))), kind="duplicate",
        title=f"{clean_name(it.target)} and {clean_name(it.partner)}", why=it.reason,
        notes=(it.target, it.partner),
        data={"target": it.target, "partner": it.partner, "score": it.score, "reason": it.reason})
        for it in plan.by_kind("dedup")])


def _apply_links(vault: str, props: list[Proposal]) -> dict[str, str | None]:
    from silica.driver import DRIVER
    from silica.kernel.link.autolink import autolink, build_title_index
    from silica.kernel.write import gate

    titles = build_title_index(DRIVER.list_files())
    out: dict[str, str | None] = {}
    for p in props:
        src, dst = p.data["src"], p.data["dst"]
        if _link(src, dst, titles) is None:
            out[p.id] = f"{clean_name(src)} no longer names {clean_name(dst)} unlinked"
            continue
        def link(body: str, src: str = src, dst: str = dst) -> str:
            return autolink(body, titles, candidates=[clean_name(dst)], self_title=clean_name(src))[0]

        gate.journaled_write(src, link, source="review", run=gate.current_run() or "")
        out[p.id] = None
    return out


# What the worker's verdict means to the person who said yes.
_VERDICT = {"no_merge": "judged distinct: nothing merged",
            "no_link": "the worker found no link worth writing",
            "no_change": "the worker changed nothing"}


def _apply_judged(vault: str, props: list[Proposal]) -> dict[str, str | None]:
    from silica.tools.curate import apply_curation_plan

    items = [CurationItem(kind="orphan", target=p.data["target"]) if p.kind == "orphan"
             else CurationItem(kind="dedup", target=p.data["target"], partner=p.data["partner"],
                               score=p.data["score"], reason=p.data["reason"])
             for p in props]
    res = apply_curation_plan(CurationPlan(items=items), vault_path=vault)
    out: dict[str, str | None] = {p.id: "the worker never reached it" for p in props}
    # A duplicate family is judged as one item under its merge target, which
    # may be either note of a pair: a result belongs to every pair that has it.
    for r in (res.get("batch") or {}).get("results") or []:
        status = str(r.get("status") or "")
        for p in props:
            if r.get("target") in p.notes:
                out[p.id] = None if status == "committed" else _VERDICT.get(status, f"not written ({status})")
    return out


register(Kind("autolink", "Links to add", lambda vault: offered(vault, "autolink"), _apply_links))
register(Kind("orphan", "Orphans to connect", lambda vault: offered(vault, "orphan"), _apply_judged,
              spends_model=True))
register(Kind("duplicate", "Possible duplicates", lambda vault: offered(vault, "duplicate"), _apply_judged,
              spends_model=True))
