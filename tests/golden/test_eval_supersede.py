# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Unit tests for probe_supersede — the §8.2 gate of spec-contested-bitemporal.

The rates' real magnitude is a corpus property; these pin the mechanics on
synthetic vaults: what counts as a wrong resolution, what counts as a missed
one, and that neither is reported on a corpus that cannot exercise it.
"""
from __future__ import annotations

from evals.golden import probe_supersede

DISTILLED = """---
AI: true
contested: true
contradictions:
- 'source: appunti.md'
---

# Warfarin

Il dosaggio raccomandato è 5mg/die.

> [!warning] Contradiction — from appunti.md
> Il dosaggio raccomandato è 50mg/die.
>
> Conflicts with this note. Unresolved.
"""

LEAF = "---\ndate: 2026-03-01\nsource_id: appunti.md\n---\n\nIl dosaggio è 50mg/die.\n"


def _contested_vault(tmp_path, note: str = DISTILLED, *, leaf: bool = True):
    (tmp_path / "Warfarin.md").write_text(note, encoding="utf-8")
    if leaf:
        (tmp_path / "sources").mkdir(exist_ok=True)
        (tmp_path / "sources" / "appunti.md").write_text(LEAF, encoding="utf-8")
    return tmp_path


def test_strict_dominance_over_an_open_contest_is_a_missed_resolution(tmp_path):
    """A distilled note contested by a claim whose verbatim source is on disk.

    The tiers differ, so a dominance rule has a verdict and the contest is
    sitting open for want of §6.1 — which is exactly the lever being sized.
    """
    res = probe_supersede.run(_contested_vault(tmp_path))

    assert res["contested_notes"] == 1
    assert res["missed_pairs_evaluated"] == 1
    assert res["missed_resolution_rate"] == 1.0
    assert res["missed_incoming_wins"] == 1   # grounded beats distilled
    assert res["missed_target_wins"] == 0


def test_equal_tier_contest_is_ranked_but_not_settleable(tmp_path):
    """Counted in the denominator, never in the numerator: §7.5 in the metric.

    A grounded note against a grounded incoming claim has no signal either way,
    so no rule could settle it. Reading that as a missed resolution would price
    a lever that does not exist.
    """
    grounded = DISTILLED.replace(
        "Il dosaggio raccomandato è 5mg/die.",
        "Il dosaggio raccomandato è 5mg/die.\n\n## Sources\n\n[[appunti]]",
    )
    res = probe_supersede.run(_contested_vault(tmp_path, grounded))

    assert res["missed_pairs_evaluated"] == 1
    assert res["missed_resolution_rate"] == 0.0
    assert res["missed_incoming_wins"] == 0 and res["missed_target_wins"] == 0


def test_a_user_flag_is_not_a_ranked_contest(tmp_path):
    """`flagged:` carries no rival claim — ranking it would invent a contest."""
    flagged = DISTILLED.replace(
        "- 'source: appunti.md'", "- 'flagged: dose looks stale (by user, 2026-08-11)'"
    )
    res = probe_supersede.run(_contested_vault(tmp_path, flagged, leaf=False))

    assert res["contested_notes"] == 1     # still a contested note
    assert res["missed_pairs_evaluated"] == 0
    assert "missed_resolution_rate" not in res


HUMAN_NOTE = "---\ntype: Note\n---\n\n# Warfarin\n\nDose 5mg.\n"
AGENT_NOTE = ("---\nAI: true\ntype: Note\n---\n\n# Dosaggio Warfarin\n\n"
              + "Distillata. " * 200)


def test_an_inversion_is_what_the_wrong_arm_counts():
    """The numerator's definition, tested where it is reachable.

    Through the seam it never fires — that is the §6.2 guarantee. Pinning the
    predicate separately is what keeps the metric from being unfalsifiable:
    a rate that cannot count is not a gate.
    """
    assert probe_supersede.is_inversion(winner=AGENT_NOTE, loser=HUMAN_NOTE)
    assert not probe_supersede.is_inversion(winner=HUMAN_NOTE, loser=AGENT_NOTE)
    assert not probe_supersede.is_inversion(winner=AGENT_NOTE, loser=AGENT_NOTE)


def test_the_merge_seam_never_inverts_on_a_tier_split_pair(tmp_vault):
    """The gate itself: ask the production seam, then rank what it chose.

    The pair is tier-split, so a seam that reverted to `len(body)` would pick
    the verbose agent note and this would go to 1.0 — the regression the rate
    exists to catch.
    """
    tmp_vault.note("A.md", HUMAN_NOTE)
    tmp_vault.note("B.md", AGENT_NOTE)

    res = probe_supersede.merge_verdicts([{"source": "B.md", "target": "A.md", "score": 0.9}])

    assert res["wrong_pairs_evaluated"] == 1
    assert res["wrong_tier_split_pairs"] == 1   # the gate can bite on this pair
    assert res["wrong_resolution_rate"] == 0.0


def test_a_corpus_with_no_tier_split_reports_no_rate(tmp_vault):
    """Measured on the real vault: 578 merge pairs, every one same-tier.

    The numerator cannot be reached, so the rate is 0.0 for a reason that has
    nothing to do with the code being right. Printed with a GATE mark it reads
    as protection; recorded in a baseline it freezes as a guarantee. Same rule
    as an empty denominator: say nothing rather than say "clean".
    """
    tmp_vault.note("A.md", AGENT_NOTE)
    tmp_vault.note("B.md", AGENT_NOTE + "altro corpo\n")

    res = probe_supersede.merge_verdicts([{"source": "B.md", "target": "A.md", "score": 0.9}])

    assert res["wrong_pairs_evaluated"] == 1
    assert res["wrong_tier_split_pairs"] == 0
    assert "wrong_resolution_rate" not in res


def test_the_runner_carries_the_metrics_and_gates_the_right_ones(tmp_path):
    """Wiring: a probe that never reaches `metrics` is a probe nobody runs."""
    import silica.driver
    from evals.golden import runner

    _contested_vault(tmp_path)
    try:
        doc = runner.collect(tmp_path, tier="cheap")
    finally:
        silica.driver._driver = None

    assert doc["metrics"]["supersede.contested_notes"] == 1
    assert doc["metrics"]["supersede.missed_resolution_rate"] == 1.0
    assert "supersede.wrong_resolution_rate" not in doc["metrics"]  # no embed leg

    # An inversion is gated as a COUNT, not a rate: a full §6.2 revert measured
    # 0.0209 against a 2pp tolerance, so the rate cleared the gate by 0.09pp and
    # a partial revert would have passed. Any inversion at all fails instead.
    assert "supersede.wrong_merges" in runner.GATED_EXACT_ZERO
    inverted = {**doc, "metrics": {**doc["metrics"], "supersede.wrong_merges": 1}}
    assert runner.compare(doc, inverted)
    assert not runner.compare(doc, doc)

    # The missed rate stays a rise gate, self-arming off the baseline.
    assert "supersede.missed_resolution_rate" in runner.GATED_RISE_2PP
    worse = {**doc, "metrics": {**doc["metrics"], "supersede.missed_resolution_rate": 1.0}}
    better = {**doc, "metrics": {**doc["metrics"], "supersede.missed_resolution_rate": 0.5}}
    assert runner.compare(better, worse)      # rose past tolerance -> fails
    assert not runner.compare(worse, better)  # fell -> passes


def test_the_fixture_corpus_prices_the_6_1_bis_variant():
    """The labeled corpus, scored against the rule §6.1-bis would apply.

    Labels are editorial (`fixture_expect` + `fixture_why` on each note), never
    derived from the tier formula — a corpus labeled by the rule under test
    measures nothing but its own arithmetic. Two of the eight contests are
    deliberate traps where tier dominance and the right answer disagree.
    """
    bare = probe_supersede.score_fixture(recency_guard=False)

    assert bare["fixture_contests"] == 8
    assert bare["fixture_ranked"] == 7         # the `flagged:` one has no rival claim
    assert bare["fixture_settleable"] == 4     # labeled target or incoming
    assert bare["fixture_suppressions"] == 4   # contests bare dominance acts on
    assert bare["fixture_wrong"] == 2          # ...and gets wrong
    assert bare["fixture_missed"] == 1
    assert bare["fixture_wrong_rate"] == 0.5   # a coin flip when it acts
    assert bare["fixture_missed_rate"] == 0.25


def test_the_recency_veto_buys_precision_with_recall():
    """The trade the veto makes, pinned so a later tweak has to face it.

    Both wrong suppressions disappear because both were a stale note meeting a
    fresher source. The cost is one more settleable contest left visible, which
    is the cheaper error: a visible contest can be resolved later, a wrongly
    buried claim is found by accident.
    """
    guarded = probe_supersede.score_fixture()

    assert guarded["fixture_suppressions"] == 2
    assert guarded["fixture_wrong"] == 0
    assert guarded["fixture_wrong_rate"] == 0.0     # precision 1.00, was 0.50
    assert guarded["fixture_missed"] == 2
    assert guarded["fixture_missed_rate"] == 0.5    # recall 0.50, was 0.75


def test_empty_vault_reports_nothing_to_gate(tmp_path):
    """A vault with no contests and no pairs must omit the rates, not print 0.0.

    A rate over an empty denominator reads as "clean" on the table and freezes
    into the baseline as a guarantee nothing measured.
    """
    res = probe_supersede.run(tmp_path)
    assert res["contested_notes"] == 0
    assert res["missed_pairs_evaluated"] == 0
    assert "missed_resolution_rate" not in res
    assert "wrong_resolution_rate" not in res
