"""Architectural invariants for the capability seam.

CAPABILITIES (silica/capabilities/__init__.py) is THE dispatch table for
background work: every WorkItem kind produced anywhere in silica/ must have a
registered capability. A new producer that bypasses the seam fails here and
forces an explicit decision.
"""
from __future__ import annotations

import re
from pathlib import Path

SILICA_ROOT = Path(__file__).resolve().parent.parent / "silica"

# kind="..." within the argument list of a WorkItem(...) construction.
_KIND_RE = re.compile(r"WorkItem\([^)]*?kind=\"([a-z_]+)\"", re.DOTALL)


def _produced_kinds() -> set[str]:
    kinds: set[str] = set()
    for path in SILICA_ROOT.rglob("*.py"):
        kinds.update(_KIND_RE.findall(path.read_text(encoding="utf-8")))
    return kinds


def test_every_produced_workitem_kind_has_a_capability():
    from silica.capabilities import CAPABILITIES

    produced = _produced_kinds()
    assert produced, "scan found no WorkItem producers — regex drifted from the code"
    missing = produced - set(CAPABILITIES)
    assert not missing, (
        f"WorkItem kind(s) produced without a registered capability: {missing}. "
        "Register them in silica/capabilities/__init__.py or stop producing them."
    )
