# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The review queue (ADR-0165): what Silica proposes to change, waiting for a yes
or a no, every kind in one list.

The registry of kinds and what they share: the Proposal, the store at
`<vault>/.silica/review/`, the dismissals and the accepted batches. A kind says
what it proposes (`pending`), lands what was accepted (`apply`) and may act on a
dismissal (`dismiss`). The kinds live one to a file in silica/tools/review/,
above the commands whose writes they reuse, which this lane cannot import.

Listing never calls a model. A proposal a model makes is made by its own command
(/aliases, /organize's dry run) and waits in `<kind>.json` until it is accepted
or dismissed; /curate's plan waits there too, because the report behind it is
minutes on a large vault. Accepting is one undo-journal run per batch, which
every write of the batch joins, so /revert takes the whole batch back.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from silica.kernel.base.durable import atomic_write_bytes

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Proposal:
    id: str
    kind: str
    title: str
    why: str                      # one line: what proposed it
    notes: tuple[str, ...] = ()   # the vault paths it touches
    preview: str = ""             # what accepting writes, as lines a person reads
    data: dict = field(default_factory=dict, compare=False)  # the kind's own, for apply


@dataclass(frozen=True)
class Kind:
    name: str
    label: str
    pending: Callable[[str], list[Proposal]]
    # {id: None} for a proposal that landed, {id: why} for one that did not and
    # stays in the queue. Writes join the batch's run (`gate.current_run()`).
    apply: Callable[[str, list[Proposal]], dict[str, str | None]]
    dismiss: Callable[[str, list[Proposal]], None] | None = None
    # Accepting runs the worker model: the GUI sends it through the chat, where
    # a turn is counted, and never through a route; `apply all` passes it over
    # (ADR-0175): on a copy of the demo vault on 2026-11-13 "all" took 34
    # proposals that each sent the worker a job, none of them named.
    spends_model: bool = False
    # `apply all` takes it. A stale note's yes says the note still holds, which
    # only a person who read it can say, so it is accepted by name or id alone.
    in_all: bool = True


KINDS: dict[str, Kind] = {}


def register(kind: Kind) -> Kind:
    KINDS[kind.name] = kind
    return kind


def proposal_id(kind: str, *key: str) -> str:
    """Stable across listings, so a dismissal outlives the next /curate: the
    kind and what the proposal is about, never its position."""
    return hashlib.sha1("\0".join((kind, *key)).encode()).hexdigest()[:10]


def _dir(vault: str) -> Path:
    return Path(vault) / ".silica" / "review"


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        # A torn or hand-edited file reads as empty: the queue loses what it
        # held, which the next run of its command offers again.
        logger.warning("review: %s unreadable (%s)", path.name, exc)
        return default


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, json.dumps(value, ensure_ascii=False, indent=1).encode())


def offer(vault: str, kind: str, proposals: list[Proposal]) -> None:
    """What a command proposed, waiting for review: the kind's whole list, the
    last run's, in place of the one before."""
    _write(_dir(vault) / f"{kind}.json", [asdict(p) for p in proposals])


def offered(vault: str, kind: str) -> list[Proposal]:
    rows = _read(_dir(vault) / f"{kind}.json", [])
    return [Proposal(**{**r, "notes": tuple(r.get("notes") or ())}) for r in rows if isinstance(r, dict)]


def _settle(vault: str, kind: str, ids: set[str]) -> None:
    """Take accepted or dismissed proposals out of a kind's offered list."""
    path = _dir(vault) / f"{kind}.json"
    rows = _read(path, None)
    if isinstance(rows, list) and any(isinstance(r, dict) and r.get("id") in ids for r in rows):
        _write(path, [r for r in rows if not (isinstance(r, dict) and r.get("id") in ids)])


def verdicts(vault: str) -> dict[str, dict]:
    """The worker's verdicts on duplicate candidates, by the pair's proposal id
    (ADR-0175): `notes`, the `hash` of both notes' text when it was asked,
    `verdict`, `why`, the `claim` a "contradicts" quoted and `at`. A
    "contradicts" waits here for the contradictions view (ADR-0166 point 6),
    which does not read it yet."""
    got = _read(_dir(vault) / "verdicts.json", {})
    return got if isinstance(got, dict) else {}


# shortcut: verdicts are never pruned, one row per pair ever judged (~300
# bytes). At 10,000 rows drop those whose notes are gone.
def remember(vault: str, rows: dict[str, dict]) -> None:
    if rows:
        _write(_dir(vault) / "verdicts.json", {**verdicts(vault), **rows})


# shortcut: dismissals are never pruned. One is ~100 bytes; at 10,000 entries
# drop those no kind lists any more, keeping the ones a recompute could revive.
def dismissed(vault: str) -> dict[str, dict]:
    got = _read(_dir(vault) / "dismissed.json", {})
    return got if isinstance(got, dict) else {}


