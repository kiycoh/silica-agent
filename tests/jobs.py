# SPDX-License-Identifier: AGPL-3.0-or-later

"""Jobs for tests that care about a few fields.

A job's required fields are required of the code that builds one for a run;
a test of the routing behind it names only what it is about.
"""
from __future__ import annotations

from typing import Any

from silica.kernel.workqueue import DedupJob, ExpandJob


def dedup_job(fields: dict[str, Any] | None = None, **more: Any) -> DedupJob:
    return DedupJob(**{"concept": "", "excerpt": "", "candidate": "", "score": 0.0, "inbox_file": "",
                       "hub": None, "content_hash": "", "run_id": "", "target_dir": "",
                       **(fields or {}), **more})


def expand_job(fields: dict[str, Any] | None = None, **more: Any) -> ExpandJob:
    return ExpandJob(**{"op": {}, "excerpt": "", "reason": "", "inbox_file": "", "hub": None,
                        "content_hash": "", "run_id": "", "target_dir": "",
                        **(fields or {}), **more})
