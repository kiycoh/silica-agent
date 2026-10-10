# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The picker (learn-suite D13): four pools, one interleave rule, an amnesty."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from silica.kernel.learn import pick

DAY = 86400.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _notes(now, spec):
    """spec: {path: (age_days, ai)}"""
    return {p: {"created": now - d * DAY, "ai": ai, "mtime": now - d * DAY} for p, (d, ai) in spec.items()}


def test_session_mixes_the_four_pools_with_their_quotas():
    now = time.time()
    notes = _notes(now, {
        **{f"A/due{i}.md": (400, False) for i in range(6)},   # pool A: decayed reader notes
        **{f"B/bot{i}.md": (5, True) for i in range(4)},       # pool B: unexplored AI notes
        "C/left.md": (5, False), "C/right.md": (5, False),     # pool C: a kept-apart pair
        "D/claim.md": (5, False),                              # pool D: contested
    })
    s = pick.session(
        n=8, now_ts=now, _notes_override=notes, _entries_override=[],
        _pairs_override=[("C/left.md", "C/right.md")], _conflicts_override=["D/claim.md"],
    )
    pools = [it["pool"] for it in s["items"]]
    assert len(s["items"]) == 8
    assert pools.count("due") == 4 and pools.count("unexplored") == 2
    assert pools.count("contrast") == 1 and pools.count("contradiction") == 1
    contrast = next(it for it in s["items"] if it["pool"] == "contrast")
    assert contrast["pair"] == ["C/left.md", "C/right.md"] and contrast["kind"] == "contrast"
    assert all(it["kind"] == "predict" for it in s["items"] if it["pool"] == "unexplored")
    assert s["amnesty"] is None


def test_due_orders_by_forgetting_bucket_then_gain_and_backfills_from_a_then_b():
    now = time.time()
    notes = _notes(now, {"A/x.md": (400, False), "A/y.md": (120, False), "B/bot.md": (5, True)})
    s = pick.session(n=8, now_ts=now, _notes_override=notes, _entries_override=[])
    paths = [it["path"] for it in s["items"]]
    assert paths.index("A/x.md") < paths.index("A/y.md")   # more forgotten first
    assert len(s["items"]) == 3 and s["short"] is not None  # material ran out: say so, never invent


def test_interleave_breaks_ties_by_folder_but_a_single_folder_scope_still_fills():
    now = time.time()
    notes = _notes(now, {**{f"A/a{i}.md": (400, False) for i in range(3)},
                         **{f"B/b{i}.md": (400, False) for i in range(3)}})
    s = pick.session(n=6, now_ts=now, _notes_override=notes, _entries_override=[])
    folders = [it["path"].split("/")[0] for it in s["items"]]
    assert folders[:2] in (["A", "B"], ["B", "A"])
    one = pick.session(n=4, now_ts=now, _notes_override=notes, _entries_override=[], scope=["A/a0.md", "A/a1.md", "A/a2.md"])
    assert len(one["items"]) == 3 and {it["path"].split("/")[0] for it in one["items"]} == {"A"}


def test_plan_frontier_comes_first_inside_its_scope():
    now = time.time()
    notes = _notes(now, {"P/step1.md": (400, False), "P/step2.md": (400, False), "Q/other.md": (400, False)})
    s = pick.session(n=4, now_ts=now, _notes_override=notes, _entries_override=[],
                     scope=["P/step1.md", "P/step2.md"], frontier=["P/step2.md"])
    assert s["items"][0]["path"] == "P/step2.md"
    assert all(it["path"].startswith("P/") for it in s["items"])


def test_amnesty_fires_when_due_exceeds_twice_the_session():
    now = time.time()
    notes = _notes(now, {f"A/n{i}.md": (400, False) for i in range(20)})
    s = pick.session(n=8, now_ts=now, _notes_override=notes, _entries_override=[])
    assert s["amnesty"] and "20" in s["amnesty"] and "debt" in s["amnesty"]
    assert len(s["items"]) == 8   # capped, never the whole backlog


def test_freshness_demotes_an_edited_note_to_predict():
    """A note edited since its last rating is asked as a pretest, not graded
    as forgetting an unchanged claim (D13 freshness)."""
    now = time.time()
    notes = {"A/edited.md": {"created": now - 400 * DAY, "ai": False, "mtime": now - DAY}}
    entries = [{"ts": _iso(now - 200 * DAY), "path": "A/edited.md", "correct": True, "rating": 3}]
    s = pick.session(n=2, now_ts=now, _notes_override=notes, _entries_override=entries)
    it = s["items"][0]
    assert it["stale"] is True and it["kind"] == "predict"


def test_revisit_queue_enters_next_session_with_priority():
    now = time.time()
    notes = _notes(now, {"A/x.md": (400, False), "A/y.md": (400, False), "A/fresh.md": (1, False)})
    s = pick.session(n=3, now_ts=now, _notes_override=notes, _entries_override=[], revisit=["A/fresh.md"])
    assert s["items"][0]["path"] == "A/fresh.md" and s["items"][0]["pool"] == "revisit"
