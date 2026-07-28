"""Tests for reliability tiers and the conditioned merge (spec §5-§6, Fasi C+D).

The tier is ordinal and derived from note text alone, so both sides of a
comparison are always ranked on the same information. The merge target follows
the tier, with length only breaking a tie inside one, which is what stops the
verbose agent note from beating the terse hand-written one.
"""
from __future__ import annotations

from silica.kernel.write import frontmatter
from silica.kernel.write.contested import (
    TIER_DISTILLED,
    TIER_GROUNDED,
    TIER_HUMAN,
    mark_superseded_by,
    merge_rank,
    reliability_tier,
)

HUMAN = "---\ntags:\n  - farmacologia\n---\n\n# X\n\nScritta a mano.\n"
AGENT = "---\ntags:\n  - farmacologia\nAI: true\n---\n\n# X\n\nDistillata.\n"
GROUNDED = AGENT.rstrip() + "\n\n## Sources\n[[appunti-2026]]\n"


# --- reliability_tier --------------------------------------------------------

def test_human_note_outranks_agent_note():
    assert reliability_tier(HUMAN) == TIER_HUMAN
    assert reliability_tier(AGENT) == TIER_DISTILLED
    assert reliability_tier(HUMAN) > reliability_tier(AGENT)


def test_explicit_ai_false_is_human():
    assert reliability_tier(AGENT.replace("AI: true", "AI: false")) == TIER_HUMAN


def test_note_without_frontmatter_is_human():
    """Every agent write stamps a frontmatter block, so a bare file is a human's."""
    assert reliability_tier("# Appunti\n\nScritto di corsa.\n") == TIER_HUMAN


def test_source_link_promotes_an_agent_note_to_grounded():
    assert reliability_tier(GROUNDED) == TIER_GROUNDED
    assert reliability_tier(GROUNDED) > reliability_tier(AGENT)


def test_has_source_leaf_override_for_a_claim_that_is_not_a_note_yet():
    assert reliability_tier(AGENT, has_source_leaf=True) == TIER_GROUNDED
    assert reliability_tier(GROUNDED, has_source_leaf=False) == TIER_DISTILLED


def test_broken_yaml_ranks_lowest():
    """A parse accident must never win a contest."""
    assert reliability_tier("---\ntags: [unclosed\n---\n\n# X\n") == TIER_DISTILLED


# --- merge_rank --------------------------------------------------------------

def test_terse_human_note_beats_verbose_agent_note():
    """The pathology the bare len(body) heuristic had, stated as a test."""
    terse_human = HUMAN
    verbose_agent = AGENT.replace("Distillata.", "Distillata. " + "prosa " * 500)
    assert len(verbose_agent) > len(terse_human)          # length says agent
    assert merge_rank(terse_human) > merge_rank(verbose_agent)  # tier says human


def test_length_still_breaks_a_tie_within_a_tier():
    short, long = AGENT, AGENT.replace("Distillata.", "Distillata molto piu' a lungo.")
    assert reliability_tier(short) == reliability_tier(long)
    assert merge_rank(long) > merge_rank(short)


# --- mark_superseded_by ------------------------------------------------------

def test_mark_superseded_by_sets_a_wikilink():
    out = mark_superseded_by(AGENT, "Farmacologia/Dosaggio Warfarin.md")
    data, _, body = frontmatter.split(out)
    assert data["superseded_by"] == "[[Dosaggio Warfarin]]"
    assert "Distillata." in body  # content is kept, not replaced


def test_mark_superseded_by_is_idempotent():
    once = mark_superseded_by(AGENT, "Dosaggio Warfarin.md")
    assert mark_superseded_by(once, "Dosaggio Warfarin.md") == once


def test_mark_superseded_by_leaves_broken_yaml_alone():
    broken = "---\ntags: [unclosed\n---\n\n# X\n"
    assert mark_superseded_by(broken, "Winner.md") == broken


# --- the pair seam -----------------------------------------------------------

