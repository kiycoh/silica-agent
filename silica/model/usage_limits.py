# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A tenant's usage limits (ADR-0102): a 5-hour window of our own, and the
week and month OpenRouter enforces on the key, each read as the share used and
the instant it resets.

Every figure comes from the key itself, `GET /api/v1/key` sent with the
tenant's own OPENROUTER_API_KEY, so the numbers live where the deploy set them
(bin/tenant's `limit` and guardrail) and in SILICA_USAGE_WINDOW_SHARE, never in
code (the amendment of 2026-10-13). The fields read, verified on 2026-10-13
against https://openrouter.ai/openapi.json (operation getCurrentKey) and one
live read of a developer key:

    data.limit      USD, or null for a key with no limit
    data.usage      lifetime USD of the key ("Total OpenRouter credit usage")
    data.budgets[]  one per layer that applies to the key: scope ("key_limit"
                    for the key's own, "api_key" for a guardrail assigned to
                    it), limit_usd, spend_usd, remaining_usd ("Zero or negative
                    means this layer rejects requests with a 403 until it
                    resets"), reset_interval (daily|weekly|monthly|null) and
                    resets_at (ISO 8601 UTC, null for a lifetime budget)

The window's spend is a difference of `usage`, the lifetime total, and not of
`usage_monthly`: the monthly figure returns to zero on the 1st at 00:00 UTC,
and a window open across that instant would read a negative spend.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from silica.config import hosted

logger = logging.getLogger(__name__)

# A module constant and not a setting: the request carries the tenant's key,
# and a value a tenant could change (its .env, the settings panel) would let
# it send that key anywhere. Tests repoint it by monkeypatch.
KEY_URL = "https://openrouter.ai/api/v1/key"
# The read sits in front of every model call. One read from a laptop in Italy
# took 305 ms on 2026-10-13; 3 s is decide()'s budget for the same host, and
# past it the call goes ahead as on any failed read (point 5).
TIMEOUT_S = 3.0
WINDOW_S = 5 * 3600

# The layers bin/tenant makes. A daily or lifetime budget is none of ours:
# OpenRouter still enforces it on the call itself, and the student meets its
# 403 as before.
_LAYER_OF = {"weekly": "week", "monthly": "month"}

_now = time.time  # tests move the clock here
_lock = threading.Lock()  # call_llm runs on the loop, the distill pool and sub-agents at once


class UsageLimited(RuntimeError):
    """A model call refused before it was made, or a 403 OpenRouter returned at
    a limit: which layer (window, week or month) and when it resets."""

    def __init__(self, layer: str, resets_at: str | None):
        self.layer = layer
        self.resets_at = resets_at
        super().__init__(f"usage limit reached: {layer}, resets {resets_at or 'never'}")


@dataclass(frozen=True)
class Layer:
    name: str               # "window", "week" or "month"
    used: float             # share of the allowance spent, 0 to 1
    resets_at: str | None   # ISO 8601 UTC; None for a window not yet open
    spent: bool


def window_share() -> float | None:
    """SILICA_USAGE_WINDOW_SHARE as a fraction of the key's monthly limit, or
    None: no window. Read per call, like hosted(), so a test or a restart with a
    new env file needs no reload. The share is also the switch for the check
    before each call: without one a laptop makes no extra request."""
    raw = os.environ.get("SILICA_USAGE_WINDOW_SHARE", "").strip()
    if not raw:
        return None
    try:
        share = float(raw)
    except ValueError:
        share = 0.0
    if not 0 < share <= 1:
        logger.warning("SILICA_USAGE_WINDOW_SHARE is not a share between 0 and 1; no window")
        return None
    return share


def read_key() -> dict[str, Any]:
    """The key's own record from OpenRouter. Raises on any failure: the caller
    decides what a failed read means."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise LookupError("no OPENROUTER_API_KEY")
    resp = httpx.get(KEY_URL, headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json().get("data")
    if not isinstance(data, dict):
        raise ValueError("no data in the key's record")
    return data


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def budget_layers(data: dict[str, Any]) -> list[Layer]:
    """The week and the month from `budgets`. Two layers on one interval (a
    workspace budget beside the key's own) show as the one that binds first."""
    best: dict[str, Layer] = {}
    for b in data.get("budgets") or []:
        if not isinstance(b, dict):
            continue
        name = _LAYER_OF.get(str(b.get("reset_interval")))
        limit, spend, remaining = _num(b.get("limit_usd")), _num(b.get("spend_usd")), _num(b.get("remaining_usd"))
        if name is None or limit is None or spend is None or remaining is None:
            continue
        used = min(1.0, max(0.0, spend / limit)) if limit > 0 else 1.0
        layer = Layer(name, used, b.get("resets_at"), remaining <= 0)
        held = best.get(name)
        if held is None or (layer.spent, layer.used) > (held.spent, held.used):
            best[name] = layer
    return [best[n] for n in ("week", "month") if n in best]


def _state_path() -> Path:
    return Path.home() / ".silica" / "usage_window.json"


def _load_window() -> dict[str, float] | None:
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
        return {"opened_at": float(raw["opened_at"]), "usage_at_open": float(raw["usage_at_open"])}
    except (OSError, ValueError, KeyError, TypeError):
        # No file yet, or one this module did not write whole: either way no
        # window is open, and the next call opens one. At worst a torn file
        # restarts a window early, which costs the tenant nothing.
        return None


def _save_window(state: dict[str, float]) -> None:
    from silica.kernel.recall.paths import atomic_write_bytes

    atomic_write_bytes(_state_path(), json.dumps(state).encode("utf-8"))


def window(data: dict[str, Any], *, open_now: bool) -> Layer | None:
    """The 5-hour window (point 4), or None where there is none: no share, or
    a key with no limit. `open_now` is the call about to be made, which opens a
    window when none is open; a reading for the drawer never does."""
    share, limit = window_share(), _num(data.get("limit"))
    usage = _num(data.get("usage"))
    if share is None or limit is None or limit <= 0 or usage is None:
        return None
    allowance = share * limit
    now = _now()
    with _lock:
        state = _load_window()
        # A lower total than at the opening is another key (the tenant's was
        # replaced), whose window this is not.
        if state is None or now >= state["opened_at"] + WINDOW_S or usage < state["usage_at_open"]:
            if not open_now:
                return Layer("window", 0.0, None, False)
            state = {"opened_at": now, "usage_at_open": usage}
            try:
                _save_window(state)
            except OSError as e:
                # A home that cannot take the file (full, read-only) costs the
                # window, never the call: the rule point 5 sets for a failed
                # read. The next call opens the window again.
                logger.warning("usage limits: the window could not be saved (%s)", type(e).__name__)
    spend = usage - state["usage_at_open"]
    return Layer("window", min(1.0, max(0.0, spend / allowance)), _iso(state["opened_at"] + WINDOW_S),
                 spend >= allowance)


def _latest(spent: list[Layer]) -> Layer:
    """Of the layers used up, the one the student waits longest for: a spent
    month outlasts a spent week, and naming the week would promise answers on
    Monday that the month still refuses."""
    return max(spent, key=lambda layer: layer.resets_at or "~")


def check() -> None:
    """Before a call to OpenRouter (point 5): raise UsageLimited, without making
    the call, when a week or month is spent or the window's spend has reached
    its allowance. The week and the month are checked first, so a refused call
    never opens a window.

    A failed read lets the call go ahead: OpenRouter still enforces the week
    and the month on that very call, and refusing on a hiccup of a free
    endpoint would stop a student for nothing. The window is fairness between
    sittings, not the safety layer.

    ponytail: one extra request per model call, 305 ms in the one read
    measured (2026-10-13, from a laptop). Cache the reading for the length of a
    turn once the host measures it above 300 ms at p90.
    """
    if window_share() is None:
        return
    try:
        data = read_key()
    except Exception as e:
        logger.warning("usage limits: the key could not be read (%s); the call goes ahead",
                       type(e).__name__)
        return
    spent = [layer for layer in budget_layers(data) if layer.spent]
    if spent:
        hit = _latest(spent)
        raise UsageLimited(hit.name, hit.resets_at)
    w = window(data, open_now=True)
    if w is not None and w.spent:
        raise UsageLimited("window", w.resets_at)


def spent_layer() -> UsageLimited | None:
    """For a 403 a call met (point 6): the layer `budgets` shows spent now, or
    None when none is, and the 403 was something else. Never the window: that
    one is ours and refused before any call."""
    try:
        data = read_key()
    except Exception as e:
        logger.warning("usage limits: the key could not be read after a 403 (%s)", type(e).__name__)
        return None
    spent = [layer for layer in budget_layers(data) if layer.spent]
    if not spent:
        return None
    hit = _latest(spent)
    return UsageLimited(hit.name, hit.resets_at)


def status(model: str) -> list[dict[str, Any]]:
    """GET /limits (point 7): the window, then the week and the month, each
    `{layer, used, resets_at}`. Empty off hosted, for a model not on
    OpenRouter, a key with no limit, or a failed read: the drawer then shows
    nothing. Off hosted because a laptop's key limit is its owner's own cap,
    set on openrouter.ai, not an allowance Silica hands out (ADR-0122
    amendment 2026-10-27); it also spares that laptop a read per page load."""
    if not hosted() or not model.startswith("openrouter/"):
        return []
    try:
        data = read_key()
    except Exception as e:
        logger.warning("usage limits: the key could not be read for the drawer (%s)", type(e).__name__)
        return []
    if _num(data.get("limit")) is None:
        return []
    layers = budget_layers(data)
    w = window(data, open_now=False)
    if w is not None:
        layers.insert(0, w)
    return [{"layer": layer.name, "used": round(layer.used, 4), "resets_at": layer.resets_at}
            for layer in layers]
