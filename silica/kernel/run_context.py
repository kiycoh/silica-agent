# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The one way a run's ambient state crosses a thread.

A thread is born with an empty context, and everything a run keeps ambient is a
context variable: read scope, workspace, meter, undo and ledger runs, provenance,
the model call, the memory turn, narration's run and parent. Each boundary used to
rebind the subset it had thought of, so a variable added later reached some
threads and not others.

There are two kinds of thread and they want opposite things. A helper does the
same work on another thread, so it gets all of it (`RunContext.here`). A worker
of a run does work of its own, launched from a chat turn it is no part of: a
worker's read must not be narrowed to the turn's workspace, spend a read-scope
decision on the turn's request or taint the turn's memory gate, so it gets only
the run it belongs to (`RunContext.of_run`).
"""
from __future__ import annotations

import contextvars
from collections.abc import Callable
from typing import Any, TypeVar

from silica.obs import narration as _narr

T = TypeVar("T")


class RunContext:
    def __init__(self, context: contextvars.Context, run: str | None, parent: str | None,
                 undo: str | None = None) -> None:
        self._context = context
        self._run, self._parent, self._undo = run, parent, undo

    @classmethod
    def here(cls, narrated: bool = False) -> RunContext:
        """Everything ambient where this is called. `narrated` carries narration's
        place too; without it the thread's beats sit at the root."""
        return cls(contextvars.copy_context(), *((_narr.current_run(), _narr.current_parent()) if narrated else (None, None)))

    @classmethod
    def of_run(cls, run: str | None = None, parent: str | None = None, undo: str | None = None) -> RunContext:
        """Nothing but the run: its narration id and span, and its undo run."""
        return cls(contextvars.Context(), run, parent, undo)

    def spawn(self, fn: Callable[..., T]) -> Callable[..., T]:
        """`fn`, to call on another thread. Each call gets a copy, since a context
        can be entered by one thread at a time, and a pool calls this at once."""
        def entered(*args: Any, **kwargs: Any) -> T:
            return self._context.copy().run(self._enter, fn, args, kwargs)
        return entered

    def _enter(self, fn: Callable[..., T], args: tuple, kwargs: dict) -> T:
        from silica.kernel.write.gate import in_run

        _narr.set_run(self._run)
        _narr.set_parent(self._parent)
        with in_run(undo=self._undo):
            return fn(*args, **kwargs)
