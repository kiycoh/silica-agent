# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Every `silica.*` name imported anywhere must resolve. evals/ and scripts/ run in no
gate (`testpaths` is tests/), and inside silica/ mypy skips unannotated function bodies,
so a function-local import of a moved name only raises the day that line runs."""
from __future__ import annotations

import ast
import importlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "silica":
            for alias in node.names:
                yield node.module, alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "silica":
                    yield alias.name, None


def test_every_silica_import_resolves():
    tracked = subprocess.check_output(["git", "ls-files", "silica/*.py", "evals/*.py", "scripts/*.py"],
                                      text=True, cwd=ROOT).split()
    assert len(tracked) > 400, f"only {len(tracked)} files found"
    broken = []
    for rel in tracked:
        for module, name in _imports(ROOT / rel):
            try:
                owner = importlib.import_module(module)
            except ImportError as exc:
                broken.append(f"{rel}: {module} ({exc})")
                continue
            if name is None or name == "*" or hasattr(owner, name):
                continue
            try:
                importlib.import_module(f"{module}.{name}")
            except ImportError:
                broken.append(f"{rel}: {module}.{name}")
    assert not broken, "\n".join(sorted(set(broken)))
