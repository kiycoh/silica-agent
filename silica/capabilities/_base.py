# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Shared building blocks for capabilities.

A capability is a plain ``run(item, config) -> dict`` function living in its own
module. The behaviours share a small skeleton — emit a feedback phase, read the
target note (or skip), check the cancel token — so those steps live here as free
functions each ``run()`` composes, keeping the per-behaviour variation explicit
rather than hidden in a base-class template method.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from silica.kernel.base.notekey import ledger_key
from silica.kernel.base.syntax import WIKILINK_TARGET_RE
from silica.kernel.workqueue import WorkItem
from silica.kernel.write.gate import CapabilityBounds
from silica.kernel.write.ops import Op, OpType
from silica.model.reply import parse_reply

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


class NoteContent(BaseModel):
    """The structured result of a whole-body authoring decision (expand / codewiki)."""

    content: str = ""


def emit_feedback(item: WorkItem, phase: str, detail: str = "") -> None:
    """Publish a WorkFeedbackEvent to the global bus (best-effort)."""
    from silica.obs.bus import BUS
    from silica.obs.events import WorkFeedbackEvent
    from silica.obs import narration as _narr_mod
    BUS.publish("work/feedback", WorkFeedbackEvent(item.id, item.kind, phase, detail))
    # The work lane (spec §4): repeated running beats on one id are progress
    # updates; the terminal lands in consume() where the outcome exists.
    _narr_mod.NARRATOR.narrate(
        "work", "running", f"{item.kind} {phase}" + (f": {detail}" if detail else ""),
        {"kind": item.kind, "phase": phase, "detail": detail}, id=f"wk-{item.id}")


def read_or_skip(path: str) -> tuple[str, dict | None]:
    """Read a note body. Returns ``(body, None)`` on success, or
    ``("", {"status": "skipped", ...})`` if the note is unreadable.

    The failure body is "" and not None on purpose: a tuple return cannot say
    "the body is a str exactly when skip is None", so every caller was left
    holding an Optional it then had to re-narrow. Both are falsy and every
    caller either returns on `skip` or tests the body for truth, so the two
    spellings were already interchangeable — this one is checkable.
    """
    from silica.driver import DRIVER
    try:
        return DRIVER.read_note(path).content or "", None
    except Exception as e:
        return "", {"status": "skipped", "reason": f"unreadable: {e}"}


def clean_twin_bundle(content_hash: str, heading: str) -> None:
    """Drop this concept's op from the deferred bundle it was parked in.

    Called only after a verified commit: the verdict has been routed into the
    vault, so the parked copy is no longer the durable one. Best-effort — a
    missing bundle (retry already flushed it, pre-C2 stub) is not an error.
    """
    if not content_hash:
        return
    try:
        from silica.kernel.write.deferred import get_deferred_store
        get_deferred_store().remove_op(content_hash, heading)
    except Exception as e:
        logger.debug("twin bundle cleanup failed (non-fatal): %s", e)


def load_prompt(name: str) -> str:
    """A prompt shipped with the capabilities. Raises when it is missing: a
    packaging miss must not reach the model as an empty instruction."""
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def parse_content(raw: str, key: str = "content") -> str:
    """Extract one string key from a structured authoring response.

    Returns ``""`` on any parse failure — the callers' no-op signal.
    """
    parsed = parse_reply(raw)
    return str(parsed[key]) if isinstance(parsed, dict) and key in parsed else ""


# ---------------------------------------------------------------------------
# Each worker's write envelope, over the gate's CapabilityBounds. The
# mechanism stays in kernel/write/gate.py; what one worker may write is that
# worker's policy, so it changes here.

def _wikilinks(text: str) -> set[str]:
    """Return the set of wikilink targets in `text` (case-insensitive, trimmed)."""
    return {m.strip().lower() for m in WIKILINK_TARGET_RE.findall(text or "") if m.strip()}


def make_backlink_guard(orphan_title: str) -> Callable[[Op, str], str | None]:
    """Build a content_guard requiring a patch to ADD a wikilink to `orphan_title`.

    Used by the orphan connector, which writes into a NEIGHBOUR: "added some
    wikilink" is not enough there, because only a link pointing back at the
    orphan lowers the orphan's in-degree. Any other link is a no-op that would
    still report the orphan as repaired.
    """
    want = ledger_key(orphan_title).rsplit("/", 1)[-1]

    def guard(op: Op, original: str) -> str | None:
        added = op.content if op.content is not None else (op.snippet or "")
        targets = {t.rsplit("/", 1)[-1] for t in _wikilinks(added)}
        if not want or want not in targets:
            return f"orphan repair added no wikilink to '{orphan_title}'"
        return None

    return guard


def _one_note_bounds(path: str, op: OpType, name: str, *, hub: str | None) -> CapabilityBounds:
    """One op of one kind on the framework-derived `path`, hub never touchable.
    `name` sets log attribution: the envelopes below differ in nothing else."""
    key = ledger_key(path)
    return CapabilityBounds(
        name=name,
        allowed_ops=frozenset({op}),
        target_predicate=lambda p: ledger_key(p) == key,
        forbidden_paths=frozenset({hub} if hub else set()),
    )


