# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia
"""The hosted pilot's usage lines, which carry no content (ADR-0100 point 3).

Each event is one INFO record on `silica.metrics`. The hosted stderr handler
writes it as a JSON line into the journal, and the nightly rollup in
silica-hosted counts it from there. A local install never logs one.
"""
import logging
import os
import time

from silica.config import hosted

_LOG = logging.getLogger("silica.metrics")


def emit(event: str, **fields) -> None:
    """Log one usage line.

    Every field value must be a number, a fixed word or a name the code
    defines. The line reaches the journal whole, so free text here would put
    a student's words where privacy notice v1.1 promises none.
    """
    # Both variables are read on every call, as hosted() reads its own: on an
    # objection (notice v1.1 section 9) bin/tenant sets SILICA_METRICS_OPTOUT
    # in the tenant's env file and restarts the unit, and the tests flip both
    # without a reload.
    if not hosted() or os.environ.get("SILICA_METRICS_OPTOUT", "").strip() == "1":
        return
    _LOG.info(event, extra={"metrics": fields})


def outcome_of(exc: BaseException) -> str:
    """A failed request's outcome: the HTTP status that came back, else the
    exception's class name. Never str(exc), which can quote the request."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return str(status) if isinstance(status, int) else type(exc).__name__


def number(value: object) -> int | float | None:
    """A provider's usage figure, or None: the reply is untrusted, and a string
    in a usage field would put its text in the journal."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def ms_since(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def transcribed(lane: str, started: float, outcome: str, payload: object = None) -> None:
    """One `stt` line, for either transcription client (dictation, media).

    The seconds and the USD are the provider's usage (OpenRouter's
    /audio/transcriptions schema, read 2026-10-15); a whisper-server reply has
    no usage, and verbose_json's duration stands in for its seconds."""
    body = payload if isinstance(payload, dict) else {}
    raw = body.get("usage")
    usage: dict = raw if isinstance(raw, dict) else {}
    emit("stt", lane=lane, outcome=outcome, ms=ms_since(started),
         seconds=number(usage.get("seconds")) or number(body.get("duration")),
         usd=number(usage.get("cost")))
