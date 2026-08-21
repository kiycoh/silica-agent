# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Learner-model derived view (docs/specs/learner-model.md).

Per-note retention estimate R = exp(-dt / S), a pure function of three things
that already live on disk: the graded-quiz ledger (quiz.jsonl), note creation
dates, and the `AI: true` authorship stamp. Never materialized: a second store
of the same state diverges from the ledger at the first crash, so the view is
recomputed by scan on read, exactly like quiz.stats().

Learning events are the note's creation (reader-authored notes only: writing
is encoding, and an AI-written note was never learned) and graded answers.
Nothing else — passive exposure is the illusion of competence this exists to
kill, so a chat answer citing a note is NOT evidence the reader knows it.

The constants below are priors, not config: the ledger keeps raw history, so
they can be refit (or the whole decay replaced by FSRS) retroactively.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DAY = 86400.0
S0_USER = 90.0   # days of stability granted by writing a note yourself
S0_AI = 30.0     # days granted by the first correct answer on an AI note
GROWTH = 2.0     # a correct answer doubles stability
SHRINK = 4.0     # a miss divides stability by this
S_FLOOR = 1.0    # days: stability never shrinks below this
DUE_R = 0.6      # retention below this puts a note in the due pool
MISS_R = 0.3     # cap while the trailing answer is wrong: a coin flip at best


def key_of(path: str) -> str:
    """The join keyspace: quiz.key (posix, no .md, casefolded)."""
    from silica.kernel.report import quiz

    return quiz.key(path)


def note_state(created_ts: float, is_ai: bool, events: list, now_ts: float) -> dict:
    """Fold creation prior and graded answers into {R, S, last, misses, correct}.

    `events` is [(ts, correct)] in any order. R is None while nothing grants
    stability: an AI note with no graded answer is unknown, not forgotten.

    Only learning events move the decay clock (creation, correct answers). A
    miss is a measurement of NOT knowing: it shrinks stability and caps R, but
    updating `last` on it would make a note missed a second ago read as fresh.
    """
    s: float | None = None if is_ai else S0_USER
    last: float | None = None if is_ai else created_ts
    misses = correct = 0
    trailing_miss = False
    for ts, ok in sorted(events):
        if ok:
            correct += 1
            s = S0_AI if s is None else s * GROWTH
            last = ts
            trailing_miss = False
        else:
            misses += 1
            if s is not None:
                s = max(s / SHRINK, S_FLOOR)
            trailing_miss = True
    r = None
    if s is not None and last is not None:
        r = math.exp(-max(0.0, now_ts - last) / DAY / s)
        if trailing_miss:
            r = min(r, MISS_R)
    elif misses:
        r = 0.0  # nothing ever learned and measured wrong: known to be unknown
    return {"R": r, "S": s, "last": last, "misses": misses, "correct": correct}


