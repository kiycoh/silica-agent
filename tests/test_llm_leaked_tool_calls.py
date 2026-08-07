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
