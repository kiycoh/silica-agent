# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia
"""The hosted pilot's usage lines, which carry no content (ADR-0100 point 3).

Each event is one INFO record on `silica.metrics`. The hosted stderr handler
writes it as a JSON line into the journal, and the nightly rollup in
silica-hosted counts it from there. A local install never logs one.
"""
import logging
import os

from silica.config import hosted

_LOG = logging.getLogger("silica.metrics")


def emit(event: str, **fields) -> None:
    """Log one usage line.

    Every field value must be a number, a fixed word or a name the code
    defines. The line reaches the journal whole, so free text here would put
    a student's words where privacy notice v1.1 promises none.
    """
    # Both variables are read on every call, as hosted() reads its own: on an
    # objection (notice v1.1 section 9) bin/tenant sets SILICA_METRICS_OPTOUT
    # in the tenant's env file and restarts the unit, and the tests flip both
    # without a reload.
    if not hosted() or os.environ.get("SILICA_METRICS_OPTOUT", "").strip() == "1":
        return
    _LOG.info(event, extra={"metrics": fields})