def _created_and_ai(text: str, mtime: float) -> tuple[float, bool]:
    """A note's creation timestamp and authorship from its own frontmatter.

    Same date precedence as the timeline: explicit `date:` outranks the claim
    clock, and a note that states neither falls back to mtime — the ceiling
    AttentionCandidate already lives with.
    """
    from silica.kernel.write import frontmatter
    from silica.kernel.write.contested import note_clock

    data, _raw, _body = frontmatter.split(text)
    date = (data or {}).get("date") or note_clock(text)
    ts = mtime
    if date:
        try:
            ts = datetime.strptime(str(date)[:10], "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            pass  # unparseable statement: keep the mtime proxy
    return ts, bool((data or {}).get("AI"))


_meta_memo: dict[str, tuple[str, dict[str, dict]]] = {}  # vault -> (epoch, meta)


def _notes_meta(vault: Path) -> dict[str, dict]:
    """{relative path: {"created": ts, "ai": bool}} for every readable note.

    Timeline's walk verbatim: dot-dirs, .silicaignore matches and the verbatim
    sources dir are not the reader's notes. Memoized on the vault's file-state
    epoch like timeline._all_rows: repeated digest reads between vault changes
    cost one stat walk instead of a full re-parse.
    """
    from silica.kernel.recall.paths import SOURCES_DIR, ignore_matcher, vault_epoch

    epoch = vault_epoch(str(vault))
    if epoch:
        hit = _meta_memo.get(str(vault))
        if hit is not None and hit[0] == epoch:
            return hit[1]

    ignored = ignore_matcher(vault)
    out: dict[str, dict] = {}
    for f in sorted(vault.rglob("*.md")):
        parts = f.relative_to(vault).parts
        if any(p.startswith(".") for p in parts):
            continue
        if any(ignored(p) for p in parts[:-1]):
            continue
        if parts[0] == SOURCES_DIR:
            continue
        try:
            text = f.read_text(encoding="utf-8")
            mtime = f.stat().st_mtime
        except (OSError, UnicodeDecodeError):
            continue  # one unreadable note must not blind the view
        created, ai = _created_and_ai(text, mtime)
        out["/".join(parts)] = {"created": created, "ai": ai}

    if epoch:
        _meta_memo.clear()
        _meta_memo[str(vault)] = (epoch, out)
    return out


def _events_by_key(entries: list[dict]) -> dict[str, list]:
    out: dict[str, list] = {}
    for e in entries:
        try:
            ts = datetime.fromisoformat(str(e.get("ts"))).timestamp()
        except (ValueError, TypeError):
            continue  # an undatable grade cannot move a decay clock
        out.setdefault(key_of(str(e.get("path") or "")), []).append((ts, bool(e.get("correct"))))
    return out


def view(
    now_ts: float | None = None,
    _notes_override: dict | None = None,
    _entries_override: list | None = None,
) -> dict[str, dict]:
    """The whole vault's learner state, keyed by key_of(path)."""
    from silica.config import CONFIG
    from silica.kernel.report import quiz

    now = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    notes = _notes_override
    if notes is None:
        notes = _notes_meta(Path(CONFIG.vault_path))
    entries = _entries_override if _entries_override is not None else quiz.entries()
    events = _events_by_key(entries)
    out: dict[str, dict] = {}
    for path, meta in notes.items():
        k = key_of(path)
        st = note_state(meta["created"], bool(meta.get("ai")), events.get(k, []), now)
        st.update(path=path, ai=bool(meta.get("ai")), attempts=st["misses"] + st["correct"])
        out[k] = st
    return out


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _measured_stems(entries: list[dict], lang: str) -> set[str]:
    """Stems of every concept a logged question ever tested.

    Concepts are logged raw, as the asking model named them; resolution to the
    cooccur keyspace happens here, at read — so every future improvement to
    stemming re-resolves the whole history for free.
    """
    from silica.kernel.text.text import stem_word

    stems: set[str] = set()
    for e in entries:
        for name in e.get("concepts") or []:
            for tok in _TOKEN_RE.findall(str(name).lower()):
                if len(tok) > 2:
                    stems.add(stem_word(tok, lang=lang))
    return stems


def review_queue(
    limit: int = 10,
    target: str = "",
    now_ts: float | None = None,
    _notes_override: dict | None = None,
    _entries_override: list | None = None,
    _store=None,
) -> list[dict]:
    """The picker: what to quiz next, or (with target=) an area's full state.

    Global mode draws half from **due** (R below threshold, worst first) and
    half from **unexplored** (zero evidence, AI-unknown first, then notes whose
    concepts no question ever measured, central first). Target mode returns
    every note under the path prefix with its R and pool, unknown first — the
    calibration read /learn builds a syllabus from.
    """
    from silica.kernel.report import quiz

    entries = _entries_override if _entries_override is not None else quiz.entries()
    rows = view(now_ts=now_ts, _notes_override=_notes_override, _entries_override=entries)

    def why(r: dict) -> str:
        if r["R"] is not None and r["R"] < DUE_R:
            return "due"
        if r["attempts"] == 0:
            return "unexplored"
        return "known"

    for r in rows.values():
        r["why"] = why(r)

    if target:
        t = target.casefold()
        scoped = sorted(
            (r for r in rows.values() if r["path"].casefold().startswith(t)),
            key=lambda r: (r["R"] is not None, r["R"] if r["R"] is not None else 0.0),
        )
        return scoped

    store = _store
    if store is None:
        try:
            from silica.config import CONFIG
            from silica.kernel.recall.cooccurrence import get_cooccur_store

            store = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
        except Exception:
            store = None  # no index yet: rank unexplored by authorship alone

    measured: set[str] = set()
    adj_mass: dict[str, float] = {}
    if store is not None:
        try:
            measured = _measured_stems(entries, getattr(store, "lang", "en"))
            adj_mass = {s: sum(nb.values()) for s, nb in store.adjacency().items()}
        except Exception:
            store = None

    def gain(r: dict) -> float:
        if store is None:
            return 0.0
        try:
            nodes = store.note_nodes(r["path"])
        except Exception:
            return 0.0
        return sum(adj_mass.get(s, 0.0) for s in nodes if s not in measured)

    due = sorted((r for r in rows.values() if r["why"] == "due"), key=lambda r: r["R"])
    unexplored = sorted(
        (r for r in rows.values() if r["why"] == "unexplored"),
        key=lambda r: (not r["ai"], -gain(r), r["path"]),
    )
    n_due = min(len(due), max(1, limit // 2)) if due else 0
    picked = due[:n_due] + unexplored[: limit - n_due]
    if len(picked) < limit:  # one pool ran short: the other fills the round
        picked += due[n_due : n_due + (limit - len(picked))]
    return picked
