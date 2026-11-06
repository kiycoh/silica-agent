# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What `run_agent` returns when the turn has no answer to give.

In-band strings and not None or "": every caller gates its output on the
returned string being truthy, so an empty return renders as silence and the user
cannot tell a broken provider call from a turn that never ran. A caller that must
not take one for the model's words asks `is_sentinel`.
"""
from __future__ import annotations

_PREFIX = "(silica:"

CANCELLED = f"{_PREFIX} cancelled)"
MAX_ITERATIONS = f"{_PREFIX} maximum iterations reached)"
EMPTY_COMPLETION = f"{_PREFIX} the model returned an empty response)"


def is_sentinel(text: str) -> bool:
    return text.startswith(_PREFIX)
