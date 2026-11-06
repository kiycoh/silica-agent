# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The two one-call shapes every lane asks for, written once.

`ask_once` is a single tool-less, thinking-off call for a lane that reads text
back; `ask_json` is one structured call parsed with its fallback. What each lane
does with the reply stays in the lane.
"""
from __future__ import annotations

from typing import Any

from silica.model import llm, providers
from silica.model.reply import parse_reply


def ask_once(prompt: str | list[dict], *, max_tokens: int, temperature: float = 0,
             role: str = "router") -> llm.LLMResponse:
    """One reply on `role`'s model. reasoning=False: these are mechanical asks
    on a tight budget, and a hybrid model bills its thinking against max_tokens,
    so with thinking on a 31KB source spent all 3864 completion tokens on the
    trace and returned an empty reply (the "parsed 0 facts" and the 45/45 / 92/92
    judge failures of run 5e88feb0). temperature=0 alone never fixed it: whether
    the model thinks is the router's call, not the temperature's."""
    messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt
    return llm.call_llm(providers.resolve_role(role)[0], messages, max_tokens=max_tokens,
                        temperature=temperature, reasoning=False)


def ask_json(messages: list[dict], *, config: Any, role: str = "worker", schema: Any = None,
             default: Any = None, **call: Any) -> Any:
    """One tool-less call on `role`, its reply as JSON, `default` when the reply
    is not JSON. A call that fails raises: only a reply can be defaulted."""
    reply = providers.get_provider(config, role=role).call_llm(
        messages=messages, tools=None, response_schema=schema, **call)
    return parse_reply(reply.text, default)
