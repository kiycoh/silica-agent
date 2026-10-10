# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""_table_column_mismatch (silica/kernel/link/health/lint.py) used to gate a
row on a compound condition (`s.startswith("|") or "|" in s and count>=2`)
and then immediately re-check `not s.startswith("|")` right after, so the
second disjunct's headerless-pipe-row branch could never reach the counting
logic below it — it was always discarded by the very next check. Collapsing
to the single leading-pipe test changes nothing observable; these pin that."""
from silica.kernel.link.health import lint


def test_leading_pipe_row_with_wrong_column_count_is_flagged():
    body = "| A | B |\n| --- | --- |\n| one |\n"
    counts = lint.scan(body, "Stem")
    assert counts["table-column-mismatch"] == 1


def test_headerless_pipe_row_is_never_flagged():
    """A line with 2+ pipes but no leading '|' was never actually linted —
    the compound condition's second disjunct let it past the first check,
    but the very next check discarded it before any column counting ran.
    Collapsing the condition must preserve that, not start linting it."""
    body = "A | B | C\n1 | 2\n"
    counts = lint.scan(body, "Stem")
    assert "table-column-mismatch" not in counts


def test_well_formed_table_is_not_flagged():
    body = "| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |\n"
    counts = lint.scan(body, "Stem")
    assert "table-column-mismatch" not in counts
