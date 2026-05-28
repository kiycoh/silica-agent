"""Phase 1 tests — ProgressLedger: schema, serialisation, dependency ordering."""
from __future__ import annotations

import pytest
from pathlib import Path

from silica.planner.progress import IssueCard, ProgressLedger, Task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ledger(tmp_path: Path, mode: str = "inject") -> ProgressLedger:
    """Return a fresh ProgressLedger whose save() target is tmp_path."""
    import silica.planner.progress as _mod
    monkeypatched = _mod._RUNS_DIR
    _mod._RUNS_DIR = tmp_path
    try:
        return ProgressLedger.new(mode=mode, inputs={"inbox": "test.md"})
    finally:
        _mod._RUNS_DIR = monkeypatched


# ---------------------------------------------------------------------------
# Round-trip serialisation
# ---------------------------------------------------------------------------

def test_roundtrip_empty(tmp_path):
    import silica.planner.progress as _mod
    orig_dir = _mod._RUNS_DIR
    _mod._RUNS_DIR = tmp_path
    try:
        p = ProgressLedger.new(mode="inject", inputs={"inbox": "notes.md"})
        p.save()
        p2 = ProgressLedger.load(p.run_id)
    finally:
        _mod._RUNS_DIR = tmp_path

    assert p2.run_id == p.run_id
    assert p2.mode == "inject"
    assert p2.inputs == {"inbox": "notes.md"}
    assert p2.tasks == []
    assert p2.issues == []
    assert p2.cursor is None


def test_roundtrip_with_tasks(tmp_path):
    import silica.planner.progress as _mod
    orig_dir = _mod._RUNS_DIR
    _mod._RUNS_DIR = tmp_path
    try:
        p = ProgressLedger.new(mode="inject", inputs={})
        t1 = p.add_task("recon", task_id="recon")
        t2 = p.add_task("payload", task_id="payload", depends_on=["recon"])
        p.set_status("recon", "done")
        p.set_status("payload", "running")
        p.save()
        p2 = ProgressLedger.load(p.run_id)
    finally:
        _mod._RUNS_DIR = tmp_path

    assert len(p2.tasks) == 2
    recon = next(t for t in p2.tasks if t.id == "recon")
    payload = next(t for t in p2.tasks if t.id == "payload")
    assert recon.status == "done"
    assert payload.status == "running"
    assert payload.depends_on == ["recon"]
    assert payload.attempts == 1


def test_roundtrip_preserves_issue_cards(tmp_path):
    import silica.planner.progress as _mod
    _mod._RUNS_DIR = tmp_path
    try:
        p = ProgressLedger.new(mode="inject", inputs={})
        p.add_task("recon", task_id="recon")
        p.issues.append(IssueCard(
            task_id="recon",
            question="Merge or create?",
            options=[{"label": "merge"}, {"label": "create"}],
            default_option="create",
        ))
        p.save()
        p2 = ProgressLedger.load(p.run_id)
    finally:
        _mod._RUNS_DIR = tmp_path

    assert len(p2.issues) == 1
    assert p2.issues[0].question == "Merge or create?"
    assert p2.issues[0].default_option == "create"


# ---------------------------------------------------------------------------
# next_pending — dependency ordering
# ---------------------------------------------------------------------------

def test_next_pending_respects_depends_on(tmp_path):
    import silica.planner.progress as _mod
    _mod._RUNS_DIR = tmp_path

    p = ProgressLedger.new(mode="inject", inputs={})
    p.add_task("recon",   task_id="recon")
    p.add_task("payload", task_id="payload", depends_on=["recon"])

    # payload has unmet dep → only recon is available
    nxt = p.next_pending()
    assert nxt is not None and nxt.id == "recon"

    p.mark_done("recon")
    nxt = p.next_pending()
    assert nxt is not None and nxt.id == "payload"


def test_next_pending_returns_none_when_all_done(tmp_path):
    import silica.planner.progress as _mod
    _mod._RUNS_DIR = tmp_path

    p = ProgressLedger.new(mode="inject", inputs={})
    p.add_task("recon", task_id="recon")
    p.mark_done("recon")

    assert p.next_pending() is None


