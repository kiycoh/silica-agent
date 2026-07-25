# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Tool-block budget (chat toolset) and Anthropic prompt-cache breakpoint.

Both legs exist for the same reason: the tool schemas are the biggest static
chunk of every agentic turn. chat_tools() shrinks it, _with_prompt_cache stops
Anthropic re-billing it.
"""
from __future__ import annotations

import importlib
import json
import pkgutil

import silica.tools as T
from silica.agent.constraints import _CHAT_EXCLUDED, chat_tools
from silica.agent.llm import _with_prompt_cache


def _all_tools():
    for m in pkgutil.iter_modules(T.__path__):
        importlib.import_module("silica.tools." + m.name)
    return T.TOOLS


# --- Leg A: chat toolset ---------------------------------------------------

def test_chat_tools_excludes_only_the_named_batch_tools():
    tools = _all_tools()
    chat = set(chat_tools())
    default = {n for n, t in tools.items() if not t.sensitive and not t.internal}
    assert chat == default - _CHAT_EXCLUDED
    # Every excluded name must actually exist, or the exclusion is a silent no-op
    # that quietly stops saving anything.
    assert _CHAT_EXCLUDED <= set(tools)


def test_chat_tools_keeps_every_recovery_path_it_advertises():
    """A tool description that says "run X first" must leave X callable.

    This is the constraint that bounds the cut: hiding a tool some other tool
    points at turns its error hint into a dead end the model cannot act on.
    """
    tools = _all_tools()
    chat = set(chat_tools())
    for name in chat:
        desc = tools[name].description or ""
        for other in tools:
            if other in desc and other != name:
                assert other in chat, f"{name} tells the model to call {other}, which chat_tools() hides"


def test_chat_tools_keeps_the_multi_turn_review_protocol():
    # The vault review applies on a LATER turn than the one that reported, so a
    # plain "yes, go ahead" message still has to reach the ledger tools.
    chat = set(chat_tools())
    for name in ("silica_vault_report", "silica_ledger_next", "silica_ledger_update"):
        assert name in chat


def test_chat_tools_actually_cuts_the_block():
    tools = _all_tools()
    def cost(names):
        return sum(len(json.dumps(tools[n].json_schema())) for n in names)
    default = [n for n, t in tools.items() if not t.sensitive and not t.internal]
    assert cost(chat_tools()) < cost(default) * 0.80  # measured ~34% saving


# --- Leg B: prompt cache breakpoint ---------------------------------------

_MSGS = [{"role": "system", "content": "you are silica"}, {"role": "user", "content": "hi"}]


def test_cache_breakpoint_marks_system_for_anthropic():
    out = _with_prompt_cache("anthropic/claude-opus-4", _MSGS)
    assert out[0]["content"] == [
        {"type": "text", "text": "you are silica", "cache_control": {"type": "ephemeral"}},
    ]
    assert out[1] == _MSGS[1]


def test_cache_breakpoint_applies_through_a_proxy_prefix():
    out = _with_prompt_cache("openrouter/anthropic/claude-sonnet-4", _MSGS)
    assert isinstance(out[0]["content"], list)


def test_cache_breakpoint_skips_non_anthropic():
    for model in ("ollama/gemma4:e4b", "openai/gpt-4o", "gemini/gemini-2.0-flash"):
        assert _with_prompt_cache(model, _MSGS) is _MSGS


def test_cache_breakpoint_never_mutates_caller_history():
    # run_agent keeps appending to this exact list; a marker written in place
    # would leak into the stored conversation and every later turn.
    msgs = [dict(m) for m in _MSGS]
    _with_prompt_cache("anthropic/claude-opus-4", msgs)
    assert msgs[0]["content"] == "you are silica"


def test_cache_breakpoint_tolerates_odd_histories():
    assert _with_prompt_cache("anthropic/claude-opus-4", []) == []
    no_sys = [{"role": "user", "content": "hi"}]
    assert _with_prompt_cache("anthropic/claude-opus-4", no_sys) is no_sys
    blocks = [{"role": "system", "content": [{"type": "text", "text": "x"}]}]
    assert _with_prompt_cache("anthropic/claude-opus-4", blocks) is blocks


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
