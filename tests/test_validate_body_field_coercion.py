# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Hot-path audit A17/A18/A21 — op-type coercion must not decouple the body field.

`_execute_write` reads `op.snippet`; `_execute_overwrite` reads `op.content` and
raises on `content=None`. Every coercion that flips `op.op` between write/patch
(body in snippet) and overwrite (body in content) must carry the body across, or
it silently persists an empty note / crashes on commit.

  A17: overwrite→write on a missing path must copy content→snippet.
  A18: dedup coercing a richer write into overwrite must copy snippet→content.
  A21: title-key coercion can retarget two writes onto one note; the post-coercion
       dedup must keep exactly one op per path.
"""
from silica.kernel.ops import OpType
from silica.kernel.validate import MIN_WRITE_SNIPPET_CHARS, validate_operations

_PAD = " lorem" * (MIN_WRITE_SNIPPET_CHARS // 6 + 2)


def test_a17_overwrite_to_missing_path_carries_content_to_snippet(tmp_vault):
    """overwrite of a non-existent note degrades to write; the body lives in
    `content`, but the write path reads `snippet` — it must be carried over."""
    body = "the real overwrite body" + _PAD
    ops = [{
        "op": "overwrite", "path": "Corso/BrandNew.md",
        "heading": "BrandNew", "source_basename": "lez.md",
        "content": body,
    }]
    validated, rejected = validate_operations(ops, [], "Corso")

    assert rejected == []
    op = next(o for o in validated if o.path == "Corso/BrandNew.md")
    assert op.op == OpType.write, "overwrite of missing path must degrade to write"
    assert op.snippet == body, "content must be carried into snippet or the write is empty"


def test_a18_dedup_coercion_preserves_the_body(tmp_vault):
    """A path-dedup group containing an overwrite and a richer write keeps exactly
    one op, and it must carry a non-empty body (the audit's A18 content=None crash
    is unreachable — the losing overwrite is skipped before the type check — but the
    body-carry guards that invariant regardless of which type wins)."""
    tmp_vault.note("Corso/Target.md", "# Target\n\nold body")

    richer_body = "the new richer body wins the dedup" + _PAD
    ops = [
        {"op": "overwrite", "path": "Corso/Target.md", "heading": "Target",
         "source_basename": "lez.md", "content": "short"},
        {"op": "write", "path": "Corso/Target.md", "heading": "Target",
         "source_basename": "lez.md", "snippet": richer_body},
    ]
    validated, rejected = validate_operations(ops, [], "Corso")

    surviving = [o for o in validated if o.path == "Corso/Target.md"]
    assert len(surviving) == 1, "exactly one op survives the path dedup"
    op = surviving[0]
    assert (op.snippet or op.content), "the survivor must not lose its body to a type flip"
    assert richer_body in (op.snippet or "") or richer_body == op.content


def test_a21_title_key_coercion_dedups_two_writes_onto_one_note(tmp_vault):
    """Two distinct-path writes whose title-keys both collide with one existing
    note are coerced to patch on that note. The step-1 path dedup ran earlier and
    cannot see this — the post-coercion dedup must keep exactly one."""
    tmp_vault.note("Corso/Machine Learning.md", "# ML\n\nbody")

    ops = [
        {"op": "write", "path": "Corso/Machine Learning (9 CFU).md",
         "heading": "Machine Learning (9 CFU)", "source_basename": "lez.md",
         "snippet": "variant A" + _PAD},
        {"op": "write", "path": "Corso/Machine Learning [2024].md",
         "heading": "Machine Learning [2024]", "source_basename": "lez.md",
         "snippet": "variant B is the longer one" + _PAD},
    ]
    validated, rejected = validate_operations(ops, [], "Corso")

    targeting = [o for o in validated if o.path == "Corso/Machine Learning.md"]
    assert len(targeting) == 1, "two writes must not both retarget onto one note (double-append)"
    assert targeting[0].op == OpType.patch
