# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A turn as a Server-Sent Events response, and the keepalive that carries it past the edge."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import suppress

from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from silica.config import demo
from silica.ui.web import flags, turn
from silica.ui.web.session import SESSION, TurnSlot


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# Cloudflare cuts a request whose origin sends no byte for 125 s (ADR-0099
# Context), and a slash command or a long tool call streams nothing while it
# works: hosted t01's /nucleate got a 524 at 124,990 ms on 2026-10-23, while web
# turns that kept streaming events ran to 333 s and ended done. ADR-0099 left
# streamed bytes untried for the jobs; a turn that stays a request needs them.
_KEEPALIVE_S = 30.0


async def _with_keepalive(chunks: AsyncGenerator[str, None], every: float) -> AsyncIterator[str]:
    """`chunks`, with an SSE comment line whenever `every` seconds pass without
    one. The pending step is waited on, never timed out, so a keepalive does not
    cancel the turn; a dropped client cancels it, as it did with no wrapper."""
    step = asyncio.ensure_future(chunks.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({step}, timeout=every)
            if not done:
                yield ": keepalive\n\n"
                continue
            try:
                chunk = step.result()
            except StopAsyncIteration:
                return
            yield chunk
            step = asyncio.ensure_future(chunks.__anext__())
    finally:
        if step.done():
            await chunks.aclose()
        else:
            step.cancel()
            # What the cancelled turn raises on its way out has no reader left:
            # its own finally already wrote the verdict and holds the gate.
            with suppress(asyncio.CancelledError, Exception):
                await step


def turn_response(slot: TurnSlot, ask: turn.Ask) -> StreamingResponse:
    """One agent turn as an SSE stream, which takes the holding over from `slot`:
    `run_turn` frees the gate, and `sweep_if_orphaned` when the body never runs."""
    slot.hand_off()

    async def gen():
        from silica.config import hosted

        # The done frame names the turn a flag would point at (ADR-0100 point
        # 6), hosted only: the one place a flag exists. Not a demo's, whose
        # flags would die unread with its HOME (ADR-0139 point 6). Read where
        # the session stood first, since the gate lets only this turn
        # narrate in it.
        flaggable = hosted() and not demo()
        sid0 = SESSION.chat.sid
        seq0 = await asyncio.to_thread(flags.session_head, sid0) if flaggable else 0
        async for item in turn.run_turn(ask):
            if flaggable and item.get("type") == "done":
                flag = await asyncio.to_thread(flags.turn_since, sid0, seq0)
                if flag:
                    item["turn"] = flag
            yield _sse(item)

    return StreamingResponse(
        _with_keepalive(gen(), _KEEPALIVE_S), media_type="text/event-stream",
        background=BackgroundTask(SESSION.sweep_if_orphaned)
    )


POLL_S = 0.25   # four wakeups a second per open tab, against a 1s deadline
