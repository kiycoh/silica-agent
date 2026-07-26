# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Keeps the span instrument honest: the buckets, the two harness guards, the parse.

The gate itself needs a real vault and a model; these cover the places where a
silent bug would hand back a plausible-looking wrong verdict — an invented span
counted as verified, a model declining its way to a fake 1.0, boilerplate spans
that verify against every note, a citation to a note nobody read.
"""
from __future__ import annotations

from pathlib import Path

from evals.probe_explain_spans import (
    ATTRIBUTION_CLAUSE,
    EXPOSE_SYSTEM,
    anchor,
    classify,
    gates,
    load_bodies,
    normalize,
    resolve,
)

BODY_A = "Silica keeps the vault on the filesystem. Every write goes through the FSM.\n"
BODY_B = "The co-occurrence index is embedder-free and deterministic.\n"
PACK = [{"key": "a", "title": "Note A", "body": BODY_A},
        {"key": "b", "title": "Note B", "body": BODY_B}]
BODIES = {**{k: PACK[0] for k in ("a", "a.md", "note a")},
          **{k: PACK[1] for k in ("b", "b.md", "note b")}}


def _rows(*specs: tuple[str, str, str]) -> list[dict]:
    return [{"text": "claim", "note": n, "span": s, "reason": r} for n, s, r in specs]


def test_a_span_that_is_not_in_the_note_is_invented_not_verified():
    """The one thing this instrument exists to catch. A near-miss (one word
    changed) must fail: the check is a substring, not a resemblance."""
    out = classify(_rows(("Note A", "Every write goes through the state machine.", "")),
                   PACK, BODIES)
    assert [c["bucket"] for c in out] == ["invented"]


def test_a_verbatim_span_verifies():
    out = classify(_rows(("Note A", "Every write goes through the FSM.", "")), PACK, BODIES)
    assert out[0]["bucket"] == "verified"
    assert out[0]["occurrences"] == 1
    assert not out[0]["cross_note"]


def test_an_empty_span_with_a_reason_is_declined_not_a_failure():
    """The confound the probe was built to separate: a claim summarising several
    sentences has no single verbatim span and nothing is wrong."""
    out = classify(_rows(("Note A", "", "synthesis")), PACK, BODIES)
    assert out[0]["bucket"] == "declined"
    g = gates(out)
    assert g["spans_claimed"] == 0, "a decline is not a claimed span"


def test_a_decline_with_no_reason_is_marked_unstated():
    out = classify(_rows(("Note A", "  ", "handwave")), PACK, BODIES)
    assert out[0]["bucket"] == "declined" and out[0]["reason"] == "unstated"


def test_a_decline_that_names_no_note_is_still_a_decline():
    """The bucketing bug of the first n=20 run: sending these to `uncited`
    zeroed the synthesis count and moved grounded_rate across 0.85 for a
    bucketing reason rather than a measured one."""
    out = classify(_rows(("", "", "synthesis"), ("", "", "general")), PACK, BODIES)
    assert [c["bucket"] for c in out] == ["declined", "declined"]
    g = gates(out)
    assert g["decline_reasons"]["synthesis"] == 1
    assert g["grounded_rate"] == 0.0, "one general decline stays in the denominator"


def test_citing_a_note_nobody_read_and_citing_nothing_are_separate_buckets():
    """Neither can be span-checked, and folding them into either column would
    hide them; they are different defects (a fabricated wikilink against an
    uncited claim, which today's /explain contract forbids outright)."""
    out = classify(_rows(("Note Z", "anything", ""), ("", "anything", "")), PACK, BODIES)
    assert [c["bucket"] for c in out] == ["unknown_note", "uncited"]
    assert gates(out)["spans_claimed"] == 0


def test_a_note_that_exists_but_was_not_retrieved_is_still_unknown():
    """resolve() sees the whole vault; the pack is what was read. A span checked
    against a note the exposition never saw would verify by luck."""
    out = classify(_rows(("Note B", "The co-occurrence index is embedder-free", "")),
                   [PACK[0]], BODIES)
    assert out[0]["bucket"] == "unknown_note"


def test_boilerplate_that_verifies_against_two_notes_trips_the_second_guard():
    """H2. A span occurring verbatim in another note of the same pack is
    verbatim without being evidence, and it inflates the primary ratio."""
    shared = "## Related\n"
    pack = [{"key": "a", "title": "A", "body": BODY_A + shared},
            {"key": "b", "title": "B", "body": BODY_B + shared}]
    bodies = {"a": pack[0], "b": pack[1]}
    out = classify(_rows(("a", shared, ""), ("b", shared, "")), pack, bodies)
    assert all(c["bucket"] == "verified" for c in out)
    assert all(c["cross_note"] for c in out)
    g = gates(out)
    assert g["cross_note_rate"] == 1.0
    assert not g["H2_spans_discriminate"]
    assert g["verdict"] == "HARNESS", "must not read as a verdict on lever C"


def test_declining_its_way_to_a_perfect_ratio_trips_the_first_guard():
    """H1. Nine declines and one good span is ratio 1.0, which would kill lever C
    for the wrong reason."""
    rows = _rows(*([("Note A", "", "synthesis")] * 9
                   + [("Note A", "Every write goes through the FSM.", "")]))
    g = gates(classify(rows, PACK, BODIES))
    assert g["verified_ratio"] == 1.0
    assert not g["H1_not_declining_out"]
    assert g["verdict"] == "HARNESS"


def test_the_two_thresholds_read_as_pre_registered():
    good = ("Note A", "Every write goes through the FSM.", "")
    bad = ("Note A", "not in the note at all", "")
    # 8/10 = 0.80 → below 0.85
    assert gates(classify(_rows(*([good] * 8 + [bad] * 2)), PACK, BODIES))[
        "verdict"] == "C JUSTIFIED"
    # 19/20 = 0.95 → not above 0.95, so not a kill
    assert gates(classify(_rows(*([good] * 19 + [bad])), PACK, BODIES))[
        "verdict"] == "INCONCLUSIVE"
    # 20/20
    assert gates(classify(_rows(*([good] * 20)), PACK, BODIES))["verdict"] == "C DEAD"


def test_grounded_rate_drops_synthesis_declines_but_keeps_the_others():
    """The secondary. `synthesis` is legitimate and leaves the denominator;
    `absent` and `general` are unanchored claims and stay in it."""
    rows = _rows(("Note A", "Every write goes through the FSM.", ""),
                 ("Note A", "", "synthesis"),
                 ("Note A", "", "absent"),
                 ("Note A", "", "general"))
    g = gates(classify(rows, PACK, BODIES))
    assert g["verified_ratio"] == 1.0, "one span offered, one verified"
    assert g["grounded_rate"] == 1 / 3, "the absent and general claims count against it"


# --- the comparison form (the smoke found this one) -------------------------

def test_a_span_copied_without_the_markup_still_verifies():
    """The defect the n=3 smoke exposed: the model drops `**` and the blockquote
    `> ` when it copies, and strict `in` called that an invented citation."""
    pack = [{"key": "a", "title": "A",
             "body": '> Il modello cerca di **imparare il "confine"** che racchiude i dati.\n'}]
    bodies = {"a": pack[0]}
    out = classify(
        _rows(("a", 'Il modello cerca di imparare il "confine" che racchiude i dati.', "")),
        pack, bodies)
    assert out[0]["bucket"] == "verified"
    assert not out[0]["strict"], "and the strict ratio must record that it did not match"


def test_normalising_does_not_let_a_different_sentence_through():
    """The loosening removes non-content characters only. If it could bridge a
    paraphrase, the whole instrument would be worthless."""
    out = classify(_rows(("Note A", "Every write goes through the state machine.", "")),
                   PACK, BODIES)
    assert out[0]["bucket"] == "invented"
    assert normalize("**a** _b_ `c`") == "a b c"
    assert normalize("- item\n> quoted\n## head") == "item quoted head"
    assert normalize("see [[Target|the alias]] here") == "see the alias here"


def test_a_span_made_only_of_markup_never_verifies():
    """It normalises to "", and "" is a substring of every note."""
    out = classify(_rows(("Note A", "** **", "")), PACK, BODIES)
    assert out[0]["bucket"] == "invented"


def test_no_claims_at_all_is_no_data_not_a_kill():
    g = gates([])
    assert g["verdict"] == "NO DATA" and g["verified_ratio"] == 0.0


def test_a_wikilink_alias_and_a_path_resolve_to_the_same_note():
    for cited in ("[[Note A]]", "Note A", "note a", "[[Note A|the A note]]", "a.md", "a"):
        assert resolve(cited, BODIES) is PACK[0], cited
    assert resolve("", BODIES) is None
    assert resolve("[[]]", BODIES) is None


def test_frontmatter_is_stripped_before_a_span_can_be_verified_against_it(tmp_path: Path):
    """A span verified against `tags:` is not evidence, and templated
    frontmatter is the text most likely to repeat across every note."""
    (tmp_path / "n.md").write_text(
        "---\ntags: [alpha]\n---\nThe body says one thing.\n", encoding="utf-8")
    bodies = load_bodies(tmp_path)
    assert "tags" not in bodies["n"]["body"]
    assert bodies["n"]["body"].strip() == "The body says one thing."


def test_the_probe_contract_tracks_the_shipped_explain_prompt():
    """The probe holds a paraphrase of `/explain`'s contract, so it can drift out
    of date and measure a prompt nobody ships. This fails the moment the product
    clause changes and the copy does not follow."""
    from silica.cli import _expand_workflow_shortcut

    live = _expand_workflow_shortcut('/explain "anything"')
    assert ATTRIBUTION_CLAUSE in live, "the shipped prompt lost the clause"
    assert ATTRIBUTION_CLAUSE in EXPOSE_SYSTEM, "the probe copy did not follow"


def test_an_unparseable_anchor_reply_returns_no_claims(monkeypatch):
    """Minting a row per claim with an empty span would push H1 towards a
    harness failure and look like a finding."""
    monkeypatch.setattr("silica.agent.llm.call_llm",
                        lambda **kw: type("R", (), {"text": "not json at all"})())
    assert anchor("some explanation", PACK, "m") == []
