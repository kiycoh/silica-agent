from __future__ import annotations

import json
from types import SimpleNamespace
from types import SimpleNamespace as NS

from silica.agent.compaction import (
    MIN_COLLAPSE_CHARS,
    generic_projection,
    read_projection,
    read_stub,
    eager_stub,
    compact_read_history,
    elide_written_args,
    fit_window,
)


def test_min_collapse_chars_is_200():
    assert MIN_COLLAPSE_CHARS == 200


def test_generic_projection_keeps_scalars_elides_long_lists():
    out = generic_projection({
        "ok": True,
        "total": 25,
        "results": [{"path": f"n{i}"} for i in range(25)],
    })
    assert "ok=True" in out
    assert "total=25" in out
    assert "results=<25 items>" in out
    assert "re-call" in out  # re-fetch hint


def test_generic_projection_no_recall_hint_when_nothing_elided():
    """A lossless stub must not tell the model to re-call.

    silica_autolink returns two scalars; the unconditional hint read as "the
    batch is unfinished, call me again" and the agent re-ran a completed
    autolink six times.
    """
    out = generic_projection({"notes_scanned": 22, "notes_linked": 1, "total_links_added": 3})
    assert "notes_scanned=22" in out
    assert "re-call" not in out


def test_read_stub_names_the_call():
    out = read_stub("silica_read_note", '{"name": "Foo"}')
    assert out.startswith("⟪silica:")
    assert "silica_read_note" in out
    assert '{"name": "Foo"}' in out


def test_eager_stub_projects_a_dict_result_generically():
    tool = SimpleNamespace()
    result_str = json.dumps({"ok": True, "items": list(range(40))})
    out = eager_stub(tool, result_str)
    assert "ok=True" in out
    assert "items=<40 items>" in out


def test_eager_stub_passes_through_non_dict_payloads():
    tool = SimpleNamespace()
    assert eager_stub(tool, "plain string body") == "plain string body"


def _msgs():
    big = "x" * 300
    return [
        {"role": "user", "content": "hi"},                                              # 0
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function",
             "function": {"name": "silica_read_note", "arguments": '{"name": "Foo"}'}}]},  # 1
        {"role": "tool", "tool_call_id": "a", "content": big},                           # 2 (old read)
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "b", "type": "function",
             "function": {"name": "silica_read_note", "arguments": '{"name": "Bar"}'}}]},  # 3
        {"role": "tool", "tool_call_id": "b", "content": big},                           # 4 (recent)
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "type": "function",
             "function": {"name": "silica_read_note", "arguments": '{"name": "Baz"}'}}]},  # 5
        {"role": "tool", "tool_call_id": "c", "content": big},                           # 6 (recent)
    ]


_TOOLS = {"silica_read_note": NS(collapse="lazy")}


def test_no_collapse_under_budget():
    m = _msgs()
    collapsed = compact_read_history(m, set(), prompt_tokens=10, budget=100, floor_turns=2, tools=_TOOLS)
    assert collapsed == set()
    assert m[2]["content"] == "x" * 300


def test_collapses_old_read_protects_floor():
    m = _msgs()
    collapsed = compact_read_history(m, set(), prompt_tokens=200, budget=100, floor_turns=2, tools=_TOOLS)
    # 3 assistant turns, floor=2 → boundary at index 3; only message 2 is old.
    assert collapsed == {2}
    assert m[2]["content"].startswith("⟪silica:")
    assert m[2]["tool_call_id"] == "a"      # pairing preserved
    assert m[4]["content"] == "x" * 300      # within floor — untouched
    assert m[6]["content"] == "x" * 300


def test_skips_eager_and_never_and_unknown_tools():
    m = _msgs()
    tools = {"silica_read_note": NS(collapse="eager")}  # not lazy → skip
    collapsed = compact_read_history(m, set(), prompt_tokens=200, budget=100, floor_turns=2, tools=tools)
    assert collapsed == set()
    assert m[2]["content"] == "x" * 300


def test_skips_already_collapsed_and_tiny_bodies():
    m = _msgs()
    m[2]["content"] = "tiny"  # below MIN_COLLAPSE_CHARS
    collapsed = compact_read_history(m, {4}, prompt_tokens=200, budget=100, floor_turns=2, tools=_TOOLS)
    # index 2 too small to collapse; index 4 already in set; nothing new
    assert collapsed == {4}
    assert m[2]["content"] == "tiny"