def test_pairs_to_items_targets_the_human_note(tmp_vault):
    """End to end at the seam: the merge goes INTO the human note."""
    from silica.tools.runners import _pairs_to_items

    tmp_vault.note("A.md", HUMAN)
    tmp_vault.note("B.md", AGENT.replace("Distillata.", "Distillata. " + "prosa " * 500))

    items = _pairs_to_items([{"source": "B.md", "target": "A.md", "score": 0.9}])
    assert len(items) == 1
    assert items[0].target_path == "A.md"          # human note is the target
    assert items[0].context["loser_path"] == "B.md"


def test_pairs_to_items_falls_back_to_length_within_a_tier(tmp_vault):
    from silica.tools.runners import _pairs_to_items

    tmp_vault.note("A.md", AGENT)
    tmp_vault.note("B.md", AGENT.replace("Distillata.", "Distillata. " + "prosa " * 500))

    items = _pairs_to_items([{"source": "A.md", "target": "B.md", "score": 0.9}])
    assert items[0].target_path == "B.md"          # the longer one, tier being equal
    assert items[0].context["loser_path"] == "A.md"


def test_merge_marks_the_loser(tmp_vault):
    """The absorbed note keeps its content and gains a pointer to the winner."""
    from unittest.mock import patch

    from silica.capabilities.dedup import DedupDecision, run_dedup
    from silica.config import SilicaConfig
    from silica.kernel.workqueue import WorkItem

    tmp_vault.note("Farmacologia/Farmacologia.md", "# Farmacologia\n")
    winner = tmp_vault.note("Farmacologia/Warfarin.md", HUMAN)
    loser = tmp_vault.note("Farmacologia/Dosaggio.md", AGENT)

    item = WorkItem(
        kind="dedup",
        target_path="Farmacologia/Warfarin.md",
        context={
            "concept": "Dosaggio", "excerpt": "Distillata.",
            "candidate": "Warfarin", "hub": "Farmacologia",
            "inbox_file": "Farmacologia/Dosaggio.md",
            "loser_path": "Farmacologia/Dosaggio.md",
        },
        reason="dedup score=0.91",
    )
    decision = DedupDecision(verdict="duplicate", rationale="same claim",
                             addition="Dettaglio nuovo dal duplicato.")
    with patch("silica.capabilities.dedup._decide_dedup", return_value=decision):
        res = run_dedup(item, SilicaConfig())

    assert res["status"] == "committed", res
    loser_content = tmp_vault.read(loser)
    data, _, body = frontmatter.split(loser_content)
    assert data["superseded_by"] == "[[Warfarin]]"
    assert "Distillata." in body            # the loser is marked, never emptied
    assert "Dettaglio nuovo" in tmp_vault.read(winner)


def test_fsm_dedup_never_marks_a_source_document(tmp_vault):
    """The FSM's 'loser' is an incoming concept, not a note: no loser_path, no mark."""
    from unittest.mock import patch

    from silica.capabilities.dedup import DedupDecision, run_dedup
    from silica.config import SilicaConfig
    from silica.kernel.workqueue import WorkItem

    tmp_vault.note("Farmacologia/Farmacologia.md", "# Farmacologia\n")
    tmp_vault.note("Farmacologia/Warfarin.md", HUMAN)
    inbox = tmp_vault.note("Inbox/appunti.md", "# Appunti\n\nTesto grezzo.\n")

    item = WorkItem(
        kind="dedup",
        target_path="Farmacologia/Warfarin.md",
        context={"concept": "Dosaggio", "excerpt": "Testo grezzo.",
                 "candidate": "Warfarin", "hub": "Farmacologia",
                 "inbox_file": "Inbox/appunti.md"},
        reason="borderline_similarity score=0.78",
    )
    decision = DedupDecision(verdict="duplicate", rationale="same claim",
                             addition="Dettaglio nuovo.")
    with patch("silica.capabilities.dedup._decide_dedup", return_value=decision):
        res = run_dedup(item, SilicaConfig())

    assert res["status"] == "committed", res
    assert "superseded_by" not in tmp_vault.read(inbox)
