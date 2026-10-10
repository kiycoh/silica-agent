# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Freeze whole-batch behavior at ecd1082 before decomposing the validator.

The fixture records observed behavior, including awkward ordering effects;
it is not a claim that every current verdict is desirable. See its README.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from silica.config import CONFIG
from silica.kernel.vault_manifest import reset_manifest_cache
from silica.kernel.write.ops_io import parse_ops
from silica.kernel.write.validate import validate_operations

FIXTURES = Path(__file__).parent / "fixtures" / "validate_characterization"
CASES = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
@pytest.mark.parametrize("input_kind", ["dict", "model"])
def test_validate_batch_characterization(case, input_kind, tmp_vault, tmp_path, monkeypatch):
    # Pin the inputs that otherwise come from the developer's .env or vault.
    for name in ("SILICA_MIN_WRITE_SNIPPET_CHARS", "SILICA_EXTRACTIVE_MIN_SNIPPET_CHARS",
                 "SILICA_EXTRACTIVE_ENFORCE", "SILICA_META_SHAPE_CHECK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(CONFIG, "cooccurrence_lang", "auto")
    for path, content in case["files"].items():
        tmp_vault.note(path, content)
    reset_manifest_cache()
    vault = Path(CONFIG.vault_path)
    # Keep relative-path comparisons independent of the checkout location.
    monkeypatch.chdir(vault)
    files_before = {str(p.relative_to(vault)): p.read_bytes()
                    for p in vault.rglob("*") if p.is_file()}
    ops = deepcopy(case["ops"])
    if input_kind == "model":
        ops = parse_ops(ops)
    payloads = deepcopy(case["payloads"])
    inputs_before = deepcopy((ops, payloads))
    diagnostics = {"cleared_parents": [], "cleared_links": [], "ungrounded": [],
                   "grounding": [], "normalized": {}}
    accepted, rejected = validate_operations(
        ops, payloads, **case["options"],
        **{f"{name}_out": value for name, value in diagnostics.items()},
    )
    actual = {
        "accepted": [op.model_dump(mode="json") for op in accepted],
        "rejected": [op.model_dump(mode="json") for op in rejected],
        **diagnostics,
    }
    assert (ops, payloads) == inputs_before, "validation mutated its caller's input"
    assert {str(p.relative_to(vault)): p.read_bytes()
            for p in vault.rglob("*") if p.is_file()} == files_before
    # Keep the full observation on a mismatch for review; tests never update
    # the oracle. In particular, do not sort away operation/diagnostic order.
    if actual != EXPECTED.get(case["id"]):
        observed = tmp_path / "observed.json"
        observed.write_text(json.dumps({case["id"]: actual}, ensure_ascii=False, indent=2)
                            + "\n", encoding="utf-8")
        assert actual == EXPECTED.get(case["id"]), f"Full observation: {observed}"
