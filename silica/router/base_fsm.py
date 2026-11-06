# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Shared FSM mechanics for the pipelines of silica.router.phases."""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Generic, TypeVar

import orjson

from silica.kernel.base.home import silica_tmp_dir
from silica.router.phases import PipelineState

logger = logging.getLogger(__name__)

StateT = TypeVar("StateT", bound=PipelineState)


class BaseFSM(Generic[StateT]):
    """Abstract base for deterministic pipeline state machines.

    Each subclass must set in __init__:
        _phase_label      — log prefix ("Injector" / "Organizer")
        _done_state       — terminal-success state
        _error_state      — terminal-failure state
        _rollback_state   — rollback state (triggers txn revert on error)
        _HANDLERS         — dict mapping state → handler callable
        state, _tmp_files
    """

    # Annotated here for static analysis; values set by subclass __init__.
    _phase_label: str
    _done_state: StateT
    _error_state: StateT
    _rollback_state: StateT
    _HANDLERS: dict[StateT, Callable[[], None]]
    state: StateT
    _tmp_files: list[str]

    # ------------------------------------------------------------------
    # Shared execution mechanics
    # ------------------------------------------------------------------

    def step(self) -> None:
        logger.info("%s phase: %s", self._phase_label, self.state.name)
        handler = self._HANDLERS.get(self.state)
        if handler:
            handler()
        else:
            raise RuntimeError(f"No handler defined for state {self.state}")

    def transition_success(self) -> None:
        if self.state == self._rollback_state:
            self.state = self._error_state
            return
        steps = type(self.state).steps()
        if self.state in steps:
            idx = steps.index(self.state)
            if idx + 1 < len(steps):
                self.state = steps[idx + 1]
            else:
                self._on_pipeline_end()

    def _run_loop(self) -> None:
        """Common while-loop: dispatch steps, route errors, clean up tmp files."""
        try:
            while self.state not in (self._done_state, self._error_state):
                try:
                    logger.debug("FSM Transition: %s -> executing handler", self.state.name)
                    self.step()
                except Exception as e:
                    logger.error("FSM Error in state %s: %s", self.state, e)
                    self._record_error(str(e))
                    if not self._on_step_error(e):
                        self.state = self._error_state
        finally:
            self._cleanup_tmp()

    # ------------------------------------------------------------------
    # Hooks — override in subclasses to change terminal behaviour
    # ------------------------------------------------------------------

    def _record_error(self, message: str) -> None:
        """Keep a handler's failure where this pipeline's result reads it."""
        raise NotImplementedError

    def _on_pipeline_end(self) -> None:
        """Called when the phase sequence is exhausted."""
        self.state = self._done_state

    def _on_step_error(self, exc: Exception) -> bool:
        """Called on a handler's failure. Return True after setting
        ``self.state`` yourself to keep the loop going; False ends the run."""
        return False

    # ------------------------------------------------------------------
    # Shared tmp-file helpers
    # ------------------------------------------------------------------

    def make_tmp(self, content: Any, suffix: str = ".json") -> str:
        """Write content as JSON to ~/.silica/tmp/ and track for cleanup."""
        import uuid
        path = str(silica_tmp_dir() / f"{uuid.uuid4().hex}{suffix}")
        with open(path, "wb") as f:
            f.write(orjson.dumps(content, option=orjson.OPT_INDENT_2))
        self._tmp_files.append(path)
        logger.debug("Created staging file: %s", path)
        return path

    def _cleanup_tmp(self) -> None:
        for path in self._tmp_files:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._tmp_files.clear()
