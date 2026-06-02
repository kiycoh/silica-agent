"""Tests for cancel_token integration in LeashedSubAgent._run_* methods.

All LLM calls and commit_ops are patched so tests run without any external
dependencies.  The key invariants tested:

1. Pre-set token → method returns {"status": "cancelled"} without calling the LLM.
2. Mid-run token → method returns {"status": "cancelled"} at the next checkpoint.
"""
from __future__ import annotations

import threading
from unittest.mock import patch, MagicMock

import pytest

from silica.planner.workqueue import WorkItem
from silica.agent.subagent import LeashedSubAgent
from silica.config import SilicaConfig


def _item(kind: str, *, cancelled: bool = False) -> WorkItem:
    item = WorkItem(kind=kind, target_path="Notes/Test.md", context={"hub": "Hub"})
    if cancelled:
        item.cancel_token.set()
    return item


@pytest.fixture()
def agent():
    return LeashedSubAgent(SilicaConfig())


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

class _MockNote:
    content = "existing body"


def test_dedup_cancelled_before_llm(agent):
    item = _item("dedup", cancelled=True)
    with patch("silica.driver.DRIVER.read_note", return_value=_MockNote()):
        result = agent._run_dedup(item)
    assert result["status"] == "cancelled"


def test_dedup_cancelled_between_llm_and_commit(agent):
    """Token is set after _decide_dedup returns (mid-run cancellation)."""
    item = _item("dedup")
    from silica.agent.subagent import DedupDecision

    def fake_decide(**kwargs):
        item.cancel_token.set()   # simulate token being set during LLM call
        return DedupDecision(is_duplicate=True, rationale="dup", addition="new info")

    with patch("silica.driver.DRIVER.read_note", return_value=_MockNote()), \
         patch.object(agent, "_decide_dedup", side_effect=fake_decide):
        result = agent._run_dedup(item)

    assert result["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Refine
# ---------------------------------------------------------------------------

def test_refine_cancelled_before_llm(agent):
    item = _item("refine", cancelled=True)
    with patch("silica.driver.DRIVER.read_note", return_value=_MockNote()):
        result = agent._run_refine(item)
    assert result["status"] == "cancelled"


def test_refine_cancelled_between_llm_and_commit(agent):
    item = _item("refine")
    from silica.agent.subagent import RefineResult

    def fake_refine(*a, **k):
        item.cancel_token.set()
        return RefineResult(content="refined body")

    with patch("silica.driver.DRIVER.read_note", return_value=_MockNote()), \
         patch.object(agent, "_refine_note", side_effect=fake_refine):
        result = agent._run_refine(item)

    assert result["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Enrich
# ---------------------------------------------------------------------------

def test_enrich_cancelled_before_llm(agent):
    item = _item("enrich", cancelled=True)
    with patch("silica.driver.DRIVER.read_note", return_value=_MockNote()):
        result = agent._run_enrich(item)
    assert result["status"] == "cancelled"


def test_enrich_cancelled_between_llm_and_commit(agent):
    item = _item("enrich")
    from silica.agent.subagent import RefineResult

    def fake_enrich(*a, **k):
        item.cancel_token.set()
        return RefineResult(content="enriched body")

    with patch("silica.driver.DRIVER.read_note", return_value=_MockNote()), \
         patch.object(agent, "_enrich_note", side_effect=fake_enrich):
        result = agent._run_enrich(item)

    assert result["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Orphan
# ---------------------------------------------------------------------------

def test_orphan_cancelled_before_llm(agent):
    item = _item("orphan", cancelled=True)
    item.context["candidates"] = [{"name": "Hub", "path": "Hub.md"}]
    with patch("silica.driver.DRIVER.read_note", return_value=_MockNote()):
        result = agent._run_orphan(item)
    assert result["status"] == "cancelled"


def test_orphan_cancelled_between_llm_and_commit(agent):
    item = _item("orphan")
    item.context["candidates"] = [{"name": "Hub", "path": "Hub.md"}]
    from silica.agent.subagent import OrphanLinkDecision

    def fake_links(*a, **k):
        item.cancel_token.set()
        return OrphanLinkDecision(links=["Hub"], rationale="ok")

    with patch("silica.driver.DRIVER.read_note", return_value=_MockNote()), \
         patch.object(agent, "_decide_links", side_effect=fake_links):
        result = agent._run_orphan(item)

    assert result["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Phase feedback events are published
# ---------------------------------------------------------------------------

def test_feedback_events_published_on_normal_run(agent):
    """WorkFeedbackEvent is published to BUS for each phase on a normal run."""
    import silica.agent.bus as bus_mod
    received = []
    bus_mod.BUS.subscribe("work/feedback", received.append)

    item = _item("refine")
    from silica.agent.subagent import RefineResult

    with patch("silica.driver.DRIVER.read_note", return_value=_MockNote()), \
         patch.object(agent, "_refine_note", return_value=RefineResult(content="x")), \
         patch("silica.agent.subagent.commit_ops", return_value={"status": "committed"}):
        agent._run_refine(item)

    phases = [e.phase for e in received]
    assert phases == ["reading", "calling_llm", "committing"]
