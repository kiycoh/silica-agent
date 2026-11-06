# SPDX-License-Identifier: AGPL-3.0-or-later

"""The router's import order, which import-linter cannot say.

The handlers name InjectorFSM in their annotations, so a contract that counts
TYPE_CHECKING imports reads a cycle that is not there, and one that drops them
for every contract loosens the others. These read the source instead.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROUTER = Path(__file__).parent.parent / "silica" / "router"


def _runtime_imports(path: Path) -> set[str]:
    """Modules imported at module level, TYPE_CHECKING blocks left out."""
    found: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found |= {node.module} | {f"{node.module}.{a.name}" for a in node.names}
    return found


def test_the_handlers_do_not_import_the_machine_that_dispatches_them():
    """Six state modules did, so that tests could patch one namespace, and the
    price was an 8-module cycle."""
    for path in (ROUTER / "states").glob("*.py"):
        assert "silica.router.orchestrator" not in _runtime_imports(path), path


def test_the_machine_names_its_handlers_and_does_not_import_them():
    assert not {m for m in _runtime_imports(ROUTER / "orchestrator.py")
                if m.startswith("silica.router.states")}


def test_the_table_and_the_state_sit_below_everything():
    """tool_labels reads the table on every start, so it costs the standard
    library and nothing else (ADR-0073)."""
    for name in ("phases.py", "run_state.py"):
        roots = {m.split(".")[0] for m in _runtime_imports(ROUTER / name)}
        assert roots <= set(sys.stdlib_module_names) | {"__future__"}, name
