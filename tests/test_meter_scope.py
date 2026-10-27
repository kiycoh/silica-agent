# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A meter scope counts the calls made inside it, apart from the call-sites
(ADR-0135 point 8): a Learn session or a reading job counts its own."""
from __future__ import annotations

import threading

import pytest

from silica.agent import llm


@pytest.fixture
def metering(monkeypatch):
    import collections

    monkeypatch.setattr(llm, "_METER_ON", True)
    monkeypatch.setattr(llm, "emit", lambda *a, **k: None)
    # the process-wide table prints at exit: these test calls stay out of it
    monkeypatch.setattr(llm, "_meter", collections.defaultdict(lambda: [0, 0, 0, 0]))
    monkeypatch.setattr(llm, "_meter_totals", [0, 0, 0, 0])


def _call(prompt=10, completion=5, cost=None):
    usage = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cost is not None:
        usage["cost"] = cost
    llm.LLMResponse(text="x", usage=usage)


def test_a_scope_counts_its_calls_and_a_cost_only_when_one_came(metering):
    m = llm.new_meter()
    _call()                                   # outside: counted nowhere
    with llm.meter_scope(m):
        _call()
        assert m == {"calls": 1, "prompt": 10, "completion": 5, "usd": None}
        _call(prompt=20, completion=2, cost=0.0015)
    assert m["calls"] == 2 and m["prompt"] == 30 and m["completion"] == 7 and m["usd"] == pytest.approx(0.0015)
    _call()
    assert m["calls"] == 2


def test_two_scopes_on_two_threads_never_read_each_others_calls(metering):
    a, b = llm.new_meter(), llm.new_meter()

    def work(m, n):
        with llm.meter_scope(m):
            for _ in range(n):
                _call()

    ts = [threading.Thread(target=work, args=(a, 3)), threading.Thread(target=work, args=(b, 5))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert a["calls"] == 3 and b["calls"] == 5


def test_a_thread_started_inside_does_not_inherit_the_scope(metering):
    m = llm.new_meter()
    with llm.meter_scope(m):
        t = threading.Thread(target=_call)
        t.start()
        t.join()
    assert m["calls"] == 0


def test_the_meter_is_off_and_counts_nothing_without_the_switch(monkeypatch):
    monkeypatch.setattr(llm, "_METER_ON", False)
    m = llm.new_meter()
    with llm.meter_scope(m):
        _call(cost=0.01)
    assert m == llm.new_meter()
