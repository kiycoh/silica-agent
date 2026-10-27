# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The grader classes a miss (ADR-0135 point 6); the picker reads the class
only once the gate has passed, and until then it is written and unread."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from silica.kernel.learn import grade, pick

REPLY = ("GRADE: {g}\nPRECISE: no\nRUBRIC: r\nFEEDBACK: f\nPARAPHRASE: no\nMISCONCEPTION: none\n"
         "MISS: {m}\nNOTE_DEFECT: none\n")


def test_the_class_is_kept_only_on_a_miss_and_only_when_it_is_one_of_the_five():
    assert grade.parse_reply(REPLY.format(g="lapsed", m="Structural."))["miss"] == "structural"
    assert "miss" not in grade.parse_reply(REPLY.format(g="recalled", m="cue"))
    assert "miss" not in grade.parse_reply(REPLY.format(g="partial", m="none"))
    assert "miss" not in grade.parse_reply(REPLY.format(g="partial", m="confusion"))
    assert "MISS: cue|discrimination|execution|careless|structural|none" in grade.GRADER_PROMPT


def test_last_miss_is_the_last_answer_and_a_later_success_clears_it():
    es = [{"path": "a.md", "correct": False, "miss": "cue"}, {"path": "b.md", "correct": False, "miss": "execution"},
          {"path": "a.md", "correct": True}, {"path": "b.md", "correct": False, "miss": "careless", "retest": True}]
    assert pick.last_miss(es) == {"b.md": "execution"}


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def test_the_picker_reads_the_class_behind_its_switch(monkeypatch):
    now = time.time()
    notes = {"A/x.md": {"created": now - 400 * 86400, "ai": False, "mtime": now - 400 * 86400}}
    entries = [{"ts": _iso(now - 300 * 86400), "path": "A/x.md", "correct": False, "rating": 1, "miss": "execution"}]
    kw = dict(n=1, now_ts=now, _notes_override=notes, _entries_override=entries, _pairs_override=[],
              _conflicts_override=[], _planned_override={"A/x.md"})
    assert pick.MISS_CLASS_READ is True           # on since 2026-10-27, the owner's call
    assert pick.session(**kw)["items"][0]["kind"] == "apply"
    monkeypatch.setattr(pick, "MISS_CLASS_READ", False)
    assert pick.session(**kw)["items"][0]["kind"] == "recall"
