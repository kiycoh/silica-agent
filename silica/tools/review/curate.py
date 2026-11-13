# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/curate's plan, waiting: links to add, orphans to connect, duplicates to merge.

The report behind the plan is minutes on a large vault, so the plan is listed as
the last dry run left it and never recomputed by a listing. A link is a
mechanical write, previewed before it is offered: a pair whose notes never name
each other has nothing to link and is not offered. An orphan or a duplicate is
the leashed worker's to land, through /curate --apply's own path, so its yes
spends the worker model.

A duplicate is offered only once the worker has judged it one (ADR-0175): the
dry run asks for the verdict alone on each candidate pair and remembers every
verdict against the text of both notes, so the next run asks again only about a
pair whose note changed."""
from __future__ import annotations

import hashlib
import logging
import re
import time
from contextvars import ContextVar

from silica.kernel.report.curator import CurationItem, CurationPlan
from silica.kernel.review import (Kind, Proposal, dismissed, offer, offered, proposal_id, register, remember,
                                  verdicts)
from silica.kernel.text.note_parts import clean_name

logger = logging.getLogger(__name__)

# Verdicts one /curate asks for. A command in the chat cannot be stopped between
# calls, and the demo vault's high band held 6 pairs on 2026-11-13, so 20 covers
# a vault several times its size in one run; the rest wait for the next.
JUDGE_CAP = 20

# Set by the /curate command: a verdict is a model call, spent only when the
# person typed /curate (owner, 2026-11-13). The chat model calling silica_curate
# reads the verdicts already given and leaves new pairs waiting.
PERSON_ASKED: ContextVar[bool] = ContextVar("curate_person_asked", default=False)


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


def _md(path: str) -> str:
    """A vault path as the drawer and the writer take it. The report mixes two
    keyspaces: the graph's carries `.md`, the embedding store's does not, and a
    path without it was written as a file without it."""
    return path if path.endswith(".md") else path + ".md"


def offer_plan(vault: str, plan: CurationPlan, judge: bool = True) -> dict[str, int]:
    """The dry run's plan as proposals of the three kinds, in place of the last,
    and how the worker's verdicts on its duplicate candidates came out."""
    from silica.driver import DRIVER
    from silica.kernel.link.autolink import build_title_index

    if not vault:
        return {}
    titles = build_title_index(DRIVER.list_files()) if plan.by_kind("autolink") else []
    links = []
    for it in plan.by_kind("autolink"):
        for src, dst in ((_md(it.target), _md(it.partner)), (_md(it.partner), _md(it.target))):
            if (pair := _link(src, dst, titles)) is not None:
                links.append(Proposal(
                    id=proposal_id("autolink", src, dst), kind="autolink",
                    title=f"Link {clean_name(dst)} from {clean_name(src)}", why=it.reason,
                    notes=(src, dst), preview=_changed(*pair), data={"src": src, "dst": dst}))
                break
    offer(vault, "autolink", links)
    offer(vault, "orphan", [Proposal(
        id=proposal_id("orphan", _md(it.target)), kind="orphan",
        title=f"Connect {clean_name(it.target)}", why="nothing links to it",
        notes=(_md(it.target),), data={"target": it.target}) for it in plan.by_kind("orphan")])
    duplicates, judged = _judged_duplicates(vault, plan.by_kind("dedup"), judge)
    offer(vault, "duplicate", duplicates)
    return judged


def pair_hash(a: str, b: str) -> str | None:
    """Both notes' text as one digest, None when either cannot be read: a
    verdict holds for the notes as they were when the worker read them."""
    from silica.driver import DRIVER

    try:
        texts = [DRIVER.read_note(p).content or "" for p in sorted((a, b))]
    except Exception:
        return None  # gone or unreadable since the report: nothing to judge
    return hashlib.sha1("\0".join(texts).encode()).hexdigest()[:16]


def band_of(vault: str, a: str, b: str) -> str:
    """What the queue makes of a duplicate candidate, as Examine's Band column
    says it: "merged" once a yes landed, "dismissed" after a no, else the
    worker's verdict for the notes as they are now, or "to judge"."""
    a, b = sorted((_md(a), _md(b)))
    pid = proposal_id("duplicate", a, b)
    if _merged(a) or _merged(b):
        return "merged"
    if pid in dismissed(vault):
        return "dismissed"
    row = verdicts(vault).get(pid)
    return row["verdict"] if row and row.get("hash") == pair_hash(a, b) else "to judge"


def _merged(path: str) -> bool:
    from silica.kernel.write.contested import follow_superseded

    return follow_superseded(path) != path


def _named(why: str, concept: str, candidate: str) -> str:
    """The judge's reason with the two notes called by their names. The dedup
    prompt sets a pair out as an incoming concept against a candidate note, and
    the reasons kept its words ("The incoming concept is an exact copy of the
    candidate note", 2026-11-13), which no one reading the queue was told. A name
    the reason gives after the prompt's word is taken in, or it would read twice."""
    for words, name in ((r"the incoming (?:concept|excerpt|note)", concept), (r"the candidate(?: note)?", candidate)):
        why = re.sub(rf"\b{words}(?:\s+\(?{re.escape(name)}\)?)?(?!\w)", name.replace("\\", "\\\\"), why,
                     flags=re.IGNORECASE)
    return why


