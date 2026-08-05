# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

from __future__ import annotations
from dataclasses import dataclass
from typing import Any

@dataclass(slots=True)
class ToolStartEvent:
    name: str
    args: dict[str, Any]
    call_id: str
    iteration: int

@dataclass(slots=True)
class ToolCompleteEvent:
    name: str
    args: dict[str, Any]
    call_id: str
    result: str          # always a string, as returned by Tool.run()
    duration_s: float
    iteration: int

@dataclass(slots=True)
class ToolErrorEvent:
    name: str
    call_id: str
    error: str
    iteration: int

ToolProgressEvent = ToolStartEvent | ToolCompleteEvent | ToolErrorEvent

@dataclass(slots=True)
class ReasoningEvent:
    text: str
    iteration: int

@dataclass(slots=True)
class ThinkingStartEvent:
    iteration: int

@dataclass(slots=True)
class ThinkingEndEvent:
    iteration: int

@dataclass(slots=True)
class LLMStreamEvent:
    chunk_type: str
    content: str
    iteration: int

@dataclass(slots=True)
class BatchRunStartEvent:
    run_id: str
    kind: str    # "refine" | "enrich"
    label: str   # display label, e.g. "Concepts/ML"
    total: int   # total number of batches

RenderEvent = ToolProgressEvent | ReasoningEvent | ThinkingStartEvent | ThinkingEndEvent | LLMStreamEvent | BatchRunStartEvent


# --- work-queue events (published on silica.agent.bus.BUS) -------------------

@dataclass(slots=True)
class WorkFeedbackEvent:
    item_id: str    # WorkItem.id
    kind: str       # "dedup" | "expand" | "refine" | "orphan" | "enrich"
    phase: str      # "reading" | "calling_llm" | "committing"
    detail: str = ""


@dataclass(slots=True)
class PhaseEvent:
    """One InjectorFSM phase transition, published on "work/phase".

    Self-describing: every event restates the full run position, so a consumer
    needs no state machine of its own and a dropped event cannot strand a view
    on the wrong chunk — the next one re-declares where the run is.

    `chunk_total` is the current FILE's chunk count, never the run's: `_chunks`
    is flat but grows one file-group at a time (states/setup.py), so a run-wide
    denominator would shrink the reported progress every time a later file is
    partitioned. `file_idx` comes from `_current_file_idx`, which is correct
    during the file-scope phases; the chunk map is not (it still points into the
    previous file until that file's PAYLOAD lands).

    No call_id: agent/loop.py dispatches tool calls strictly sequentially, so
    these attach unambiguously to the last injector call still running.
    """
    phase: str          # recipe phase id, e.g. "distill"
    status: str         # "running" | "done" | "failed"
    scope: str          # "file" | "chunk" | "exception" (rollback)
    file_idx: int
    file_total: int
    chunk_idx: int
    chunk_total: int
    source_file: str = ""
    elapsed: float | None = None   # set on done/failed only


@dataclass(slots=True)
class WorkCompleteEvent:
    item_id: str
    kind: str
    status: str     # "done" | "no_merge" | "no_change" | "skipped" | "error"
    duration_s: float


@dataclass(slots=True)
class WorkCancelledEvent:
    item_id: str
    kind: str
    phase: str      # where cancellation was detected

