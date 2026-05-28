"""ProgressLedger — persistent run-state for the Silica planner.

Distinct from kernel/ledger.py (CommitLedger), which records what was
written to the vault.  This module records what the planner intends to do
and how far along it is.

Serialised to ~/.silica/runs/<run_id>/ledger.json via orjson.
"""
from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import orjson

TaskStatus = Literal["pending", "running", "done", "failed", "skipped", "blocked"]

_RUNS_DIR = Path.home() / ".silica" / "runs"


@dataclass
class IssueCard:
    """A question the planner cannot resolve without human input."""
    task_id: str
    question: str
    options: list[dict[str, Any]]
    default_option: str | None = None
    free_form_allowed: bool = False


@dataclass
class Task:
    """A single unit of work tracked by the ProgressLedger."""
    id: str
    capability_name: str
    status: TaskStatus = "pending"
    input_ref: str | None = None   # path to input payload on disk
    output_ref: str | None = None  # path to output payload on disk
    depends_on: list[str] = field(default_factory=list)
    attempts: int = 0
    error: str | None = None


@dataclass
class ProgressLedger:
    """Mutable, serialisable run-state for a planner pipeline execution."""
    run_id: str
    mode: str
    started_at: str          # ISO-8601 UTC
    last_updated: str        # ISO-8601 UTC
    inputs: dict[str, Any]
    tasks: list[Task] = field(default_factory=list)
    issues: list[IssueCard] = field(default_factory=list)
    cursor: str | None = None  # task_id currently running

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def new(cls, mode: str, inputs: dict[str, Any] | None = None) -> ProgressLedger:
        now = _now()
        return cls(
            run_id=uuid.uuid4().hex,
            mode=mode,
            started_at=now,
            last_updated=now,
            inputs=inputs or {},
        )

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def add_task(
        self,
        capability_name: str,
        *,
        task_id: str | None = None,
        input_ref: str | None = None,
        depends_on: list[str] | None = None,
    ) -> Task:
        t = Task(
            id=task_id or uuid.uuid4().hex,
            capability_name=capability_name,
            input_ref=input_ref,
            depends_on=depends_on or [],
        )
        self.tasks.append(t)
        return t

    def set_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        error: str | None = None,
    ) -> None:
        t = self._get(task_id)
        t.status = status
        if error is not None:
            t.error = error
        if status == "running":
            t.attempts += 1
            self.cursor = task_id
        elif status in ("done", "failed", "skipped", "blocked"):
            if self.cursor == task_id:
                self.cursor = None
        self._touch()

    def mark_done(self, task_id: str, *, output_ref: str | None = None) -> None:
        t = self._get(task_id)
        t.status = "done"
        if output_ref is not None:
            t.output_ref = output_ref
        if self.cursor == task_id:
            self.cursor = None
        self._touch()

    def mark_failed(self, task_id: str, error: str = "") -> None:
        t = self._get(task_id)
        t.status = "failed"
        t.error = error
        if self.cursor == task_id:
            self.cursor = None
        self._touch()

    def next_pending(self) -> Task | None:
        """Return first pending task whose dependencies are all done.

        Tasks with status 'blocked' are never returned — they require
        human resolution via IssueCard before they can proceed.
        """
        done_ids = {t.id for t in self.tasks if t.status == "done"}
        for t in self.tasks:
            if t.status != "pending":
                continue
            if all(dep in done_ids for dep in t.depends_on):
                return t
        return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> Path:
        run_dir = _RUNS_DIR / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "ledger.json"
        path.write_bytes(orjson.dumps(dataclasses.asdict(self), option=orjson.OPT_INDENT_2))
        return path

    @classmethod
    def load(cls, run_id: str) -> ProgressLedger:
        path = _RUNS_DIR / run_id / "ledger.json"
        data = orjson.loads(path.read_bytes())
        return _from_dict(data)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, task_id: str) -> Task:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise KeyError(f"Task {task_id!r} not found in run {self.run_id!r}")

    def _touch(self) -> None:
        self.last_updated = _now()


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _from_dict(data: dict[str, Any]) -> ProgressLedger:
    tasks = [Task(**t) for t in data.get("tasks", [])]
    issues = [IssueCard(**i) for i in data.get("issues", [])]
    return ProgressLedger(
        run_id=data["run_id"],
        mode=data["mode"],
        started_at=data["started_at"],
        last_updated=data["last_updated"],
        inputs=data.get("inputs", {}),
        tasks=tasks,
        issues=issues,
        cursor=data.get("cursor"),
    )
