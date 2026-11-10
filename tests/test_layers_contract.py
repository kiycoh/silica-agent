# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What the layers contract in pyproject.toml cannot say by itself: that it names every
package, so a new one cannot sit outside the order, and that the one import it lets
through, of the tools registry, leads nowhere."""
from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _layers() -> list[str]:
    contracts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["importlinter"]["contracts"]
    (layers,) = [c["layers"] for c in contracts if c["type"] == "layers"]
    return [m.strip() for line in layers for m in line.replace("|", ":").split(":")]


def _children(package: Path, dotted: str) -> set[str]:
    return {f"{dotted}.{p.stem}" for p in package.iterdir()
            if (p.suffix == ".py" and p.stem != "__init__") or (p / "__init__.py").is_file()}


def test_every_package_and_kernel_module_has_a_layer():
    named = set(_layers())
    children = (_children(ROOT / "silica", "silica") - {"silica.kernel"}) | _children(ROOT / "silica" / "kernel", "silica.kernel")
    assert not children - named, "put it on a line of the layers contract in pyproject.toml"
    assert not named - children, "a layer names a module that is gone"


def test_the_tools_registry_imports_nothing_of_silica():
    """The contract ignores imports of silica/tools/__init__.py from below, which is
    safe only while the registry reaches nothing that could import back up."""
    tree = ast.parse((ROOT / "silica" / "tools" / "__init__.py").read_text(encoding="utf-8"))
    reached = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {
        n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not {m for m in reached if m == "silica" or m.startswith("silica.")}
