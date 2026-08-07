# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Counterexamples that prove a deterministic gate metric can still move.

A metric that cannot fail reports PASS regardless of the arm, and the gate reads
as a result. This harness has been bitten by the class repeatedly (`a333ce0`: the
L3 gate scored the recomposed floor and not the note; `e8ddf63`: the decompose cap
cut long notes mid-fact and never judged the tail; the PPR phase-0 kill gate, vacuous
because 3-hop reached 98% of the vault) showed the pure
form: two eval metrics matching `\\d+` against citation IDs guaranteed to contain a
letter, so both scored 1.0 on every input and two rows of its summary table were
decoration.

`_shared.assert_lexical_live` / `assert_reranker_live` already refuse a RUN that
would produce an artifact instead of a result. This refuses a run whose METRICS
would produce one, for the same reason and at the same moment: before any model
work, so a dead metric costs zero tokens.

Each entry pins a metric against cases it must score exactly. At least two cases
must disagree, or the control proves nothing: a metric stuck at 1.0 and a metric
stuck at 0.0 are both dead, and only a pair of fixtures separates a live metric
from either.

REGISTRATION RULE. Adding a deterministic metric to a gate without a control here
is the defect, not an omission. `assert_metrics_discriminate` takes the names the
runner is about to compute and refuses any it does not know, so the gap fails the
run rather than passing quietly. The rule it cannot enforce is a runner that never
names its new metric in the call at all — that one stays on whoever adds it.

Out of scope: LLM judges. A judge cannot be pinned to an expected value, and its
failure mode is different anyway — the factscore judge saturates near 1.0, which is
calibration, not a dead branch.
"""
from __future__ import annotations

from typing import Any, Callable

from evals.probe_web_gate import acquisition, bank_validity, effective_citations
from silica.sources import web_research as wr

_PAGE = "Source: https://e.example/p\nThe cat sat on the mat."


def flags_phantom_marker(body: str, bank: dict) -> bool:
    """Did the citation binder flag a marker naming no banked quote?

    The gate reports `_bind_citations`' audit line verbatim (probe_web_gate._arm
    -> "phantom_audit"); the control pins the discriminating half of it, so
    rewording the sentence does not fail a run while gutting the check would.
    """
    return bool(wr._bind_citations(body, [], bank)[2])


def _quote(url: str, text: str) -> wr._Quote:
    return wr._Quote(url=url, quote=text, why="control")


# metric name -> (fn, [((args...), expected), ...], why these inputs are the test)
CONTROLS: dict[str, tuple[Callable, list[tuple[tuple, Any]], str]] = {
    "effective_citations": (
        effective_citations,
        [
            (("A claim with [Q1] and a bare [ref].",), 0),
            (("A claim [1], another [2, 3], and [2] again.",), 3),
        ],
        "Counts distinct BOUND sources. Unbound [Qk] markers and prose brackets "
        "are not citations; three distinct numbers across two markers are three "
        "sources. Dies silently if the binder's output format ever stops being [n].",
    ),
    "bank_validity": (
        bank_validity,
        [
            (({"Q1": _quote("https://e.example/p", "The dog sat")}, [_PAGE]), 0.0),
            (({"Q1": _quote("https://e.example/p", "The cat sat")}, [_PAGE]), 1.0),
        ],
        "A quote absent from its own page must not read as verbatim. This is the "
        "guardian's sanity number: at 1.0 it is trusted and never looked at again, "
        "so it has to be shown failing on a quote the page does not contain.",
    ),
    "flags_phantom_marker": (
        flags_phantom_marker,
        [
            (("Fact [Q9].", {"Q1": _quote("https://e.example/p", "The cat sat")}), True),
            (("Fact [Q1].", {"Q1": _quote("https://e.example/p", "The cat sat")}), False),
        ],
        "A phantom-citation check whose "
        "pattern cannot match the markers the writer actually emits reports zero "
        "phantoms forever. Cite a quote id that was never banked; it must be seen.",
    ),
    "acquisition": (
        acquisition,
        [
            # Fixtures in WIRE shape: the runner feeds acquisition() the raw
            # JSON-decoded recording, so bank values are lists, not _Quote.
            (({"arm": "A", "bank": {}, "trace": {"1": "no results"}},),
             {"arm": "A", "urls_with_quotes": 0, "quotes": 0, "fetches": 0,
              "yield_per_fetch": None, "steps": 1}),
            (({"arm": "B",
               "bank": {"Q1": ["https://a.example", "one", "control"],
                        "Q2": ["https://b.example", "two", "control"]},
               "trace": {"1": "Source: https://a.example\ntext",
                         "2": "Source: https://b.example\ntext"}},),
             {"arm": "B", "urls_with_quotes": 2, "quotes": 2, "fetches": 2,
              "yield_per_fetch": 1.0, "steps": 2}),
        ],
        "The L3 primaries, read off the recording's shape. Renaming a recording "
        "key would leave every arm at zero fetches and yield None — a metric that "
        "cannot move, reported as 'no difference between the arms'.",
    ),
}


def assert_metrics_discriminate(*names: str) -> None:
    """Refuse a gate run whose metrics cannot fail. Call it in the runner beside
    the `assert_*_live` checks, before the corpus is built and before any judge
    call: a dead metric found afterwards has already cost the run."""
    unknown = [n for n in names if n not in CONTROLS]
    if unknown:
        raise SystemExit(
            f"no negative control registered for: {', '.join(sorted(unknown))}. "
            "A deterministic metric with no counterexample has never been shown to "
            "fail, so a PASS from it means nothing. Register it in "
            "evals/negative_controls.py.")

    broken: list[str] = []
    for name in names:
        fn, cases, _ = CONTROLS[name]
        if len({repr(expected) for _, expected in cases}) < 2:
            broken.append(f"{name}: its cases all expect the same value, so they "
                          "cannot tell a live metric from a constant")
            continue
        for args, expected in cases:
            got = fn(*args)
            if got != expected:
                broken.append(f"{name}{args!r} -> {got!r}, control expects {expected!r}")
    if broken:
        raise SystemExit(
            "metrics failed their own controls:\n  " + "\n  ".join(broken) +
            "\nThe gate would report a number that is an artifact of the metric, "
            "not of the arm. Fix the metric (or the control, if the contract moved).")


if __name__ == "__main__":
    assert_metrics_discriminate(*CONTROLS)
    print(f"ok: {len(CONTROLS)} metrics discriminate")
