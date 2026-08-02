# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Validate already repairs some malformed ops; now it says which, and how many.

The gate reports a reject total, and a total says nothing a prompt author can
act on. The repairs it performs silently say a great deal: "nine of ten
corrections were a path rebuilt from the title" names the line of the prompt to
change, where "twelve ops rejected" names nothing. The counts are per pattern
for that reason, and they are an out-parameter so no caller has to read them.

Nothing here changes what validate accepts. It is the same repairs, counted.
"""
from silica.kernel.write.validate import MIN_WRITE_SNIPPET_CHARS, validate_operations

_PAD = " lorem" * (MIN_WRITE_SNIPPET_CHARS // 6 + 1)


def _write_op(heading: str, path: str, **extra) -> dict:
    return {"op": "write", "path": path, "heading": heading,
            "source_basename": "lez.md",
            "snippet": f"corpo di {heading}" + _PAD, **extra}


def test_a_path_rebuilt_from_the_title_is_counted(tmp_vault):
    counts: dict = {}
    ops, _ = validate_operations(
        [_write_op("Some Heading", "Corso/Some Heading.md", title="Real Concept")],
        [], "Corso", normalized_out=counts)

    assert counts == {"title_path": 1}
    # Only that this repair fired: later stages (title gate, collision
    # coercion) own the final path and are covered by their own tests.
    assert ops[0].path != "Corso/Some Heading.md"


def test_an_illegal_filename_character_is_counted(tmp_vault):
    counts: dict = {}
    ops, _ = validate_operations(
        [_write_op("Ratio", "Corso/Signal: Noise.md")], [], "Corso",
        normalized_out=counts)

    assert counts == {"path_slugify": 1}
    assert ":" not in ops[0].path


def test_a_write_rebased_into_the_write_dir_is_counted(tmp_vault, monkeypatch):
    import silica.kernel.write.validate as validate_mod

    monkeypatch.setattr(validate_mod, "active_write_dir", lambda: "Notes",
                        raising=False)
    monkeypatch.setattr("silica.kernel.vault_manifest.active_write_dir",
                        lambda: "Notes")
    counts: dict = {}
    validate_operations([_write_op("Outside", "Elsewhere/Outside.md")], [],
                        "Elsewhere", normalized_out=counts)

    assert counts.get("write_dir_rebase") == 1


def test_patterns_are_counted_apart_not_summed(tmp_vault):
    """One number cannot tell a prompt author which rule to fix."""
    counts: dict = {}
    validate_operations(
        [_write_op("A", "Corso/A.md", title="Clean Name"),
         _write_op("B", "Corso/Bad: Name.md")],
        [], "Corso", normalized_out=counts)

    assert counts == {"title_path": 1, "path_slugify": 1}


def test_repeats_of_one_pattern_accumulate(tmp_vault):
    counts: dict = {}
    validate_operations(
        [_write_op("A", "Corso/A: one.md"), _write_op("B", "Corso/B: two.md")],
        [], "Corso", normalized_out=counts)

    assert counts == {"path_slugify": 2}


def test_clean_ops_report_nothing_rather_than_zeroes(tmp_vault):
    """An empty report reads as "no repairs"; a wall of zeroes reads as noise
    and stops being read."""
    counts: dict = {}
    validate_operations([_write_op("Clean", "Corso/Clean.md")], [], "Corso",
                        normalized_out=counts)

    assert counts == {}


def test_the_counter_is_optional(tmp_vault):
    """Every existing caller passes nothing and must keep working."""
    ops, _ = validate_operations(
        [_write_op("Ratio", "Corso/Signal: Noise.md")], [], "Corso")

    assert ":" not in ops[0].path


def test_counting_does_not_change_what_validate_returns(tmp_vault):
    """The observation must not move the verdict."""
    fixture = [_write_op("Ratio", "Corso/Signal: Noise.md"),
               _write_op("Clean", "Corso/Clean.md")]
    plain_ops, plain_rej = validate_operations(list(fixture), [], "Corso")
    counted_ops, counted_rej = validate_operations(list(fixture), [], "Corso",
                                                   normalized_out={})

    assert [o.model_dump() for o in plain_ops] == [o.model_dump() for o in counted_ops]
    assert len(plain_rej) == len(counted_rej)


def test_a_second_pass_over_repaired_ops_counts_nothing(tmp_vault):
    """Idempotent: re-validating validate's own output must not report
    repairs that were already applied, or the rate drifts upward on every
    retry the steer loop makes."""
    ops, _ = validate_operations(
        [_write_op("Ratio", "Corso/Signal: Noise.md")], [], "Corso")
    counts: dict = {}
    validate_operations(ops, [], "Corso", normalized_out=counts)

    assert counts == {}
