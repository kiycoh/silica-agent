"""The typed decision in front of the orphan LLM call (ADR-0044, second site)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from silica.capabilities.orphan import LINK_ACCEPT, run_orphan
from silica.config import CONFIG
from silica.kernel.workqueue import WorkItem


def _item(names):
    return WorkItem(kind="orphan", target_path="Notes/Lonely.md",
                    context={"candidates": [{"name": n, "path": f"Concepts/{n}"} for n in names]},
                    reason="residual_orphan")


def _run(names, p_by_name, monkeypatch):
    """p_by_name: candidate -> noul, or None for a non-answer."""
    monkeypatch.setattr("silica.config.CONFIG.decision_model", "typesafe/jev-1.13")

    def fake_decide(state, questions):
        p = p_by_name[state["candidate_note"]["title"]]
        return None if p is None else {"link": {"type": "noul", "noul": p}}

    with patch("silica.driver.DRIVER.read_note", return_value=MagicMock(content="---\na: 1\n---\nbody")), \
         patch("silica.capabilities.orphan.decide", side_effect=fake_decide), \
         patch("silica.capabilities.orphan._decide_links") as llm, \
         patch("silica.capabilities.orphan.commit_ops",
               return_value={"status": "committed", "committed": 1}) as commit:
        llm.return_value = MagicMock(links=["A"], rationale="llm")
        res = run_orphan(_item(names), CONFIG)
    return res, llm, commit


def test_verdict_replaces_the_llm_call_and_keeps_the_top_three(monkeypatch):
    p = {"A": 0.95, "B": 0.72, "C": LINK_ACCEPT, "D": 0.99, "E": 0.9}
    res, llm, commit = _run(list(p), p, monkeypatch)
    llm.assert_not_called()
    assert res["linked"] == ["D", "A", "E"]  # over the cut, by p, at most 3
    assert len(commit.call_args.args[0]) == 3


def test_all_below_the_cut_is_no_link_without_the_llm(monkeypatch):
    res, llm, commit = _run(["A", "B"], {"A": 0.2, "B": LINK_ACCEPT}, monkeypatch)
    llm.assert_not_called()
    commit.assert_not_called()
    assert res["status"] == "no_link"


def test_a_non_answer_hands_the_orphan_to_the_llm(monkeypatch):
    res, llm, _ = _run(["A", "B"], {"A": 0.99, "B": None}, monkeypatch)
    llm.assert_called_once()
    assert res["linked"] == ["A"]


def test_decide_raising_hands_the_orphan_to_the_llm(monkeypatch):
    monkeypatch.setattr("silica.config.CONFIG.decision_model", "typesafe/jev-1.13")
    with patch("silica.driver.DRIVER.read_note", return_value=MagicMock(content="body")), \
         patch("silica.capabilities.orphan.decide", side_effect=RuntimeError("boom")), \
         patch("silica.capabilities.orphan._decide_links",
               return_value=MagicMock(links=[], rationale="none")) as llm:
        res = run_orphan(_item(["A"]), CONFIG)
    llm.assert_called_once()
    assert res["status"] == "no_link"


def test_off_by_default_never_asks(monkeypatch):
    monkeypatch.setattr("silica.config.CONFIG.decision_model", "")
    with patch("silica.driver.DRIVER.read_note", return_value=MagicMock(content="body")), \
         patch("silica.capabilities.orphan.decide") as dec, \
         patch("silica.capabilities.orphan._decide_links",
               return_value=MagicMock(links=[], rationale="none")) as llm:
        run_orphan(_item(["A"]), CONFIG)
    dec.assert_not_called()
    llm.assert_called_once()


def test_a_staging_orphan_is_left_alone(monkeypatch):
    monkeypatch.setattr("silica.kernel.vault_manifest.active_done_dir", lambda: "Done")
    for target in ("Inbox/lecture.md", "Done/book/chapter-1.md"):
        item = WorkItem(kind="orphan", target_path=target,
                        context={"candidates": [{"name": "A", "path": "Concepts/A"}]}, reason="residual_orphan")
        with patch("silica.capabilities.orphan._decide_links") as llm:
            res = run_orphan(item, CONFIG)
        llm.assert_not_called()
        assert res == {"status": "skipped", "reason": "staging note"}


def test_link_candidates_never_offer_a_staging_note(monkeypatch):
    from silica.kernel.recall.relatedness import RelatedNote
    from silica.tools.curate import link_candidates

    monkeypatch.setattr("silica.kernel.vault_manifest.active_done_dir", lambda: "Done")
    store = MagicMock()
    store.paths.return_value = ["Concepts/A", "Inbox/chapter", "Done/book/chapter-vi"]
    with patch("silica.kernel.recall.embed.get_store", return_value=store), \
         patch("silica.kernel.recall.cooccurrence.get_cooccur_store", return_value=None), \
         patch("silica.agent.providers.get_reranker", return_value=None), \
         patch("silica.kernel.recall.relatedness.related_notes",
               return_value=[MagicMock(spec=RelatedNote, name="A", path="Concepts/A")]) as rel:
        link_candidates("Notes/Lonely.md", k=3)
    assert rel.call_args.kwargs["exclude"] == {"Inbox/chapter", "Done/book/chapter-vi"}
