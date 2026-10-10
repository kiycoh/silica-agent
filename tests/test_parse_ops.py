# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""parse_ops is the one structural gate, for dicts and for models alike.

An Op is mutable and nothing re-checks it after construction, so a model
whose path was blanked downstream used to pass parse_ops untouched and crash
validate_operations at Rejection(), which re-validates the op it wraps
(2026-09-19). The validator's own "Missing 'path'" guards were the dead
second copy of this rule.
"""
from __future__ import annotations

import pytest

from silica.kernel.write.ops import Op, OpType
from silica.kernel.write.ops_io import parse_ops


def _valid(kind: OpType) -> Op:
    body = {"content": "x"} if kind is OpType.overwrite else {"snippet": "x"}
    return Op(op=kind, heading="H", source_basename="lesson.md", path="Course/Note.md", **body)


@pytest.mark.parametrize("kind", [OpType.write, OpType.patch, OpType.overwrite])
def test_a_model_blanked_after_construction_is_salvaged_as_skip(kind):
    op = _valid(kind)
    op.path = None

    [out] = parse_ops([op])

    assert out.op is OpType.skip
    assert out.heading == "H" and out.source_basename == "lesson.md" and out.path is None
    assert "salvaged as skip" in (out.reason or "")


@pytest.mark.parametrize("kind", [OpType.write, OpType.patch, OpType.overwrite])
def test_a_valid_model_comes_back_equal(kind):
    op = _valid(kind)
    [out] = parse_ops([op])
    assert out == op
