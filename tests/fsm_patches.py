# SPDX-License-Identifier: AGPL-3.0-or-later

"""Replace a name in every module of the injector run at once.

A collaborator the handlers share is bound once per module that imports it, so
a test driving the machine through several phases has that many names to
replace. `patch_run` is that loop, handing back the one mock they all see.
"""
from __future__ import annotations

import importlib
from contextlib import ExitStack, contextmanager
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

RUN_MODULES = (
    "silica.router.orchestrator",
    "silica.router.states.setup",
    "silica.router.states.collision",
    "silica.router.states.distill",
    "silica.router.states.write",
    "silica.router.states.linking",
    "silica.router.states.finalize",
)


@contextmanager
def patch_run(name: str, new: Any = None, **mock_kwargs: Any) -> Iterator[Any]:
    """`name` replaced by one object wherever the run binds it. Raises on a name
    no module binds: a stale patch must not pass while testing nothing."""
    binders = [m for m in RUN_MODULES if hasattr(importlib.import_module(m), name)]
    if not binders:
        raise AttributeError(f"no module of the run binds {name!r}")
    target = MagicMock(**mock_kwargs) if new is None else new
    with ExitStack() as stack:
        for module in binders:
            stack.enter_context(patch(f"{module}.{name}", target))
        yield target
