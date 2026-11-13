"""ADR-0175: the review queue holds judgments, not candidates.

/curate asks the worker for a verdict alone on the pairs at or above the high
bar and on the pairs that share their wording; only a "duplicate" becomes a
proposal. A verdict is remembered per pair and per the bytes of both notes, so
the next /curate asks again only about a pair whose note changed. The
borderline band is a link at most, and `/review apply all` never spends the
model.
"""
from __future__ import annotations

import pytest

from silica.kernel import review as queue
from silica.kernel.report.curator import CurationItem, CurationPlan, compose_curation_plan
from silica.kernel.report.graph_report import VaultReport
from silica.kernel.report.graph_report.models import DuplicatePair


def _report(**kw) -> VaultReport:
    r = VaultReport(generated_at="2026-11-13T00:00:00Z", scope="", totals={}, god_nodes=[],
                    bridges=[], orphans=[], dangling=[], clusters=[])
    for k, v in kw.items():
        setattr(r, k, v)
    return r


@pytest.fixture
def kinds(monkeypatch):
    import importlib

    import silica.tools.review as pkg
    pkg.load()
    monkeypatch.setattr(queue, "KINDS", {})
    for name in ("aliases", "curate", "deferred", "organize", "stale"):
        importlib.reload(importlib.import_module(f"silica.tools.review.{name}"))
    return pkg.load()


@pytest.fixture
def judge(monkeypatch):
    """The worker's verdict, by the pair's two names: no model is called."""
    from silica.capabilities.dedup import DedupDecision

    said: dict[frozenset, tuple[str, str, str]] = {}
    calls: list[tuple[str, str]] = []

    def fake(item, config):
        job = item.context
        calls.append((job.concept, job.candidate))
        verdict, why, claim = said.get(frozenset((job.concept, job.candidate)), ("distinct", "different", ""))
        return DedupDecision(verdict=verdict, rationale=why, addition=claim)

    monkeypatch.setattr("silica.capabilities.judge_pair", fake)
    return said, calls


def _pair(a: str, b: str, score: float = 0.9) -> CurationItem:
    return CurationItem("dedup", a, b, score, f"{round(score * 100)}% similar")


def test_only_the_high_band_and_same_wording_are_judged_and_the_borderline_is_a_link_at_most():
    """The borderline band is "related but distinct": it is no duplicate, and
    becomes a link candidate, which the queue offers only where a note names
    the other (ADR-0165 point 6)."""
    report = _report(
        confirmed_duplicate_pairs=[DuplicatePair("A.md", "B.md", 0.9)],
        duplicate_pairs=[DuplicatePair("C", "D", 0.75), DuplicatePair("E.md", "F.md", 0.7, cosine=False)])
    plan = compose_curation_plan(report)
    assert [(i.target, i.partner) for i in plan.by_kind("dedup")] == [("A.md", "B.md"), ("E.md", "F.md")]
    (link,) = plan.by_kind("autolink")
    assert (link.target, link.partner, link.reason) == ("C", "D", "75% similar")


def test_only_a_duplicate_verdict_is_offered_and_every_verdict_is_remembered(tmp_vault, kinds, judge):
    from silica.config import CONFIG
    from silica.tools.review.curate import offer_plan

    said, calls = judge
    vault = CONFIG.vault_path
    for n in "ABCDEF":
        tmp_vault.note(f"{n}.md", f"# {n}\n\nText of {n}.\n")
    # The prompt's words for the two sides, as the worker wrote them on 2026-11-13.
    said[frozenset("AB")] = ("duplicate", "The incoming concept is an exact copy of the candidate note.", "")
    said[frozenset("EF")] = ("contradicts", "They date the founding differently.", "Founded in 280 BC.")
    plan = CurationPlan(items=[_pair("A.md", "B.md"), _pair("C.md", "D.md"), _pair("E.md", "F.md")])

    got = offer_plan(vault, plan)
    assert got == {"asked": 3, "duplicate": 1, "distinct": 1, "contradicts": 1, "waiting": 0}
    (p,) = [x for x in queue.listing(vault) if x.kind == "duplicate"]
    assert p.notes == ("A.md", "B.md") and p.why == "90% similar: A is an exact copy of B."
    kept = {tuple(r["notes"]): r for r in queue.verdicts(vault).values()}
    assert kept[("C.md", "D.md")]["verdict"] == "distinct"
    assert kept[("E.md", "F.md")]["verdict"] == "contradicts"
    assert kept[("E.md", "F.md")]["claim"] == "Founded in 280 BC."

    # The next /curate asks nothing about notes that did not change.
    calls.clear()
    assert offer_plan(vault, plan)["asked"] == 0 and calls == []
    assert [x.id for x in queue.listing(vault) if x.kind == "duplicate"] == [p.id]

    # An edit to one note of a pair asks about that pair alone, once.
    tmp_vault.note("D.md", "# D\n\nNow the text of C.\n")
    said[frozenset("CD")] = ("duplicate", "Now the same.", "")
    got = offer_plan(vault, plan)
    assert got["asked"] == 1 and len(calls) == 1 and set(calls[0]) == {"C", "D"}
    assert len([x for x in queue.listing(vault) if x.kind == "duplicate"]) == 2