def _judged_duplicates(vault: str, items: list[CurationItem],
                       judge: bool = True) -> tuple[list[Proposal], dict[str, int]]:
    """The candidates the worker judged duplicates. It is asked only about a
    pair with no verdict for its notes as they are, at most JUDGE_CAP a run."""
    from silica import capabilities
    from silica.config import CONFIG
    from silica.kernel.recall.duplicates import pairs_to_items

    known, gone = verdicts(vault), dismissed(vault)
    rows: dict[str, dict] = {}
    out: list[Proposal] = []
    said = {"asked": 0, "duplicate": 0, "distinct": 0, "contradicts": 0, "waiting": 0}
    reachable = True
    for it in items:
        a, b = sorted((_md(it.target), _md(it.partner)))
        pid = proposal_id("duplicate", a, b)
        if pid in gone:
            continue  # a no is for good (ADR-0165 point 7): a verdict would buy nothing
        digest = pair_hash(a, b)
        if digest is None or _merged(a) or _merged(b):
            continue  # a note gone, or a pair a yes already merged: nothing left to judge
        row = known.get(pid)
        if not row or row.get("hash") != digest:
            if not judge or not reachable or said["asked"] >= JUDGE_CAP:
                said["waiting"] += 1
                continue
            said["asked"] += 1
            work = pairs_to_items([{"source": a, "target": b, "score": it.score}])
            decision = None
            try:
                decision = capabilities.judge_pair(work[0], CONFIG) if work else None
            except Exception as exc:
                # The worker is down or refused: every later pair would fail the
                # same way, so they wait for the next /curate instead of each
                # paying a failed call.
                logger.warning("curate: the worker could not judge %s and %s (%s)", a, b, exc)
                reachable = False
            if decision is None or not decision.judged:
                # An unparsed reply reads as "distinct" downstream; kept, it
                # would hide a duplicate until one of the notes changed.
                said["waiting"] += 1
                continue
            job = work[0].context
            row = {"notes": [a, b], "hash": digest, "verdict": decision.verdict,
                   "why": _named(decision.rationale, job.concept, job.candidate),
                   "claim": decision.addition if decision.verdict == "contradicts" else "", "at": time.time()}
            rows[pid] = row
        said[row["verdict"]] = said.get(row["verdict"], 0) + 1
        if row["verdict"] == "duplicate":
            out.append(Proposal(
                id=pid, kind="duplicate", title=f"{clean_name(it.target)} and {clean_name(it.partner)}",
                why=f"{it.reason}: {row['why']}" if row.get("why") else it.reason,
                notes=(_md(it.target), _md(it.partner)),
                data={"target": it.target, "partner": it.partner, "score": it.score, "reason": it.reason}))
    remember(vault, rows)
    return out, said


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


# What the worker's verdict means to the person who said yes
# (capabilities.orphan and .dedup return these).
_VERDICT = {"no_link": "the worker found no link worth writing",
            "no_candidates": "no note to link it from",
            "skipped": "the worker skipped it"}


def _landed(r: dict, notes: tuple[str, ...]) -> str | None:
    """None when the worker wrote, else what it decided. A duplicate family is
    `partial` when its members' verdicts differ: one merge written is a yes
    that landed. A contradiction is filed as a contested patch, `committed`."""
    from silica.kernel.write.contested import follow_superseded

    status = str(r.get("status") or "")
    if status == "committed" or (status == "partial" and any(
            x.get("status") == "committed" for x in r.get("results") or [])):
        return None
    if status == "no_merge":
        # A pure duplicate merges no text, so it answers no_merge, yet the
        # worker points the other note at the winner (superseded_by) and keeps
        # its name as an alias (dedup._route_verdict): that is the yes landing.
        if r.get("verdict") == "duplicate" and any(follow_superseded(n) != n for n in notes):
            return None
        return f"judged {r.get('verdict') or 'distinct'}: nothing merged"
    return _VERDICT.get(status, f"not written ({status or 'no answer'})")


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
        for p in props:
            if _md(str(r.get("target") or "")) in p.notes:
                out[p.id] = _landed(r, p.notes)
    return out


def _orphans(vault: str) -> list[Proposal]:
    """The last /curate's orphans that still are: a link written since (a yes
    on a link of this queue, an edit) or a note moved away answers the
    proposal, and a yes on it would spend the worker on a note that needs
    nothing. The driver's graph, so a listing still reads no model."""
    from pathlib import Path

    from silica.driver import DRIVER

    return [p for p in offered(vault, "orphan")
            if (Path(vault) / p.notes[0]).is_file() and not DRIVER.backlinks(p.notes[0])]


register(Kind("autolink", "Links to add", lambda vault: offered(vault, "autolink"), _apply_links))
register(Kind("orphan", "Orphans to connect", _orphans, _apply_judged, spends_model=True))
register(Kind("duplicate", "Duplicates to merge", lambda vault: offered(vault, "duplicate"), _apply_judged,
              spends_model=True))
