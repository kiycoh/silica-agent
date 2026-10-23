# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""FSRS-6 replay of the quiz ledger (learn-suite D10).

The learner state stays a derived view (learner-model D3): every read folds
the ledger through `Scheduler.review_card`, nothing is materialized, and a
refit of the parameters rewrites history for free because the ledger is raw.
Card priors are the v1 ones: a reader-authored note is a Review card with
S=90 days at its creation date, an AI note has no card until its first rating.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fsrs import Card, Rating, Scheduler, State

# No intra-day steps: a note is not a card drilled ten minutes apart. Fuzz off:
# the view must be deterministic under test and identical on every surface.
# 365 days because a vault changes under its notes.
SCHEDULER = Scheduler(
    desired_retention=0.9, learning_steps=(), relearning_steps=(),
    maximum_interval=365, enable_fuzzing=False,
)

S0_USER = 90.0   # days of stability granted by writing a note yourself (v1 prior)
DUE_R = 0.9      # the scheduler's desired retention: below it a note enters the session pool
WEAK_R = 0.6     # the digest's WEAK RECALL line and the attention list, measured notes only
DAY = 86400.0

AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4


def rating_of(entry: dict) -> int:
    """The scheduling decision of a ledger line: `rating` 1..4 when present,
    else the legacy boolean mapped Again/Good. Emit new, recognize legacy."""
    r = entry.get("rating")
    if isinstance(r, int) and not isinstance(r, bool) and AGAIN <= r <= EASY:
        return r
    return GOOD if entry.get("correct") else AGAIN


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


def _prior_card(created_ts: float) -> Card:
    return Card(
        state=State.Review, stability=S0_USER,
        difficulty=SCHEDULER._initial_difficulty(rating=Rating.Good, clamp=True),
        due=_dt(created_ts) + timedelta(days=SCHEDULER._next_interval(stability=S0_USER)),
        last_review=_dt(created_ts),
    )


def note_state(created_ts: float, is_ai: bool, events: list, now_ts: float) -> dict:
    """Fold the creation prior and the rated answers into one note's state.

    `events` is [(ts, rating, correct)] in any order. R is None while nothing
    grants a card: an AI note with no rating is unknown, not forgotten.

    A miss (Again) shrinks stability through FSRS but never moves the clock:
    `last` stays on the last success, so R decays from the last time the
    note was actually recalled. Updating it on a miss would make a note
    missed a second ago read as fresh (the v1 defect, kept fixed).
    """
    card: Card | None = None if is_ai else _prior_card(created_ts)
    last: float | None = None if is_ai else created_ts
    misses = correct = 0
    for ts, rating, ok in sorted(events):
        if ok:
            correct += 1
        else:
            misses += 1
        when = _dt(ts)
        if card is None:
            card = Card(due=when)
        card, _log = SCHEDULER.review_card(card, Rating(rating), review_datetime=when)
        if rating == AGAIN:
            if last is not None:
                assert card.stability is not None  # review_card above always sets it
                card.last_review = _dt(last)
                card.due = card.last_review + timedelta(
                    days=SCHEDULER._next_interval(stability=card.stability)
                )
        else:
            last = ts
    if card is None:
        return {"R": None, "S": None, "D": None, "last": None, "due": None,
                "state": "new", "misses": misses, "correct": correct}
    if last is None:
        r = 0.0  # rated wrong and never right: known to be unknown
    else:
        r = float(SCHEDULER.get_card_retrievability(card, _dt(now_ts)))
    return {
        "R": r, "S": card.stability, "D": card.difficulty, "last": last,
        "due": card.due.timestamp(), "state": "review",
        "misses": misses, "correct": correct,
    }


def events_by_key(entries: list[dict]) -> dict[str, list]:
    """{note key: [(ts, rating, correct)]} over the ledger."""
    from silica.kernel.report.quiz import key

    out: dict[str, list] = {}
    for e in entries:
        try:
            ts = datetime.fromisoformat(str(e.get("ts"))).timestamp()
        except (ValueError, TypeError):
            continue  # an undatable rating cannot move a clock
        out.setdefault(key(str(e.get("path") or "")), []).append(
            (ts, rating_of(e), bool(e.get("correct")))
        )
    return out


def view(
    now_ts: float | None = None,
    _notes_override: dict | None = None,
    _entries_override: list | None = None,
    cold: frozenset[str] = frozenset(),
) -> dict[str, dict]:
    """The whole vault's learner state, keyed by quiz.key(path).

    `cold` paths start with no authorship prior: inside a plan with a date,
    a note the reader wrote is a card like any other until its first answer,
    or one Good gives it a stability of months and it never returns before
    the exam (simulated on the real fold, docs research report
    study-plans-and-deadline-scheduling, 2026-10-23).

    ponytail: no memo on (ledger length, epoch); the replay is one
    review_card per ledger line. Memoize when a profile shows the fold above
    the notes walk it sits next to, roughly at 20k lines.
    """
    from pathlib import Path

    from silica.config import CONFIG
    from silica.kernel.report import quiz
    from silica.kernel.report.learner import _notes_meta

    now = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    notes = _notes_override
    if notes is None:
        notes = _notes_meta(Path(CONFIG.vault_path))
    entries = _entries_override if _entries_override is not None else quiz.entries()
    events = events_by_key(entries)
    out: dict[str, dict] = {}
    for path, meta in notes.items():
        k = quiz.key(path)
        st = note_state(meta["created"], bool(meta.get("ai")) or path in cold, events.get(k, []), now)
        st.update(path=path, ai=bool(meta.get("ai")), attempts=st["misses"] + st["correct"])
        out[k] = st
    return out