def test_noop_when_not_enough_turns():
    m = _msgs()
    collapsed = compact_read_history(m, set(), prompt_tokens=999, budget=1, floor_turns=3, tools=_TOOLS)
    assert collapsed == set()  # exactly 3 assistant turns, floor=3 → nothing old


def test_skips_already_collapsed_before_boundary():
    """Isolates the `i in updated` guard (line 125 of compaction.py).

    When an index is already in the collapsed set passed in, compact_read_history
    must skip re-processing it and leave its content unchanged. Without this guard,
    a tool message with a body > MIN_COLLAPSE_CHARS would be re-stubbed even if
    already collapsed, corrupting the collapsed set tracking.
    """
    m = _msgs()
    collapsed = compact_read_history(m, {2}, prompt_tokens=200, budget=100, floor_turns=2, tools=_TOOLS)
    # Message 2 is already in collapsed set; should not be re-processed.
    assert collapsed == {2}
    assert m[2]["content"] == "x" * 300  # unchanged — guard prevented re-stubbing


def test_read_projection_keeps_paths_elides_the_fat_field():
    """The point of the projection: a recall's `notes` survive, its body doesn't."""
    out = read_projection(json.dumps({
        "query": "chemistry",
        "context": "y" * 40_000,
        "notes": ["Chem/Bonds.md", "Chem/Orbitals.md"],
        "facts": 2,
    }))
    assert "Chem/Bonds.md" in out and "Chem/Orbitals.md" in out
    assert "context=<40000 chars>" in out
    assert "facts=2" in out
    assert "y" * 200 not in out


def test_read_projection_ignores_non_json_bodies():
    assert read_projection("# A note body\n\nprose") == ""


def test_read_stub_carries_the_projection():
    out = read_stub("silica_recall", '{"query": "x"}', json.dumps({"notes": ["A.md"]}))
    assert "kept: notes=[\"A.md\"]" in out


def _rw_msgs(second_call: tuple[str, str]):
    """Read Foo, then make `second_call` — used to drive the invalidation pass."""
    big = "x" * 300
    name, arguments = second_call
    return [
        {"role": "user", "content": "hi"},                                                # 0
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function",
             "function": {"name": "silica_read_note", "arguments": '{"name": "Foo"}'}}]},  # 1
        {"role": "tool", "tool_call_id": "a", "content": big},                             # 2
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "b", "type": "function",
             "function": {"name": name, "arguments": arguments}}]},                        # 3
        {"role": "tool", "tool_call_id": "b", "content": big},                             # 4
    ]


_RW_TOOLS = {
    "silica_read_note": NS(collapse="lazy"),
    "silica_related": NS(collapse="lazy"),
    "silica_patch_note": NS(collapse="eager"),
}


def test_stale_read_collapses_under_budget():
    """A read whose note was patched afterwards is wrong, so budget is irrelevant."""
    m = _rw_msgs(("silica_patch_note", '{"name": "Foo", "snippet": "..."}'))
    collapsed = compact_read_history(
        m, set(), prompt_tokens=1, budget=10**9, floor_turns=99, tools=_RW_TOOLS
    )
    assert collapsed == {2}
    assert "stale" in m[2]["content"]
    assert m[2]["tool_call_id"] == "a"   # pairing preserved


def test_stale_matches_a_path_write_against_a_name_read():
    m = _rw_msgs(("silica_write_note", '{"path": "Sub/Foo.md", "body": "..."}'))
    tools = {**_RW_TOOLS, "silica_write_note": NS(collapse="eager")}
    collapsed = compact_read_history(
        m, set(), prompt_tokens=1, budget=10**9, floor_turns=99, tools=tools
    )
    assert collapsed == {2}


def test_superseded_read_collapses_and_keeps_the_later_copy():
    m = _rw_msgs(("silica_read_note", '{"name": "Foo"}'))
    collapsed = compact_read_history(
        m, set(), prompt_tokens=1, budget=10**9, floor_turns=99, tools=_RW_TOOLS
    )
    assert collapsed == {2}
    assert "superseded" in m[2]["content"]
    assert m[4]["content"] == "x" * 300  # the latest read survives