def test_next_pending_skips_running_tasks(tmp_path):
    import silica.planner.progress as _mod
    _mod._RUNS_DIR = tmp_path

    p = ProgressLedger.new(mode="inject", inputs={})
    p.add_task("recon",   task_id="recon")
    p.add_task("payload", task_id="payload", depends_on=["recon"])

    p.set_status("recon", "running")
    # recon is running (not done) → payload dep unmet, nothing available
    assert p.next_pending() is None


# ---------------------------------------------------------------------------
# blocked tasks are never returned
# ---------------------------------------------------------------------------

def test_next_pending_never_returns_blocked(tmp_path):
    import silica.planner.progress as _mod
    _mod._RUNS_DIR = tmp_path

    p = ProgressLedger.new(mode="inject", inputs={})
    p.add_task("recon", task_id="recon")
    p.set_status("recon", "blocked")  # human must resolve

    assert p.next_pending() is None


def test_next_pending_skips_blocked_even_with_deps_met(tmp_path):
    import silica.planner.progress as _mod
    _mod._RUNS_DIR = tmp_path

    p = ProgressLedger.new(mode="inject", inputs={})
    p.add_task("recon",   task_id="recon")
    p.add_task("payload", task_id="payload", depends_on=["recon"])
    p.mark_done("recon")
    p.set_status("payload", "blocked")  # all deps met but blocked

    assert p.next_pending() is None


# ---------------------------------------------------------------------------
# set_status / mark_done / mark_failed
# ---------------------------------------------------------------------------

def test_set_status_running_increments_attempts(tmp_path):
    import silica.planner.progress as _mod
    _mod._RUNS_DIR = tmp_path

    p = ProgressLedger.new(mode="inject", inputs={})
    p.add_task("recon", task_id="recon")

    p.set_status("recon", "running")
    assert p.tasks[0].attempts == 1
    assert p.cursor == "recon"

    p.set_status("recon", "failed", error="oops")
    assert p.tasks[0].error == "oops"
    assert p.cursor is None


def test_mark_done_clears_cursor(tmp_path):
    import silica.planner.progress as _mod
    _mod._RUNS_DIR = tmp_path

    p = ProgressLedger.new(mode="inject", inputs={})
    p.add_task("recon", task_id="recon")
    p.set_status("recon", "running")
    p.mark_done("recon", output_ref="/tmp/recon_out.json")

    assert p.tasks[0].status == "done"
    assert p.tasks[0].output_ref == "/tmp/recon_out.json"
    assert p.cursor is None


def test_set_status_unknown_task_raises(tmp_path):
    import silica.planner.progress as _mod
    _mod._RUNS_DIR = tmp_path

    p = ProgressLedger.new(mode="inject", inputs={})
    with pytest.raises(KeyError, match="no-such-id"):
        p.set_status("no-such-id", "running")


def test_mark_failed_records_error(tmp_path):
    import silica.planner.progress as _mod
    _mod._RUNS_DIR = tmp_path

    p = ProgressLedger.new(mode="inject", inputs={})
    p.add_task("distill", task_id="distill")
    p.mark_failed("distill", "LLM timeout")

    t = p.tasks[0]
    assert t.status == "failed"
    assert t.error == "LLM timeout"


# ---------------------------------------------------------------------------
# add_task returns the Task and it is in ledger.tasks
# ---------------------------------------------------------------------------

def test_add_task_returns_task_appended(tmp_path):
    import silica.planner.progress as _mod
    _mod._RUNS_DIR = tmp_path

    p = ProgressLedger.new(mode="inject", inputs={})
    t = p.add_task("recon", task_id="recon", input_ref="/tmp/inbox.md")

    assert isinstance(t, Task)
    assert t in p.tasks
    assert t.input_ref == "/tmp/inbox.md"
    assert t.status == "pending"


# ---------------------------------------------------------------------------
# last_updated advances on mutation
# ---------------------------------------------------------------------------

def test_last_updated_advances_on_mutation(tmp_path):
    import time
    import silica.planner.progress as _mod
    _mod._RUNS_DIR = tmp_path

    p = ProgressLedger.new(mode="inject", inputs={})
    before = p.last_updated
    time.sleep(0.01)

    p.add_task("recon", task_id="recon")
    p.set_status("recon", "running")

    assert p.last_updated > before
