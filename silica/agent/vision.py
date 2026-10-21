# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Images to the vision model (ADR-0077 and its 2026-10-21 amendment): the chat
model unless SILICA_VISION_MODEL names another, as an OpenAI `image_url` part.
Qwen3.5 and Qwen3.7 are one model for text and vision; DeepSeek V4 Flash, the
hosted chat model since that amendment, takes text only."""
from __future__ import annotations

from pathlib import Path


def ask_chat_model(messages: list[dict], max_tokens: int = 700, cut: str = "") -> str:
    """The vision model's text answer to `messages`, which may carry image parts.
    The name predates the vision role: unset, it is still the chat model.
    `cut` is appended when the answer stopped at `max_tokens`, so a page read
    in part does not pass for the whole of it."""
    from silica.agent.providers import get_provider
    from silica.config import CONFIG
    # temperature 0 skips ADR-0080's card sampling: the same bytes should give
    # the same words. reasoning=False because a thinking pass spends the budget
    # before the answer (residue decompose, 2026-08-24, the same failure on a
    # smaller ask).
    resp = get_provider(CONFIG, role="vision").call_llm(
        messages=messages, max_tokens=max_tokens, temperature=0.0, reasoning=False)
    text = resp.text or ""
    return text + cut if text and resp.finish_reason == "length" else text


def separate() -> bool:
    """Whether images go to a model other than the chat's. Then the chat model
    may take text only, and an image must reach it as words, never as pixels."""
    from silica.config import CONFIG
    return bool(CONFIG.vision_model) and CONFIG.vision_model != CONFIG.model


def read_attached(paths: list[Path], question: str) -> str:
    """The images attached to a chat turn as words, for a chat model that
    cannot see them: one reading each by the vision model, appended to the
    turn's text where the image parts would have gone."""
    from silica.kernel.recall import image_readings

    language = image_readings._language(question)
    out = []
    for p in paths:
        try:
            r = image_readings.read_image(Path(p), language, ask=ask_chat_model)
        except Exception:
            # A refused or failed vision call must not cost the question: the
            # turn still runs, and the marker sends the model to silica_look,
            # which asks for this image again.
            r = None
        out.append(f"\n\n[image {p}, read by {r['model']}] {image_readings._text(r)}" if r
                   else f"\n\n[image {p}: not read; silica_look can look at it]")
    return "".join(out)
