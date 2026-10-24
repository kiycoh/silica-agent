# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The source check (ADR-0110): a note the person wrote, read against the
vault on request, block by block. Rules decide what a rule can tell; the model
(step 2) only meaning. The check never edits the note, and never reads or
writes a `/verify` stamp.

Step 1 is this package with no model: blocks, the pool, a block's sources, the
rule findings and the grade table. `run.check_note` takes its search legs as
arguments, so the whole of it runs under a test with no index and no network.
"""
from __future__ import annotations

# The grade belongs to the kind, never to whoever found it (point 8): a model
# that graded could call its own contradiction a nit, and the same block would
# change grade between runs. One table, every kind under exactly one grade.
GRADES: dict[str, str] = {
    "contradicted": "error",
    "overstated": "error",
    "misquoted": "error",
    "not found": "warning",
    "not in the vault": "warning",
    "off-point": "warning",
    "source status": "warning",
    "date not found": "warning",
    "inconsistent": "warning",
    "quotation without its source": "nit",
    "no pinpoint": "nit",
}
ORDER = ("error", "warning", "nit")