def dedup_bounds(larger_path: str, *, hub: str | None = None) -> CapabilityBounds:
    """Dedup bounds: append-only into the LARGER note of a borderline pair.

    The only permitted action is a `patch` against `larger_path`.  The model may
    never overwrite, delete, or create notes, and never touch the hub.  Which note
    is "larger" is decided mechanically by the framework (via ofm.metrics), not by
    the model.
    """
    return _one_note_bounds(larger_path, OpType.patch, "dedup", hub=hub)


def dedup_supersede_bounds(loser_path: str, *, hub: str | None = None) -> CapabilityBounds:
    """Supersede bounds: one `overwrite` of the note a merge just absorbed.

    The content is framework-computed (a single `superseded_by` frontmatter
    key), never model-authored — the envelope exists so the write cannot drift
    onto another path if that ever changes.
    """
    return _one_note_bounds(loser_path, OpType.overwrite, "dedup_supersede", hub=hub)


def dedup_alias_bounds(winner_path: str, *, hub: str | None = None) -> CapabilityBounds:
    """Alias bounds: one `overwrite` of the note a merge just absorbed INTO.

    Sibling of dedup_supersede_bounds on the other side of the merge. Separate
    from dedup_bounds, which guards a model-authored body and must stay
    patch-only, or a merge could rewrite the note it was supposed to append to.
    """
    return _one_note_bounds(winner_path, OpType.overwrite, "dedup_alias", hub=hub)


def alias_consolidation_bounds(canonical_path: str) -> CapabilityBounds:
    """One `overwrite` of the note gaining framework-computed `aliases:` keys.

    Same envelope as dedup_alias_bounds, own name for log attribution: this
    write comes from the /aliases consolidation pass, not from a merge.
    """
    return _one_note_bounds(canonical_path, OpType.overwrite, "alias_consolidation", hub=None)


def dedup_settle_bounds(path: str, *, hub: str | None = None) -> CapabilityBounds:
    """A soft-gate note judged distinct: one overwrite that drops its
    `review:` key and appends the relation trace. Framework-computed content."""
    return _one_note_bounds(path, OpType.overwrite, "dedup_settle", hub=hub)


def dedup_spoke_bounds(spoke_path: str, *, hub: str | None = None) -> CapabilityBounds:
    """Spoke bounds for a dedup `distinct` verdict: create exactly ONE new note.

    The judge that ruled a borderline pipeline concept distinct also authored
    its spoke (C2 verdict routing); the only permitted action is a `write` of
    `spoke_path` — the path the framework derived from the title, never one the
    model picked — and the hub is never touchable.
    """
    return _one_note_bounds(spoke_path, OpType.write, "dedup_spoke", hub=hub)


def expand_bounds(spoke_path: str, *, hub: str | None = None, landed: bool = False) -> CapabilityBounds:
    """Expand bounds: re-author exactly ONE thin spoke.

    A single op on the path the validator already sanitized, hub never
    touchable, under its own name so logs attribute it to the expand retry,
    not the dedup judge. `landed`: the thin note is on disk (soft gate), so
    the envelope is one overwrite of it instead of one write.
    """
    return _one_note_bounds(spoke_path, OpType.overwrite if landed else OpType.write, "expand", hub=hub)


def enrich_bounds(target_path: str, *, hub: str | None = None) -> CapabilityBounds:
    """Enrich bounds: append-only into the note being enriched.

    The only permitted action is a `patch` against `target_path`, the hub never
    touchable. The twin of dedup_bounds under its own name for log attribution.
    An addition cannot lose what the note says, which is why this envelope
    carries no content guard where the overwrite it replaced needed one.
    """
    return _one_note_bounds(target_path, OpType.patch, "enricher", hub=hub)


def orphan_bounds(
    neighbour_paths: list[str], *, orphan_title: str, hub: str | None = None
) -> CapabilityBounds:
    """Connector bounds: append-only patch into a NEIGHBOUR that links TO the orphan.

    An orphan is a note with in-degree 0 — literally what graph_report counts —
    so the link has to land in the neighbour and point back at the orphan. The
    pre-2026-08-14 bounds permitted a patch into the orphan itself, which only
    ever gave it out-degree: on a 795-note vault 39 of the 100 orphans already
    carried outgoing links and were still counted, so the `orphans` term of
    E(vault) sat frozen at +100 no matter how many repairs ran.

    Permits `patch` against the offered neighbours only, each introducing a
    wikilink to `orphan_title`.  Never overwrites, deletes, or creates.
    """
    allowed = {ledger_key(p) for p in neighbour_paths if p}
    forbidden = frozenset({hub} if hub else set())
    return CapabilityBounds(
        name="orphan",
        allowed_ops=frozenset({OpType.patch}),
        target_predicate=lambda p: ledger_key(p) in allowed,
        forbidden_paths=forbidden,
        content_guard=make_backlink_guard(orphan_title),
    )