def test_a_different_read_of_the_same_note_supersedes_nothing():
    """silica_related(Foo) answers another question than silica_read_note(Foo)."""
    m = _rw_msgs(("silica_related", '{"note": "Foo"}'))
    collapsed = compact_read_history(
        m, set(), prompt_tokens=1, budget=10**9, floor_turns=99, tools=_RW_TOOLS
    )
    assert collapsed == set()
    assert m[2]["content"] == "x" * 300


def test_mutating_a_different_note_leaves_the_read_alone():
    m = _rw_msgs(("silica_patch_note", '{"name": "Bar", "snippet": "..."}'))
    collapsed = compact_read_history(
        m, set(), prompt_tokens=1, budget=10**9, floor_turns=99, tools=_RW_TOOLS
    )
    assert collapsed == set()
    assert m[2]["content"] == "x" * 300


def test_skips_unknown_tool_not_in_registry():
    """Isolates the `tool is None` guard (line 131 of compaction.py).

    When a tool is not in the registry (tool=None), compact_read_history must skip
    that message. Without this guard, the function would try to call read_stub()
    on a tool that doesn't exist, and the message would be incorrectly collapsed
    even though the tool's collapse mode is unknown.
    """
    m = _msgs()
    collapsed = compact_read_history(m, set(), prompt_tokens=200, budget=100, floor_turns=2, tools={})
    # Empty tools dict means silica_read_note is unknown (tool=None for all reads).
    # Nothing should be collapsed.
    assert collapsed == set()
    assert m[2]["content"] == "x" * 300  # unchanged — guard prevented collapse


# --- 2026-09-22: byte-identical excerpts across one turn's recalls ----------

def _recall(blocks: list[tuple[str, str]]) -> str:
    ctx = "\n\n---\n\n".join(f"[#{i} | {p} | embed:0.5]\n{ex}" for i, (p, ex) in enumerate(blocks, 1))
    return json.dumps({"query": "q", "context": ctx, "notes": [p for p, _ in blocks],
                       "partial": [], "facts": 0}, ensure_ascii=False)


def test_dedup_recall_excerpts_stubs_a_repeat_and_keeps_its_header():
    """Six recalls in one turn handed the model 87 windows for 56 notes, 8 of
    them byte-identical. By excerpt, not by note: a second query can return a
    complementary passage of the same note, which must survive."""
    from silica.agent.compaction import dedup_recall_excerpts

    seen: set[str] = set()
    first = dedup_recall_excerpts(_recall([("a", "alpha text"), ("b", "beta text")]), seen)
    assert first == _recall([("a", "alpha text"), ("b", "beta text")])  # nothing seen yet
    second = dedup_recall_excerpts(
        _recall([("a", "alpha text"), ("b", "beta text, second window"), ("c", "gamma")]), seen)
    d = json.loads(second)
    assert "[#1 | a | embed:0.5]\n(same excerpt as an earlier recall this turn)" in d["context"]
    assert "beta text, second window" in d["context"]      # complementary passage kept
    assert "gamma" in d["context"]
    assert d["notes"] == ["a", "b", "c"]                     # the rest of the reply untouched


def test_dedup_recall_excerpts_passes_non_json_and_errors_through():
    from silica.agent.compaction import dedup_recall_excerpts

    seen: set[str] = set()
    assert dedup_recall_excerpts("not json", seen) == "not json"
    err = json.dumps({"error": "boom", "context": ""})
    assert dedup_recall_excerpts(err, seen) == err


# --- 2026-09-22: a later SLICE of the same note is not "the same read" -------

def _slice_msgs(first: str, second: str):
    big = "x" * 300
    return [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "silica_read", "arguments": first}}]},
        {"role": "tool", "tool_call_id": "a", "content": big},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "b", "type": "function", "function": {"name": "silica_read", "arguments": second}}]},
        {"role": "tool", "tool_call_id": "b", "content": big},
    ]


_SLICE_TOOLS = {"silica_read": NS(collapse="lazy")}


