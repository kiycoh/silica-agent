# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Images to the chat model (ADR-0077): Qwen3.5 is one model for text and
vision, so an image goes to the same endpoint the chat uses, as an OpenAI
`image_url` part, and never to a second model."""
from __future__ import annotations


def ask_chat_model(messages: list[dict], max_tokens: int = 700, cut: str = "") -> str:
    """The chat model's text answer to `messages`, which may carry image parts;
    `cut` is appended when the answer stopped at `max_tokens`, so a page read
    in part does not pass for the whole of it."""
    from silica.agent.providers import get_provider
    from silica.config import CONFIG
    # temperature 0 skips ADR-0080's card sampling: the same bytes should give
    # the same words. reasoning=False because a thinking pass spends the budget
    # before the answer (residue decompose, 2026-08-24, the same failure on a
    # smaller ask).
    resp = get_provider(CONFIG, role="router").call_llm(
        messages=messages, max_tokens=max_tokens, temperature=0.0, reasoning=False)
    text = resp.text or ""
    return text + cut if text and resp.finish_reason == "length" else text
