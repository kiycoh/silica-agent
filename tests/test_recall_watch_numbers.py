# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0095 in the turn's wrapper: what the turn read, what the session read,
and the line under the answer."""
from __future__ import annotations

import json

import silica.agent.narration as narr
from silica.agent.events import ToolCompleteEvent, ToolStartEvent
from silica.agent.recall_watch import (
    RecallWatch,
    numbers_from_beats,
    numbers_notes,
    numbers_of_session,
    result_numbers,
    unfounded_in_turn,
)


def done(name: str, result: str, call_id: str = "c1") -> ToolCompleteEvent:
    return ToolCompleteEvent(name=name, args={}, call_id=call_id, result=result,
                             duration_s=0.1, iteration=0)


def test_a_json_result_is_read_by_its_string_and_number_leaves():
    # Raw JSON escapes the newline, so "\n12" reads as a letter glued to 12
    # and the 12 would be lost; a number field is evidence as it stands.
    raw = json.dumps({"text": "line\n12 rows", "count": 242, "ok": True, "x": None})
    assert {"12", "242"} <= result_numbers(raw)
    assert result_numbers("plain text with 77 items") >= {"77"}


def test_a_failed_result_is_not_evidence():
    assert result_numbers(json.dumps({"error": "no note 999"})) == set()


def test_the_watch_collects_raw_results_and_forwards_every_event():
    seen = []
    watch = RecallWatch(seen.append)
    start = ToolStartEvent(name="silica_read_note", args={}, call_id="c1", iteration=0)
    watch(start)
    watch(done("silica_read_note", json.dumps({"body": "242 columns"})))
    watch(done("silica_write_note", json.dumps({"notes created": 20}), "c2"))
    watch(done("silica_read_note", json.dumps({"error": "missing 555"}), "c3"))
    assert {"242", "20"} <= watch.numbers
    assert "555" not in watch.numbers
    assert len(seen) == 4


def test_the_tokens_come_from_the_event_not_from_the_stubbed_message():
    # The loop may stub the tool message (eager tools, repeated recall, compaction);
    # the event carries the result whole, and the check never reads tool messages.
    watch = RecallWatch()
    watch(done("silica_read_note", json.dumps({"body": "the table holds 242 columns"})))
    messages = [
        {"role": "system", "content": "Today is 2026-10-11."},
        {"role": "user", "content": "how many columns?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "silica_read_note", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "[elided: read again]"},
        {"role": "assistant", "content": "242 columns."},
    ]
    assert unfounded_in_turn("242 columns.", watch.numbers, messages, set()) == []


def test_evidence_is_the_seed_the_user_and_the_session_never_the_model():
    messages = [
        {"role": "system", "content": "Today is 2026-10-11. The vault holds 318 notes."},
        {"role": "user", "content": "plan 45 minutes a day"},
        # the model's own earlier answer and a tool call's arguments
        {"role": "assistant", "content": "I count 243 variables.", "tool_calls": [
            {"id": "c9", "function": {"name": "silica_write_note",
                                      "arguments": json.dumps({"body": "777 rows"})}}]},
        {"role": "tool", "tool_call_id": "c9", "content": json.dumps({"ok": 1})},
        {"role": "user", "content": [{"type": "text", "text": "and 64 more?"}]},
    ]
    answer = "In 2026, 318 notes, 45 minutes, 64 more, 90 from earlier, 243 and 777."
    assert unfounded_in_turn(answer, set(), messages, {"90"}) == ["243", "777"]


def test_the_note_names_at_most_five_numbers():
    assert numbers_notes([]) == []
    assert numbers_notes(["243", "1.96"]) == [
        {"kind": "numbers", "text": "numbers not in what this turn read: 243, 1.96"}]
    many = ["11", "12", "13", "14", "15", "16", "17"]
    assert numbers_notes(many)[0]["text"] == (
        "numbers not in what this turn read: 11, 12, 13, 14, 15 and 2 more")


def test_the_session_set_rebuilt_from_narration_equals_the_live_one(_fresh_narrator, monkeypatch):
    monkeypatch.setattr("silica.config.CONFIG.vault_path", "/v/demo", raising=False)
    _fresh_narrator.ensure_session(driver="gui")
    events = [
        done("silica_read_note", json.dumps({"body": "242 columns, 1,067 rows"}), "a"),
        done("silica_search", "plain 31 hits", "b"),
        done("silica_read_note", json.dumps({"error": "no such note 404"}), "c"),
    ]
    live: set[str] = set()
    for turn in (events[:2], events[2:]):
        watch = RecallWatch()
        for ev in turn:
            _fresh_narrator.on_render_event(ev)
            watch(ev)
        live |= watch.numbers
    beats = list(narr.read_beats(narr.narration_dir() / f"{_fresh_narrator.sid}.jsonl"))
    assert numbers_from_beats(beats) == live
    assert numbers_of_session(_fresh_narrator.sid) == live
    assert numbers_of_session(None) == set() == numbers_of_session("deadbeef")
    assert "404" not in live and {"242", "1067", "31"} <= live
