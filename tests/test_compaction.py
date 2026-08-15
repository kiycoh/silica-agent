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


def test_eager_stub_uses_tool_summarize_when_present():
    tool = SimpleNamespace(summarize=lambda r: f"validated={r['validated_count']}")
    result_str = json.dumps({"validated_count": 7, "rejected_ops": [1, 2, 3]})
    assert eager_stub(tool, result_str) == "validated=7"


def test_eager_stub_falls_back_to_generic_without_summarize():
    tool = SimpleNamespace(summarize=None)
    result_str = json.dumps({"ok": True, "items": list(range(40))})
    out = eager_stub(tool, result_str)
    assert "ok=True" in out
    assert "items=<40 items>" in out


def test_eager_stub_passes_through_non_dict_payloads():
    tool = SimpleNamespace(summarize=None)
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