def test_a_later_slice_of_the_same_note_supersedes_nothing():
    """Live run d08dccb60587 (2026-09-22): the model read Lezione 32 by line
    ranges and every slice was collapsed as "superseded" the moment it read the
    next one (13 collapses, peak prompt 25k), so it re-read the same ranges
    five times over and never answered. Superseded means the SAME read."""
    m = _slice_msgs('{"path": "Inbox/L32.md", "start": 599, "end": 650}',
                    '{"path": "Inbox/L32.md", "start": 412, "end": 435}')
    collapsed = compact_read_history(m, set(), prompt_tokens=1, budget=10**9, floor_turns=99, tools=_SLICE_TOOLS)
    assert collapsed == set()
    assert m[2]["content"] == "x" * 300


def test_the_identical_slice_re_read_is_superseded():
    m = _slice_msgs('{"path": "Inbox/L32.md", "start": 599, "end": 650}',
                    '{"end": 650, "path": "Inbox/L32.md", "start": 599}')  # same read, keys reordered
    collapsed = compact_read_history(m, set(), prompt_tokens=1, budget=10**9, floor_turns=99, tools=_SLICE_TOOLS)
    assert collapsed == {2}
    assert "superseded" in m[2]["content"]


def test_fit_window_leaves_a_request_that_fits_alone():
    m = _msgs()
    collapsed, est = fit_window(m, set(), estimate=900, limit=1000, tools=_TOOLS)
    assert collapsed == set() and est == 900
    assert m[2]["content"] == "x" * 300


def test_fit_window_collapses_oldest_first_and_stops_when_it_fits():
    m = _msgs()
    collapsed, est = fit_window(m, set(), estimate=1050, limit=1000, tools=_TOOLS)
    assert collapsed == {2}
    assert est <= 1000
    assert m[4]["content"] == "x" * 300 and m[6]["content"] == "x" * 300


def test_fit_window_reaches_into_the_floor():
    """Probe run longform-current-20260930T021321-r0: one batch of reads put
    the next request at 83925 tokens on a 65536 window. Those reads were the
    newest turn, which the budget sweep never touches, and llama.cpp answered
    400. A stub the model can re-issue beats a turn that dies."""
    m = _msgs()
    collapsed, _ = fit_window(m, set(), estimate=10**6, limit=1000, tools=_TOOLS)
    assert collapsed == {2, 4, 6}
    assert "context window" in m[6]["content"]


def test_fit_window_skips_non_lazy_tools():
    m = _msgs()
    collapsed, est = fit_window(m, set(), estimate=10**6, limit=1000,
                                tools={"silica_read_note": NS(collapse="eager")})
    assert collapsed == set() and est == 10**6


def _two_turns():
    """A read answered last turn (2), a new request (4), a read this turn (6)."""
    m = _msgs()[:3]
    m += [{"role": "assistant", "content": "done"},                                     # 3
          {"role": "user", "content": "now the other note"},                            # 4
          {"role": "assistant", "content": "", "tool_calls": [
              {"id": "d", "type": "function",
               "function": {"name": "silica_read_note", "arguments": '{"name": "Qux"}'}}]},  # 5
          {"role": "tool", "tool_call_id": "d", "content": "y" * 3000}]                # 6
    return m


def test_fit_window_shrinks_only_this_turns_read_and_leaves_it_sweepable():
    """Context cap 2026-10-13, finding 4: a fresh read stubbed for not fitting
    names a re-read that cannot fit either, and the model re-read Lezione 18
    ten times in one turn. The earlier turn's read is stubbed as before."""
    m = _two_turns()
    asked = []

    def shrink(name, arguments, body):
        asked.append(arguments)
        return "passages"

    collapsed, est = fit_window(m, set(), estimate=10**6, limit=1000, tools=_TOOLS, shrink=shrink)
    assert asked == ['{"name": "Qux"}']
    assert m[2]["content"].startswith("⟪silica: result elided")
    assert m[6]["content"] == "passages"
    assert collapsed == {2}


def test_fit_window_stubs_a_shrink_that_saves_less_than_half():
    """Otherwise the next iteration shrinks the shrunk body to the same
    passages and the request never comes under the limit."""
    m = _two_turns()
    collapsed, _ = fit_window(m, set(), estimate=10**6, limit=1000, tools=_TOOLS,
                              shrink=lambda *a: "y" * 1600)
    assert "context window" in m[6]["content"] and 6 in collapsed