def test_the_verdicts_of_one_run_are_capped_and_the_rest_wait(tmp_vault, kinds, judge, monkeypatch):
    from silica.config import CONFIG
    from silica.tools.review import curate
    from silica.tools.review.curate import offer_plan

    _said, calls = judge
    vault = CONFIG.vault_path
    for n in "ABCDEF":
        tmp_vault.note(f"{n}.md", f"# {n}\n\nText of {n}.\n")
    monkeypatch.setattr(curate, "JUDGE_CAP", 2)
    plan = CurationPlan(items=[_pair("A.md", "B.md"), _pair("C.md", "D.md"), _pair("E.md", "F.md")])
    assert offer_plan(vault, plan) == {"asked": 2, "duplicate": 0, "distinct": 2, "contradicts": 0, "waiting": 1}
    assert offer_plan(vault, plan)["asked"] == 1 and len(calls) == 3


def test_an_unreadable_reply_or_a_failed_call_is_not_remembered(tmp_vault, kinds, monkeypatch):
    """A parse failure reads as "distinct" downstream (dedup._decide_dedup);
    kept, it would hide a duplicate until the note changed."""
    from silica.capabilities.dedup import DedupDecision
    from silica.config import CONFIG
    from silica.tools.review.curate import offer_plan

    vault = CONFIG.vault_path
    for n in "ABCD":
        tmp_vault.note(f"{n}.md", f"# {n}\n")
    replies = iter([DedupDecision(verdict="distinct", rationale="unparseable decision", judged=False)])

    def fake(item, config):
        reply = next(replies, None)
        if reply is None:
            raise ConnectionError("worker unreachable")
        return reply

    monkeypatch.setattr("silica.capabilities.judge_pair", fake)
    got = offer_plan(vault, CurationPlan(items=[_pair("A.md", "B.md"), _pair("C.md", "D.md")]))
    assert got == {"asked": 2, "duplicate": 0, "distinct": 0, "contradicts": 0, "waiting": 2}
    assert queue.verdicts(vault) == {}


def test_a_pair_already_merged_is_not_judged_again(tmp_vault, kinds, judge):
    from silica.config import CONFIG
    from silica.tools.review.curate import offer_plan

    _said, calls = judge
    tmp_vault.note("A.md", "# A\n")
    tmp_vault.note("B.md", '---\nsuperseded_by: "[[A]]"\n---\n# B\n')
    assert offer_plan(CONFIG.vault_path, CurationPlan(items=[_pair("A.md", "B.md")]))["asked"] == 0
    assert calls == []


