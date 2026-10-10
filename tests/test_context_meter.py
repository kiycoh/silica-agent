"""The context meter counts with tiktoken directly (ADR-0073): no litellm import
to count, and text that looks like a special token is counted, not refused."""
import subprocess
import sys


def test_the_meter_counts_without_importing_litellm():
    """litellm cost the GUI 161 MB at startup to count the seed. Checked in a
    fresh interpreter: this test process imported litellm long ago."""
    code = (
        "import sys\n"
        "from silica.cli import _count_context_tokens\n"
        "n = _count_context_tokens([{'role': 'user', 'content': 'ciao'}],"
        " [{'type': 'function', 'function': {'name': 'x'}}])\n"
        "assert n > 4, n\n"
        "assert 'litellm' not in sys.modules\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]


def test_special_token_text_is_counted_not_sent_to_the_fallback():
    from silica.cli import _cl100k, _count_context_tokens

    text = "the model stops at <|endoftext|> and this note says so"
    expected = len(_cl100k().encode(text, disallowed_special=())) + 4
    assert _count_context_tokens([{"role": "user", "content": text}]) == expected


def test_tool_calls_and_the_tool_block_are_counted():
    from silica.cli import _count_context_tokens

    call = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "silica_recall", "arguments": '{"query": "x"}'}}]}
    tools = [{"type": "function", "function": {"name": "silica_recall", "parameters": {"type": "object"}}}]
    bare = _count_context_tokens([{"role": "assistant", "content": ""}])
    assert _count_context_tokens([call]) > bare
    assert _count_context_tokens([call], tools) > _count_context_tokens([call])
