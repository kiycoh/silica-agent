"""Streaming path: call_llm(on_delta=…) emits deltas and reassembles the response;
run_agent gates streaming to the interactive main loop; the renderer accumulates
the transient preview buffer and resets it at the turn boundaries."""
from unittest.mock import MagicMock, patch, PropertyMock

from silica.config import CONFIG
from silica.ui.console import CONSOLE
from tests.llm_mocks import litellm_mock_response


def _chunk(content=None, reasoning=None):
    delta = MagicMock()
    delta.content = content
    delta.reasoning_content = reasoning
    delta.reasoning = None
    choice = MagicMock()
    choice.delta = delta
    ch = MagicMock()
    ch.choices = [choice]
    return ch


def test_call_llm_streams_deltas_and_reassembles():
    from silica.agent.llm import call_llm

    chunks = [
        _chunk(reasoning="hmm"),
        _chunk(content="Hel"),
        _chunk(content="lo"),
        MagicMock(choices=[]),  # usage-only trailing chunk → skipped
    ]
    built = litellm_mock_response("Hello")
    deltas: list[tuple[str, str]] = []

    with patch("litellm.completion", return_value=iter(chunks)) as mock_completion, \
         patch("litellm.stream_chunk_builder", return_value=built) as mock_builder:
        res = call_llm(model="test_model", messages=[{"role": "user", "content": "hi"}],
                       on_delta=lambda t, c: deltas.append((t, c)))

    assert mock_completion.call_args.kwargs.get("stream") is True
    assert mock_builder.called
    assert deltas == [("reset", ""), ("reasoning", "hmm"), ("text", "Hel"), ("text", "lo")]
    assert res.text == "Hello"


def test_call_llm_without_on_delta_does_not_stream():
    from silica.agent.llm import call_llm

    with patch("litellm.completion", return_value=litellm_mock_response("Hi")) as mock_completion:
        res = call_llm(model="test_model", messages=[{"role": "user", "content": "hi"}])

    assert "stream" not in mock_completion.call_args.kwargs
    assert res.text == "Hi"


@patch("silica.agent.loop.call_llm")
def test_run_agent_streams_only_with_callback(mock_call_llm):
    from silica.agent.llm import LLMResponse
    from silica.agent.loop import run_agent

    mock_call_llm.return_value = LLMResponse(
        text="done", tool_calls=[],
        assistant_message={"role": "assistant", "content": "done"}, usage={},
    )

    run_agent([{"role": "user", "content": "x"}], model="m",
              tool_progress_callback=lambda e: None)
    assert callable(mock_call_llm.call_args.kwargs["on_delta"])

    # Without a callback the kwarg is omitted entirely (bare-signature doubles keep working)
    run_agent([{"role": "user", "content": "x"}], model="m")
    assert "on_delta" not in mock_call_llm.call_args.kwargs


def test_renderer_stream_buffer_accumulates_and_resets():
    from rich.console import Console
    from silica.agent.events import LLMStreamEvent, ThinkingEndEvent
    from silica.ui.renderer import make_progress_callback

    orig = (CONFIG.tool_progress, CONFIG.show_thinking, CONFIG.verbose)
    cb = make_progress_callback()
    try:
        CONFIG.tool_progress = "all"
        CONFIG.show_thinking = False
        CONFIG.verbose = False
        with patch.object(Console, "is_terminal", new_callable=PropertyMock, return_value=True), \
             patch.object(cb, "_update_stream_live", lambda: None):
            cb(LLMStreamEvent(chunk_type="text", content="Hel", iteration=1))
            cb(LLMStreamEvent(chunk_type="text", content="lo", iteration=1))
            assert cb._stream_buf == "Hello"
            # Reasoning deltas hidden unless thinking/verbose is on
            cb(LLMStreamEvent(chunk_type="reasoning", content="secret", iteration=1))
            assert cb._stream_buf == "Hello"
            # Retry reset clears the preview
            cb(LLMStreamEvent(chunk_type="reset", content="", iteration=1))
            assert cb._stream_buf == ""
            # Turn boundary clears it too — the final answer is printed by cli.py
            cb(LLMStreamEvent(chunk_type="text", content="again", iteration=1))
            cb(ThinkingEndEvent(iteration=1))
            assert cb._stream_buf == ""
    finally:
        CONFIG.tool_progress, CONFIG.show_thinking, CONFIG.verbose = orig
        cb.close()
