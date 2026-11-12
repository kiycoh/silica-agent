# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The kinds of the review queue (ADR-0165), one file each.

A module here that calls `kernel.review.register` is a kind, and `load()`
imports every module here, so adding a kind is adding a file. Here, above the
router, the agent and the capabilities, because the writes a yes reuses live
there: the deferred retry, the leashed workers, the alias bounds.
"""
from __future__ import annotations

import importlib
import pkgutil

from silica.kernel import review
from silica.kernel.review import Kind


def load() -> dict[str, Kind]:
    """Register every kind and return the registry. Idempotent: an imported
    module is not run twice."""
    for mod in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{mod.name}")
    return review.KINDS
