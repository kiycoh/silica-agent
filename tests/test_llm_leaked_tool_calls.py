"""The provider serialized a tool call into assistant content; the loop read the
markup as a final answer. Verbatim capture from the L3 gate (eBPF-A)."""
import json

from silica.agent.llm import build_assistant_message, recover_leaked_tool_calls

LEAKED = (
    '<｜DSML｜tool_calls>\n<｜DSML｜invoke name="web_fetch">\n'
    '<｜DSML｜parameter name="url" string="true">'
    'https://docs.kernel.org/bpf/verifier.html</｜DSML｜parameter>\n'
    '</｜DSML｜invoke>\n</｜DSML｜tool_calls>'
)


def test_leaked_call_becomes_a_real_call():
    msg, parsed = build_assistant_message(LEAKED, None)
    assert [c.name for c in parsed] == ["web_fetch"]
    assert parsed[0].args == {"url": "https://docs.kernel.org/bpf/verifier.html"}
    # the markup must not survive as the model's answer
    assert "DSML" not in msg.get("content", "")
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"])["url"].endswith(
        "verifier.html")


def test_prose_around_the_leak_survives():
    body, calls = recover_leaked_tool_calls("Let me check the docs.\n" + LEAKED)
    assert body == "Let me check the docs."
    assert len(calls) == 1


def test_multiple_leaked_calls_get_distinct_ids():
    _msg, parsed = build_assistant_message(LEAKED + "\n" + LEAKED, None)
    assert len({c.id for c in parsed}) == 2


def test_structured_calls_are_left_alone():
    """A real tool_calls payload must never be second-guessed, even if the
    content happens to mention DSML."""
    raw = [("call_1", "web_fetch", '{"url": "https://example.com"}')]
    _msg, parsed = build_assistant_message("talking about DSML markup", raw)
    assert [c.name for c in parsed] == ["web_fetch"]
    assert parsed[0].args == {"url": "https://example.com"}


def test_ordinary_content_untouched():
    msg, parsed = build_assistant_message("# A normal note\n\nBody text.", None)
    assert parsed == [] and msg["content"] == "# A normal note\n\nBody text."


QWEN_IN_REASONING = (
    "Nessuna nota con quel titolo esatto. Cerco nel testo delle note.\n\n"
    "<tool_call>\n<function=silica_search_context>\n<parameter=query>\n"
    "eteroschedasticità\n</parameter>\n<parameter=k>\n5\n</parameter>\n"
    "</function>\n</tool_call>"
)


def test_qwen_call_written_inside_reasoning_is_recovered_when_turn_is_empty():
    """Qwen3.5 on llama-server can write its call before closing </think>: the
    answer comes back empty and the call sits in the reasoning (seen live
    2026-09-27). Recovered, it runs instead of ending the turn empty."""
    msg, parsed = build_assistant_message("", None, QWEN_IN_REASONING)
    assert [(c.name, c.args) for c in parsed] == [
        ("silica_search_context", {"query": "eteroschedasticità", "k": "5"})]
    assert "tool_calls" in msg


def test_call_only_considered_in_reasoning_does_not_run_when_answered():
    _msg, parsed = build_assistant_message("Ecco la risposta.", None, QWEN_IN_REASONING)
    assert parsed == []