def test_the_judge_reads_both_notes_makes_one_call_and_writes_nothing(tmp_vault, monkeypatch):
    from silica.capabilities.dedup import DedupVerdict, judge_pair
    from silica.config import CONFIG
    from silica.kernel.recall.duplicates import pairs_to_items

    a = tmp_vault.note("Stoicism.md", "# Stoicism\n\nVirtue is the only good.\n")
    b = tmp_vault.note("The Stoics.md", "# The Stoics\n\nThey held virtue alone to be good.\n")
    sent = []

    class Reply:
        text = '{"verdict": "duplicate", "rationale": "Same school.", "addition": ""}'

    class Provider:
        def call_llm(self, **kw):
            sent.append(kw)
            return Reply()

    monkeypatch.setattr("silica.model.providers.get_provider", lambda config, role: Provider())
    (item,) = pairs_to_items([{"source": "Stoicism.md", "target": "The Stoics.md", "score": 0.83}])
    before = (tmp_vault.read(a), tmp_vault.read(b))
    d = judge_pair(item, CONFIG)
    assert (d.verdict, d.rationale, d.judged) == ("duplicate", "Same school.", True)
    (call,) = sent
    assert call["response_schema"] is DedupVerdict and call["reasoning"] is False
    text = call["messages"][0]["content"]
    assert "Virtue is the only good." in text and "They held virtue alone to be good." in text
    assert 'Write "rationale" in English, calling the two notes' in text
    assert (tmp_vault.read(a), tmp_vault.read(b)) == before


def test_apply_all_passes_over_a_kind_whose_yes_spends_the_model(tmp_vault, monkeypatch):
    from silica.config import CONFIG
    from silica.kernel.review import Kind, Proposal

    vault = CONFIG.vault_path
    monkeypatch.setattr(queue, "KINDS", {})
    queue.register(Kind("cheap", "Cheap", lambda v: [Proposal(id="c" * 10, kind="cheap", title="C", why="w")],
                        lambda v, ps: {}))
    queue.register(Kind("paid", "Paid", lambda v: [Proposal(id="p" * 10, kind="paid", title="P", why="w")],
                        lambda v, ps: {}, spends_model=True))
    assert [p.kind for p in queue.pick(vault, ["all"])] == ["cheap"]
    assert [p.kind for p in queue.pick(vault, ["paid"])] == ["paid"]
    assert [p.kind for p in queue.pick(vault, ["pppp"])] == ["paid"]


def test_curate_says_how_many_pairs_were_judged_and_how_many_wait(tmp_vault, kinds, judge, monkeypatch):
    from silica.commands import dispatch
    from silica.tools import curate as curation
    from silica.tools.review import curate

    said, _calls = judge
    for n in "ABCD":
        tmp_vault.note(f"{n}.md", f"# {n}\n")
    said[frozenset("AB")] = ("duplicate", "Same.", "")
    monkeypatch.setattr(curate, "JUDGE_CAP", 1)
    monkeypatch.setattr(curation, "compute_report", lambda **kw: _report(
        confirmed_duplicate_pairs=[DuplicatePair("A.md", "B.md", 0.93), DuplicatePair("C.md", "D.md", 0.86)]))
    out = dispatch("/curate", [])
    text = "\n".join(str(o) for objects, _ in out.lines for o in objects)
    assert "Judged 1 pair" in text and "1 duplicate" in text and "1 pair waits" in text


def test_examine_names_each_pair_as_the_queue_does(tmp_vault, kinds, judge):
    """The Duplicate pairs view's Band: the verdict where one stands for the
    notes as they are, "to judge" where /curate has not asked yet, "link" for
    the borderline band, never a duplicate the queue does not hold."""
    from silica.config import CONFIG
    from silica.tools.review.curate import offer_plan
    from silica.ui.web.metrics_routes import _duplicates

    said, _calls = judge
    for n in "ABCDEFGH":
        tmp_vault.note(f"{n}.md", f"# {n}\n")
    said[frozenset("AB")] = ("duplicate", "Same.", "")
    offer_plan(CONFIG.vault_path, CurationPlan(items=[_pair("A.md", "B.md"), _pair("C.md", "D.md")]))
    report = _report(
        confirmed_duplicate_pairs=[DuplicatePair("A", "B", 0.93), DuplicatePair("C", "D", 0.9),
                                   DuplicatePair("E", "F", 0.88)],
        duplicate_pairs=[DuplicatePair("G.md", "H.md", 0.7, cosine=False), DuplicatePair("A", "C", 0.75)])
    assert [r["band"] for r in _duplicates(report)] == ["duplicate", "distinct", "to judge", "to judge", "link"]
    tmp_vault.note("B.md", "# B\n\nEdited.\n")
    assert _duplicates(report)[0]["band"] == "to judge"
