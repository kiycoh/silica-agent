"""A local `import x` inside a function that ALSO uses `x` earlier is a latent
UnboundLocalError: the local import makes `x` function-local for the whole body,
so every use before that line raises at runtime — even when `x` is imported at
module scope and the code reads as correct.

It bit `/path`: a lazy `import shlex` added to one branch of the 600-line
`_handle_direct_shortcut` crashed the CLI on a branch 100 lines above it, with
`shlex` sitting at the top of the file the whole time. Import alone cannot see
it, so a scan stands in for the test nobody writes per function.
"""
from __future__ import annotations

import ast
import pathlib

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "silica"


def _bound_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    return [(a.asname or a.name).split(".")[0] for a in node.names]


def _offenders(tree: ast.AST) -> list[tuple[str, str, int, int]]:
    """(function, name, use_line, import_line) for every shadowed module use."""
    module_level = {
        n
        for stmt in tree.body
        if isinstance(stmt, (ast.Import, ast.ImportFrom))
        for n in _bound_names(stmt)
    }
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # First local re-import of a module-level name, by line.
        local: dict[str, int] = {}
        for node in ast.walk(fn):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for name in _bound_names(node):
                    if name in module_level:
                        local.setdefault(name, node.lineno)
        for name, import_line in local.items():
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Name)
                    and node.id == name
                    and isinstance(node.ctx, ast.Load)
                    and node.lineno < import_line
                ):
                    found.append((fn.name, name, node.lineno, import_line))
                    break
    return found


def test_no_function_local_import_shadows_an_earlier_use():
    hits = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits += [
            f"{path.relative_to(PACKAGE.parent)}:{use} — {fn}() uses {name!r} "
            f"before its own `import {name}` on line {imp}"
            for fn, name, use, imp in _offenders(tree)
        ]
    assert not hits, "UnboundLocalError waiting to happen:\n  " + "\n  ".join(hits)
