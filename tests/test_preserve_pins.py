# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Every `loss=` and `free=` in silica/, pinned by file, function, value and
count (ADR-0093 decision 6). A new one fails here until the ADR is amended:
a declared reason switches the preservation rule off, or part of it, for its
write, so where they sit is the record's to decide, not a call site's.

Forwarding a caller's own keyword (`loss=loss`, `free=free`) declares
nothing and is not pinned.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LOSS_PINS = {
    ("silica/ui/web/server.py", "note_save", "Loss.PERSON"): 1,
    ("silica/cli.py", "_dc_verify", "Loss.PERSON"): 1,
    ("silica/tools/wrapped.py", "silica_restore", "Loss.RESTORE"): 2,
    ("silica/cli.py", "_dc_undo", "Loss.RESTORE"): 1,
    ("silica/agent/commit.py", "commit_derived", "Loss.RESTORE"): 1,
    ("silica/driver/ws_backend.py", "restore", "preserve.Loss.RESTORE"): 1,
    ("silica/sources/convert.py", "_doc_to_md", "Loss.STAGED"): 2,
    ("silica/sources/convert.py", "_convert_tabular", "Loss.STAGED"): 1,
    ("silica/sources/convert.py", "_write_leaf", "Loss.STAGED"): 1,
    ("silica/sources/registry.py", "stage", "Loss.STAGED"): 1,
    ("silica/tools/codedocs_tool.py", "silica_document", "Loss.STAGED"): 1,
    ("silica/cli.py", "_stage_envelope", "Loss.STAGED"): 1,
    ("silica/cli.py", "_promote", "Loss.STAGED"): 1,
    ("silica/ui/web/server.py", "_add_to_vault", "Loss.STAGED"): 1,   # ADR-0121 point 5
    ("silica/agent/commit.py", "commit_derived", "Loss.DERIVED"): 1,
    ("silica/kernel/write/migrate.py", "apply", "Loss.MIGRATE"): 1,
    ("silica/kernel/learn/plans.py", "edit_plan", "Loss.PLAN"): 1,
    ("silica/tools/notes.py", "silica_flag_note", "Loss.RESOLVE if clear else None"): 1,
    ("silica/router/states/finalize.py", "_write_source_leaf", "Loss.CITE"): 1,
    ("silica/kernel/link/sweep.py", "sweep_note", "Loss.UNLINK"): 1,
    ("silica/driver/fs_backend.py", "move", "preserve.Loss.RENAME"): 1,
    ("silica/tools/wrapped.py", "explicit_links", "Loss.RENAME"): 1,   # ADR-0126 point 3
}

FREE_PINS = {
    ("silica/tools/events.py", "silica_event_update", "tuple(_FIELD_TO_KEY.values())"): 1,
    ("silica/kernel/learn/plans.py", "set_revisit", "('revisit',)"): 1,
    ("silica/kernel/learn/plans.py", "clear_revisit", "('revisit',)"): 1,
    ("silica/kernel/learn/plans.py", "close_plan", "('status', 'completed')"): 2,
    ("silica/driver/fs_backend.py", "set_prop", "(name,)"): 1,
    ("silica/driver/ws_backend.py", "set_prop", "(name,)"): 1,
}

_FORWARDS = {"loss", "free", "None", "()"}


def _declared() -> tuple[Counter, Counter]:
    loss: Counter = Counter()
    free: Counter = Counter()

    class Visit(ast.NodeVisitor):
        def __init__(self, rel: str):
            self.rel, self.fn = rel, ["<module>"]

        def _def(self, node):
            self.fn.append(node.name)
            self.generic_visit(node)
            self.fn.pop()

        visit_FunctionDef = visit_AsyncFunctionDef = _def

        def visit_Call(self, node: ast.Call):
            for kw in node.keywords:
                if kw.arg in ("loss", "free"):
                    value = ast.unparse(kw.value)
                    if value not in _FORWARDS:
                        (loss if kw.arg == "loss" else free)[(self.rel, self.fn[-1], value)] += 1
            self.generic_visit(node)

    for path in sorted((ROOT / "silica").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        Visit(rel).visit(ast.parse(path.read_text(encoding="utf-8")))
    return loss, free


def test_every_declared_loss_is_pinned():
    loss, _free = _declared()
    assert dict(loss) == LOSS_PINS


def test_every_free_key_set_is_pinned():
    _loss, free = _declared()
    assert dict(free) == FREE_PINS


def test_the_counts_the_record_states():
    from silica.kernel.write.preserve import _RELAX_ALL, Loss

    assert sum(LOSS_PINS.values()) == 24  # ADR-0126: explicit_links under RENAME
    off = sum(n for (_f, _fn, v), n in LOSS_PINS.items()
              if Loss[v.rsplit(".", 1)[-1].split()[0]] in _RELAX_ALL)
    assert off == 18
    assert sum(FREE_PINS.values()) == 7


def test_a_reason_never_rides_on_an_op():
    from silica.kernel.write.ops import Op

    assert not {"loss", "free"} & set(Op.model_fields)
