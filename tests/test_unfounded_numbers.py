# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The numbers rule of ADR-0095: which numbers in a chat answer no evidence holds."""
from __future__ import annotations

from silica.kernel.write.provenance import number_tokens, unfounded_numbers


def flag(answer: str, *evidence: str) -> list[str]:
    tokens: set[str] = set()
    for text in evidence:
        tokens |= number_tokens(text)
    return unfounded_numbers(answer, tokens)


def test_a_changed_count_is_flagged_against_the_read_one():
    # The 2026-09-22 study-plan error: the mail said 242 columns.
    assert flag("The dataset has 243+ variables.", "the sensor table holds 242 columns") == ["243"]
    assert flag("The dataset has 242 variables.", "the sensor table holds 242 columns") == []


def test_distinct_numbers_in_order_of_appearance():
    answer = "Use 243 rows, then 1.96, then 243 again and 17."
    assert flag(answer, "nothing here") == ["243", "1.96", "17"]


def test_a_single_digit_is_never_checked():
    assert flag("There are 3 notes and 7 links.", "") == []
    # two digits across a decimal point are two digits
    assert flag("a ratio of 1.5", "") == ["1.5"]


def test_a_hyphen_after_a_digit_or_letter_is_a_range_not_a_sign():
    # "1-10" yields 10, never -10, so evidence saying 10 grounds it
    assert flag("pick between 1-10 items", "a scale of 10") == []
    assert flag("pick between 1-10 items", "") == ["10"]
    assert flag("pages 12-15", "pages 12 to 15") == []
    # a hyphen with nothing before it is a sign
    assert flag("the minimum is -12", "the minimum is 12") == ["-12"]
    assert flag("the minimum is -12", "minimum: -12") == []


def test_digit_grouping_folds_on_both_sides():
    assert flag("a sample of 2,583 answers", "n = 2583") == []
    assert flag("a sample of 2583 answers", "n = 2,583") == []
    # an Italian thousands point
    assert flag("un campione di 1.067 persone", "n = 1067") == []
    # groups that are not threes are not grouping
    assert flag("a sample of 25,83", "n = 2583") == ["25,83"]


def test_a_decimal_comma_reads_as_a_point():
    assert flag("z vale 1,96", "z = 1.96") == []
    assert flag("z is 1.96", "z vale 1,96") == []


def test_identifiers_are_neither_checked_nor_evidence():
    # a digit run glued to a letter before it: a version, a variable, a hash
    assert flag("the v2 model and x12", "") == []
    assert flag("there are 12 of them", "column x12") == ["12"]
    assert flag("commit a3f9 changed it", "") == []


def test_code_links_and_urls_are_not_checked():
    answer = (
        "See [[Lezione 32]] and ![[figure 45.png]].\n"
        "Run `head -n 50 file.csv` first.\n"
        "```python\nx = 9999\n```\n"
        "```mermaid\ngraph TD; A[17] --> B[42]\n```\n"
        "Source: https://example.org/paper/2401.12345\n"
        "[the paper](https://example.org/v/77)\n"
    )
    assert flag(answer, "") == []


def test_leading_list_and_heading_markers_are_layout():
    answer = "12. the twelfth step\n## Plan\n- 3 items\n10) the tenth\n"
    assert flag(answer, "") == []
    # the marker only: a number after it is still a claim
    assert flag("12. it costs 450 euro", "") == ["450"]


def test_numbers_in_math_are_checked():
    answer = "The bound is $z_{0.025} \\approx 1.96$ for n = 1067."
    assert flag(answer, "a sample of 1067 people") == ["0.025", "1.96"]


def test_the_seed_date_line_grounds_the_year_and_today():
    seed = "Today is Sunday, 2026-10-11."
    assert flag("As of 2026, on day 11 of month 10", seed) == []
    assert flag("As of 2025", seed) == ["2025"]


def test_a_number_ends_before_a_sentence_point():
    assert flag("It holds 242.", "242 columns") == []


def test_number_tokens_carries_every_spelling():
    toks = number_tokens("n = 1,067 and z = 1,96 in v2")
    assert {"1,067", "1067", "1.067", "1,96", "1.96"} <= toks
    assert "2" not in toks
