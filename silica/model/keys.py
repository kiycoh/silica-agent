# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A provider's keys (ADR-0063): the pool, and the ones sitting out.

Its own module because more than `call_llm` spends a key: a decision and the
usage read are requests too, and a benched key must sit out for them as well.
"""
from __future__ import annotations

import logging
import time

from silica.config import CONFIG, env_raw, live_env
from silica.model.errors import status_of
from silica.provider_table import PROVIDERS

logger = logging.getLogger(__name__)

# How long a key sits out once the endpoint blamed it. A rate limit clears in
# about the minute _RATE_LIMIT_ATTEMPTS already backs off over, so 60 s, not
# the hour Hermes Agent benches a key for after a second 429. An unpaid key
# (402) does not clear by waiting: the hour Hermes uses. A refused key (401,
# 403) gets the hour too, where Hermes waits 5 minutes: it has an OAuth token
# to refresh first, and a static key here has nothing that could change.
_KEY_BENCH_S = {429: 60.0, 401: 3600.0, 402: 3600.0, 403: 3600.0}
_benched: dict[str, float] = {}   # key -> monotonic time it may serve again


def key_pool(model: str) -> list[str]:
    """The keys `model` may be called with: its provider's key variable, then
    the numbered siblings (OPENROUTER_API_KEY_2, _3, ...) up to the first gap.
    custom/ reads SILICA_PROVIDER_API_KEY the same way. Fewer than two keys is
    no pool, and the call goes out exactly as it did before pools."""
    prefix = model.split("/", 1)[0]
    row = PROVIDERS.get(prefix)
    if row is None or not row.key_env:
        return []
    env = row.key_env
    first = CONFIG.provider_api_key if prefix == "custom" else live_env(env)
    keys = [first]
    n = 2
    while sibling := env_raw(f"{env}_{n}"):
        keys.append(sibling)
        n += 1
    return [k for k in keys if k]


def _by_readiness(keys: list[str]) -> list[str]:
    """The keys free to serve, in pool order, then the benched by who is back soonest."""
    now = time.monotonic()
    ready = [k for k in keys if _benched.get(k, 0.0) <= now]
    return ready + sorted((k for k in keys if k not in ready), key=lambda k: _benched[k])


def with_keys(keys: list[str], kwargs: dict, call):
    """One attempt, made with each key in turn while the endpoint blames the
    key; any other failure, or the last key's, goes up to the retry policy.
    Fill-first: the first healthy key serves until it is benched."""
    if len(keys) < 2:
        return call()
    last: BaseException | None = None
    for key in _by_readiness(keys):
        kwargs["api_key"] = key
        try:
            return call()
        except Exception as e:
            bench = _KEY_BENCH_S.get(status_of(e) or 0)
            if bench is None:
                raise
            _benched[key] = time.monotonic() + bench
            logger.warning("key ...%s for %s sits out %.0fs: %s", key[-4:], kwargs["model"], bench, e)
            last = e
    assert last is not None
    raise last


def ready_key(provider: str) -> str:
    """The key the next request to `provider` should carry, as a pool would
    choose it. "" when the provider has none."""
    keys = _by_readiness(key_pool(provider))
    return keys[0] if keys else ""