def listing(vault: str) -> list[Proposal]:
    """Every pending proposal of every kind, the dismissed left out."""
    gone = dismissed(vault)
    out: list[Proposal] = []
    for kind in KINDS.values():
        try:
            out += [p for p in kind.pending(vault) if p.id not in gone]
        except Exception as exc:
            # One kind that cannot list (a git error under /stale, a store mid
            # write) leaves the others listed: the queue says what it can.
            logger.warning("review: %s could not list (%s)", kind.name, exc)
    return out


def pick(vault: str, words: list[str]) -> list[Proposal]:
    """The proposals `words` name: ids (or 4+ leading characters of one, as
    /revert takes a run), kind names, or `all`, which skips a kind that is not
    `in_all` or whose yes spends the model."""
    pending = listing(vault)
    chosen: dict[str, Proposal] = {}
    for w in (w.strip().lower() for w in words):
        if not w:
            continue
        if w == "all":
            hits = [p for p in pending if KINDS[p.kind].in_all and not KINDS[p.kind].spends_model]
        elif w in KINDS:
            hits = [p for p in pending if p.kind == w]
        elif len(w) >= 4:
            hits = [p for p in pending if p.id.startswith(w)]
        else:
            hits = []
        chosen.update((p.id, p) for p in hits)
    return list(chosen.values())


def _by_kind(props: list[Proposal]) -> dict[str, list[Proposal]]:
    groups: dict[str, list[Proposal]] = {}
    for p in props:
        groups.setdefault(p.kind, []).append(p)
    return groups


def accept(vault: str, props: list[Proposal]) -> dict:
    """Land `props` as one revertible run: `{"run", "applied", "failed"}`, the
    run None when nothing was written. A proposal that did not land stays in
    the queue with its reason."""
    from silica.kernel.write import gate
    from silica.kernel.write.undo_journal import get_undo_journal

    run = gate.open_run("review")
    applied: list[Proposal] = []
    failed: list[tuple[Proposal, str]] = []
    with gate.in_run(undo=run):
        for name, group in _by_kind(props).items():
            try:
                said = KINDS[name].apply(vault, group)
            except Exception as exc:
                # One kind failing leaves what the others landed: their writes
                # are journalled under the run already, and the revert of the
                # batch still takes them back.
                logger.warning("review: %s failed to apply (%s)", name, exc)
                said = {p.id: f"failed: {exc}" for p in group}
            for p in group:
                why = said.get(p.id, "not applied")
                if why is None:
                    applied.append(p)
                else:
                    failed.append((p, why))
            _settle(vault, name, {p.id for p in group if said.get(p.id, "") is None})
    journal = get_undo_journal()
    if not journal.inverses_for(run):
        journal.mark_reverted(run)  # nothing written: no batch to take back
        return {"run": None, "applied": applied, "failed": failed}
    batch = {"run": run, "at": time.time(), "count": len(applied),
             "kinds": sorted({p.kind for p in applied}), "titles": [p.title for p in applied[:5]]}
    log = _dir(vault) / "batches.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(batch, ensure_ascii=False) + "\n")
    return {"run": run, "applied": applied, "failed": failed}


def dismiss(vault: str, props: list[Proposal]) -> None:
    """Remember the no, and let each kind act on it (a deferred bundle is
    flushed, as `/review --flush` did)."""
    gone = dismissed(vault)
    for name, group in _by_kind(props).items():
        hook = KINDS[name].dismiss if name in KINDS else None
        if hook is not None:
            hook(vault, group)
        _settle(vault, name, {p.id for p in group})
        for p in group:
            gone[p.id] = {"kind": p.kind, "title": p.title, "at": time.time()}
    _write(_dir(vault) / "dismissed.json", gone)


def batches(vault: str, limit: int = 12) -> list[dict]:
    """The accepted batches, newest first, each with whether it still stands."""
    from silica.kernel.write.undo_journal import get_undo_journal

    try:
        lines = (_dir(vault) / "batches.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    journal = get_undo_journal()
    out = []
    for line in reversed(lines[-limit:]):
        try:
            b = json.loads(line)
        except ValueError:
            continue  # a torn last line from a crash mid-append: the others still read
        info = journal.run_info(b.get("run") or "")
        out.append({**b, "reverted": bool(info is None or info.get("reverted_at"))})
    return out


def revert(vault: str, run: str) -> dict | None:
    """Take an accepted batch back; None when `run` is no batch of this queue."""
    from silica.kernel.link.sweep import revert_and_sweep

    if not any(b["run"] == run for b in batches(vault, limit=10_000)):
        return None
    return revert_and_sweep(run)
