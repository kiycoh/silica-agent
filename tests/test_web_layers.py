# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The web layer stays a stack: `server.py` assembles the app and nothing reads it, and
below it the session reads no surface."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _imports_server(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == "silica.ui.web.server" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom):
            if node.module == "silica.ui.web.server":
                return True
            if node.module == "silica.ui.web" and any(a.name == "server" for a in node.names):
                return True
    return False


def _reaches(tree: ast.AST, package: str) -> bool:
    """An import of `package` or below it, lazy ones included."""
    def under(name: str | None) -> bool:
        return bool(name) and (name == package or name.startswith(package + "."))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(under(a.name) for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom):
            if under(node.module) or any(under(f"{node.module}.{a.name}") for a in node.names):
                return True
    return False


def _importers(folder: str, package: str) -> list[str]:
    return [str(p.relative_to(ROOT)) for p in (ROOT / folder).rglob("*.py")
            if _reaches(ast.parse(p.read_text(encoding="utf-8")), package)]


def test_no_surface_reads_the_repl():
    """The web took 16 private names from it, and a feature of a turn landed twice.
    What a surface shares is `silica/session`; the commands reach it through
    `session.commands`, which the CLI fills."""
    assert not _importers("silica/ui", "silica.cli")


@pytest.mark.parametrize("above", ["silica.ui", "silica.cli", "silica.onboarding"])
def test_the_session_reads_no_surface(above):
    assert not _importers("silica/session", above)


def test_nothing_imports_up_into_the_app():
    """Its lazy imports are why the routers once looked independent of it. The
    package's `serve` is the one way in, for the CLI, and it is relative."""
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / "silica").rglob("*.py")
                 if p != ROOT / "silica/ui/web/server.py" and _imports_server(ast.parse(p.read_text(encoding="utf-8")))]
    assert not offenders


def _loaded_after(module: str) -> set[str]:
    out = subprocess.run([sys.executable, "-c", f"import sys, {module}; print('\\n'.join(sys.modules))"],
                         capture_output=True, text=True, check=True, cwd=ROOT).stdout
    return set(out.split())


def test_the_static_site_builder_loads_no_app():
    """The host runs it by module path (`python -m silica.ui.web.pages_catalog`)."""
    loaded = _loaded_after("silica.ui.web.pages_catalog")
    assert "silica.ui.web.server" not in loaded and "fastapi" not in loaded


def test_a_turn_loads_no_web_framework():
    """The bridge runs a turn in `silica connect`, whose extra has no FastAPI."""
    assert "fastapi" not in _loaded_after("silica.ui.web.turn")


def test_the_web_event_map_loads_no_terminal_renderer():
    assert "silica.ui.renderer" not in _loaded_after("silica.ui.web.callback")
