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


def _bound_modules(tree: ast.AST) -> dict[str, str]:
    """Local name -> silica module, for every module this file binds to a name."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "silica" and alias.asname:
                    bound[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "silica":
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bound


def test_every_attribute_read_off_a_silica_module_exists():
    """`cli._handle_direct_shortcut` survived a move in an eval: an attribute is no
    import, and mypy skips unannotated bodies. Names a file assigns itself are skipped,
    because a local can shadow a module and then the attribute is not the module's."""
    tracked = subprocess.check_output(["git", "ls-files", "silica/*.py", "evals/*.py", "scripts/*.py"],
                                      text=True, cwd=ROOT).split()
    broken = []
    for rel in tracked:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        bound = _bound_modules(tree)
        shadowed = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        shadowed |= {a.arg for n in ast.walk(tree) if isinstance(n, ast.arguments)
                     for a in n.args + n.kwonlyargs + n.posonlyargs}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                continue
            target = bound.get(node.value.id)
            if target is None or node.value.id in shadowed:
                continue
            try:
                module = importlib.import_module(target)
            except ImportError:
                continue  # a name imported from a module, not a module: nothing to check here
            if not hasattr(module, node.attr):
                broken.append(f"{rel}:{node.lineno}: {target}.{node.attr}")
    assert not broken, "\n".join(sorted(set(broken)))