def test_fit_shrink_is_off_by_default_and_reads_the_request_when_no_query(monkeypatch):
    from silica.agent import loop, read_scope
    from silica.tools import atomic

    seen = []
    monkeypatch.setattr(atomic, "narrowed_read", lambda name, q: seen.append((name, q)) or "p")
    token = read_scope.REQUESTS.set(("first", "the momentum rule please"))
    try:
        assert loop._fit_shrink("silica_read_note", '{"name": "L18"}', "x") is None
        monkeypatch.setattr(loop, "FIT_NARROWS", True)
        assert loop._fit_shrink("silica_read_note", '{"name": "L18"}', "x") == "p"
        assert loop._fit_shrink("silica_read_note", '{"name": "L18", "query": "momentum"}', "x") == "p"
        assert loop._fit_shrink("silica_read", '{"path": "L18.md"}', "x") is None
        assert loop._fit_shrink("silica_read_note", '{"name": "L18", "vault": "/o"}', "x") is None
    finally:
        read_scope.REQUESTS.reset(token)
    assert seen == [("L18", "the momentum rule please"), ("L18", "momentum")]


# --- 2026-10-22, hosted t06: a stub says whether this turn saw the read ------

def test_a_read_this_turn_saw_is_stubbed_as_seen_with_its_cost():
    """The chapter read before the write came back as "re-call to view
    again", which read as never seen: the model re-read it 14 times."""
    m = _msgs()
    fit_window(m, set(), estimate=10**6, limit=1000, tools=_TOOLS, unsent_from=len(m))
    assert m[2]["content"].startswith("⟪silica: result elided to fit the context window")
    assert "you read it in full this turn" in m[2]["content"]
    assert "(~100 tokens)" in m[2]["content"]   # 300 chars at 3 a token
    assert "to view again" not in m[2]["content"]


def test_a_read_no_request_carried_is_stubbed_as_never_seen():
    m = _msgs()
    fit_window(m, set(), estimate=10**6, limit=1000, tools=_TOOLS, unsent_from=5)
    assert "you read it in full" in m[2]["content"]
    assert "too large for this turn's window" in m[6]["content"]


def test_an_earlier_turns_read_keeps_the_re_call_advice():
    m = _two_turns()
    fit_window(m, set(), estimate=10**6, limit=1000, tools=_TOOLS, unsent_from=len(m))
    assert "to view again" in m[2]["content"]


def test_the_budget_sweep_tells_a_read_of_this_turn_it_was_seen():
    m = _msgs() + [{"role": "assistant", "content": f"t{i}"} for i in range(3)]
    collapsed = compact_read_history(m, set(), prompt_tokens=10**6, budget=1, floor_turns=3, tools=_TOOLS)
    assert collapsed == {2, 4, 6}
    assert "you read it in full this turn" in m[2]["content"]


def _write_call(name, args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "w", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_a_landed_write_keeps_a_pointer_not_its_body():
    body = "parola " * 100
    msg = _write_call("silica_write_note", {"path": "Inbox/R.md", "body": body, "tags": ["a"]})
    assert elide_written_args(msg, "w", "silica_write_note")
    tc = msg["tool_calls"][0]
    args = json.loads(tc["function"]["arguments"])
    assert tc["id"] == "w" and args["path"] == "Inbox/R.md" and args["tags"] == ["a"]
    assert args["body"] == f"⟪written to Inbox/R.md, {len(body)} chars: read the note to see it⟫"


def test_a_short_text_another_tool_or_another_call_is_left_alone():
    short = _write_call("silica_write_note", {"path": "a.md", "body": "x" * MIN_COLLAPSE_CHARS})
    read = _write_call("silica_read_note", {"name": "a", "body": "x" * 999})
    long = _write_call("silica_patch_note", {"name": "a", "snippet": "x" * 999})
    before = [json.dumps(m) for m in (short, read, long)]
    assert not elide_written_args(short, "w", "silica_write_note")
    assert not elide_written_args(read, "w", "silica_read_note")
    assert not elide_written_args(long, "other", "silica_patch_note")
    assert [json.dumps(m) for m in (short, read, long)] == before
