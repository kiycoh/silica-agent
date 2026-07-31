# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Content-addressed cache for eval oracle calls (answer, judge, factscore).

An eval rerun repays every judge call it already paid, and the provider is
not deterministic even pinned at temperature 0 (one byte-identical prompt,
5 calls: 4 abstain, 1 correct). Freezing the first non-empty reply per
unique request makes unchanged cells bit-identical and free across reruns —
the frozen-corpus methodology extended to frozen judgments.

Key = sha256 of the canonical JSON of the ENTIRE request. The invariant: a
knob that can change the outcome is inside the key or it does not exist —
callers forward every knob, nothing is filtered out of the hash.

A cached verdict is one frozen sample of a nondeterministic distribution:
exactly right for A/B attribution, wrong forever if it froze wrong. The
audit valve is SILICA_EVAL_NO_CACHE=1 — a periodic full re-run that skips
reads and refreshes every entry in place.

Empty replies are transient provider drops (HTTP-200-no-text, so call_llm's
own retry never fires) — retried here with backoff, never cached. This
absorbs the three copy-pasted retry loops it replaces (LME judge, LoCoMo
answer, factscore._llm).

NOT for the agentic read path: answer_question_agent is the system under
measurement, and its tool-call variance at temp 0 (24/27/31 on a fixed
conversation) is signal, not noise to freeze.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent / ".oracle"
_ATTEMPTS = 3


def cached_text(model: str, messages: list[dict], **kwargs) -> str:
    """call_llm -> non-empty stripped .text, cached. "" = persistent empty
    (a provider/judge failure — mapped by callers, never cached)."""
    key = hashlib.sha256(json.dumps(
        {"model": model, "messages": messages, **kwargs},
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    path = _CACHE_DIR / key[:2] / f"{key}.json"
    if os.getenv("SILICA_EVAL_NO_CACHE") != "1":
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))["text"]
        except Exception as exc:  # corrupt entry = miss; the write below replaces it
            logger.warning("oracle: unreadable cache entry %s (%s)", path.name, exc)

    from silica.agent.llm import call_llm

    for attempt in range(_ATTEMPTS):
        resp = call_llm(model, messages, **kwargs)
        text = (resp.text or "").strip()
        if text:
            try:
                from silica.kernel.recall.paths import atomic_write_bytes

                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(path, json.dumps(
                    {"text": text, "model": model},
                    ensure_ascii=False).encode("utf-8"))
            except Exception as exc:  # a cache write must never fail an eval
                logger.warning("oracle: cache write failed for %s (%s)", path.name, exc)
            return text
        time.sleep(1.0 * (attempt + 1))
    return ""
