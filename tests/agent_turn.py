# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The constraints for a test of the loop's own mechanics."""
from __future__ import annotations

from silica.agent import loop
from silica.agent.constraints import AgentConstraints


def open_turn() -> AgentConstraints:
    """A chat turn over every non-sensitive tool the loop's registry holds.

    What run_agent ran on with no constraints until 2026-11-25, when the
    argument became required: no product or eval caller left it out, only the
    tests of cancel, compaction and streaming, which register a fake tool or
    none and are not about which tools a surface offers. Built at the call and
    read off the loop's own TOOLS, so it sees the registry a test patched in."""
    return AgentConstraints(tools=tuple(n for n, t in loop.TOOLS.items() if not t.sensitive),
                            interactive=True)
