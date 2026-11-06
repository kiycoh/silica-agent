# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What an endpoint's failure means, read off the exception it raised.

A provider words its failures its own way, so the wording is known here and
nowhere else: callers ask a question of an exception, never of its text. Stdlib
only, so naming a failure costs a caller no litellm (ADR-0073).
"""
from __future__ import annotations

import re

# llama.cpp's rejection of a prompt over n_ctx names both counts.
# shortcut: llama.cpp's wording only; LM Studio and vLLM word an overflow their
# own way and still raise. Add a pattern the first time one shows up in a log.
_LLAMA_OVERFLOW = re.compile(
    r"request \((\d+) tokens\) exceeds the available context size \((\d+) tokens\)"
)


def status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def context_overflow(exc: BaseException) -> tuple[int, int] | None:
    """(tokens sent, tokens the window holds) when a server refused a request
    for its size, else None."""
    over = _LLAMA_OVERFLOW.search(str(exc))
    return (int(over[1]), int(over[2])) if over else None


def shared_pool(exc: BaseException) -> bool:
    """A 429 the provider's shared capacity raised, not our key's own limit.

    OpenRouter names the source (`limit_source`) in the error body. The floor
    in llm.retry_transient paces our requests against a limit they count towards; a shared
    pool's is every other customer's traffic, so pacing ourselves frees nothing
    and only makes the user wait. On hosted t06 2026-10-21 the floor a failed
    turn left behind accounts for 32 of the next turn's 96 s (ADR-0124).
    """
    return "upstream_provider_shared_pool" in str(exc)


def server_fault(exc: BaseException) -> bool:
    """A 500 from the endpoint. Status, not `litellm.InternalServerError`: that
    class is the transport's to name, and every 500 it raises carries the code."""
    return status_of(exc) == 500


def unreadable_reply(exc: BaseException) -> bool:
    """llama-server b10053 500s a finished Qwen3.5 reply its chat parser cannot
    read ("does not match the expected peg-native format"). Asked the same way,
    the same reply comes back; a retry has to ask differently."""
    return server_fault(exc) and "does not match the expected" in str(exc)
