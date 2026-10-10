# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Objectives (learn-suite D27, ADR-0041): combinable intentions, cards in
Mager's shape, the plan note's section, per-objective readings; and key
points (D28): a note session sized on its distinct claims."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

import silica.tools.notes  # noqa: F401 — the write gate registers on import
from silica.kernel.learn import items, mastery, objectives as obj, plans
from tests.test_learn_items import LEAF, NOTE

DAY = 86400.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _e(path, days_ago, **kw):
    base = {"ts": _iso(time.time() - days_ago * DAY), "path": path, "correct": True, "rating": 3,
            "kind": "explain", "level": 3, "grader": "model", "hints": 0, "told": False, "conf": 70}
    base.update(kw)
    return base


@pytest.fixture
def vault(tmp_vault, tmp_path, monkeypatch):
    import silica.kernel.report.quiz as quiz
    from silica.kernel.write import undo_journal

    monkeypatch.setattr(quiz, "log_path", lambda: tmp_path / "quiz.jsonl")
    monkeypatch.setattr(undo_journal, "_DEFAULT_JOURNAL_PATH", tmp_path / "undo.db", raising=False)
    monkeypatch.setattr(items, "_llm", lambda *a, **k: ("", "stop"))
    tmp_vault.note("ML/Margine geometrico.md", NOTE.replace("AI: true\n", "date: 2025-01-01\n"))
    tmp_vault.note("sources/Lezione 11.md", LEAF)
    tmp_vault.note("ML/Iperpiano.md", "---\ndate: 2025-01-01\n---\n# Iperpiano\n\nUn piano che separa lo spazio in due semispazi distinti.\n")
    return tmp_vault


# ------------------------------------------------------------------ intentions

def test_intentions_normalize_and_a_legacy_line_reads_by_its_kind():
    assert obj.normalize(["Explain", "apply", "explain", "nonsense"]) == ["explain", "apply"]
    assert obj.normalize("recall, compare") == ["recall", "compare"]
    assert obj.of_entry({"kind": "cloze"}) == "recall"
    assert obj.of_entry({"kind": "structure"}) == "explain"
    assert obj.of_entry({"kind": "contrast"}) == "compare"
    assert obj.of_entry({"kind": "recall", "objective": "apply"}) == "apply"   # written: wins over the kind
    assert obj.level_of(["recall"]) == 1 and obj.level_of(["recall", "evaluate"]) == 4
    assert obj.REACH_TO_INTENTIONS["relational"] == ["explain"]


def test_cards_take_mager_shape_and_the_template_holds_without_a_model(vault):
    tree = [{"node": "Margine", "leaves": ["ML/Margine geometrico.md"]}, {"node": "Nesterov", "leaves": [], "gap": True}]
    cards = obj.propose(tree, ["explain", "apply"])
    assert [c["source"] for c in cards] == ["chosen", "chosen", "gap"]
    assert cards[0]["intention"] == "explain" and cards[0]["title"].startswith("Explain how Margine geometrico")
    assert cards[0]["conditions"] and cards[0]["criterion"] and cards[0]["paths"] == ["ML/Margine geometrico.md"]
    assert cards[2]["title"].startswith("Kept on the plan as a gap")
    # nothing chosen: one card per node on the intention the material suggests, marked proposed
    auto = obj.propose(tree, [])
    assert auto[0]["source"] == "proposed" and auto[0]["intention"] == "explain"   # relations + two claims


def test_the_model_rewrites_the_cards_and_a_garbled_reply_leaves_the_template(vault, monkeypatch):
    tree = [{"node": "Margine", "leaves": ["ML/Margine geometrico.md"]}]
    monkeypatch.setattr(items, "_llm", lambda *a, **k: (
        "OBJ 1\nTITLE: Spiegare perché il margine limita l'errore.\nCONDITIONS: senza consultare gli appunti\n"
        "CRITERION: collegare margine e bound con la giustificazione.\nEND\n", "stop"))
    cards = obj.propose(tree, ["explain"])
    assert cards[0]["title"].startswith("Spiegare") and cards[0]["criterion"].startswith("collegare")
    monkeypatch.setattr(items, "_llm", lambda *a, **k: ("nonsense", "stop"))
    assert obj.propose(tree, ["explain"])[0]["title"].startswith("Explain how")


def test_the_objectives_call_names_the_language_and_an_echoed_title_keeps_the_template(vault, monkeypatch):
    # Measured 2026-10-08 on Qwen3.5-9B over Psicologia/ (test vault): asked for
    # "the language of the notes" it wrote English cards for Italian notes, and
    # 7 of 7 TITLEs were the INTENTION line copied back. ADR-0065 fixed the
    # same two faults for questions by naming LANGUAGE and checking in code.
    tree = [{"node": "Margine", "leaves": ["ML/Margine geometrico.md"]}]
    seen = {}

    def reply(prompt, *_a, **_k):
        seen["prompt"] = prompt
        return ("OBJ 1\nTITLE: explain (Explain how and why: Make relations, mechanisms and conditions of validity explicit.)\n"
                "CONDITIONS: senza consultare gli appunti\nCRITERION: collegare margine e bound.\nEND\n", "stop")

    monkeypatch.setattr(items, "_llm", reply)
    card = obj.propose(tree, ["explain"])[0]
    assert "LANGUAGE: Italian" in seen["prompt"]
    assert card["title"].startswith("Explain how Margine geometrico")      # the template, not the echo
    assert card["criterion"].startswith("collegare")                        # the rest of the block is kept
    got = obj.parse_reply("OBJ 1\nTITLE: spiega a\nCONDITIONS: c\nCRITERION: k\nOBJ 2\nTITLE: richiama b\nCRITERION: k2\n")
    assert sorted(got) == [1, 2] and got[2]["title"] == "richiama b"         # no END line: still two blocks
    assert obj._unlabel("Recall information: restate the two definitions.", "recall") == "Restate the two definitions."
    assert obj._unlabel("Explain: why the margin bounds the error.", "explain") == "Why the margin bounds the error."
    assert obj._unlabel("Spiegare perché il margine limita l'errore.", "explain").startswith("Spiegare")
    assert obj._unlabel("richiama le due definizioni", "recall") == "Richiama le due definizioni"


def test_the_objectives_section_round_trips():
    cards = [obj.card("explain", "step 4, Backpropagation", ["ML/Iperpiano.md"], "chosen",
                      title="Explain the role of the chain rule in backpropagation.",
                      conditions="given a simple network, without consulting the notes",
                      criterion="link the dependencies between layers correctly"),
             obj.card("", "step 3, Nesterov", [], "gap")]
    lines = [obj.format_line(c) for c in cards]
    assert lines[0] == ("- explain · step 4, Backpropagation · chosen: Explain the role of the chain rule in backpropagation."
                        " Conditions: given a simple network, without consulting the notes."
                        " Criterion: link the dependencies between layers correctly.")
    back = obj.parse_section(lines)
    assert back[0]["intention"] == "explain" and back[0]["node"] == "step 4, Backpropagation" and back[0]["source"] == "chosen"
    assert back[0]["title"] == "Explain the role of the chain rule in backpropagation."
    assert back[0]["conditions"] == "given a simple network, without consulting the notes"
    assert back[0]["criterion"] == "link the dependencies between layers correctly"
    assert back[1]["source"] == "gap"


# ------------------------------------------------------------------ readings

def test_objective_status_counts_only_unassisted_success_and_keeps_the_rest_beside_it():
    evs = [_e("A.md", 10, objective="explain", hints=1),                 # with help: not counted
           _e("A.md", 8, objective="explain", correct=False, rating=1),   # missed
           _e("A.md", 5, objective="apply", kind="apply")]                # apply met, explain still open
    st = mastery.objectives_met(["A.md"], ["explain", "apply"], evs)["A.md"]
    assert st["explain"]["met"] is None and st["explain"]["assisted"] and st["explain"]["missed"] and st["explain"]["state"] == "missed"
    assert st["apply"]["met"] and st["apply"]["state"] == "met" and st["apply"]["asked"] == 1
    later = evs + [_e("A.md", 1, objective="explain")]
    assert mastery.objectives_met(["A.md"], ["explain"], later)["A.md"]["explain"]["state"] == "met"
    # `since` cuts what came before the plan was written
    old = [_e("A.md", 400, objective="explain")]
    assert mastery.objectives_met(["A.md"], ["explain"], old, since="2026-01-01")["A.md"]["explain"]["met"] is None


def test_node_readings_count_leaves_that_met_each_objective():
    view = {"a": {"path": "A.md", "R": 0.5, "S": 3.0, "attempts": 2}, "b": {"path": "B.md", "R": 0.95, "S": 40.0, "attempts": 1}}
    evs = [_e("A.md", 5, objective="explain"), _e("A.md", 4, objective="apply", kind="apply"), _e("B.md", 5, objective="explain")]
    node = mastery.node_readings(["A.md", "B.md"], view, evs, None, ["explain", "apply"])
    assert node["met_by_objective"] == {"explain": 2, "apply": 1} and node["all_met"] == 1
    assert node["leaves"]["A.md"]["objectives"]["apply"]["state"] == "met"


# ------------------------------------------------------------------ the plan note

def test_write_plan_carries_objectives_and_cards_and_reads_them_back(vault):
    out = plans.write_plan("Esame", "notes", ["Margine geometrico", "Iperpiano"], objectives=["explain", "apply"])
    text = plans._read(out["path"])
    assert "objectives:" in text and "## Objectives" in text and "- explain · " in text and "- apply · " in text
    assert "Learn these notes: explain how and why, and apply to a case." in text
    p = plans.read_plan(out["path"])
    assert p["objectives"] == ["explain", "apply"] and p["objectives_chosen"] is True and p["reach_level"] == 3
    assert len(p["cards"]) == 4 and all(c["source"] == "chosen" for c in p["cards"])
    rep = plans.plan_report(p)
    assert rep["objective_labels"] == ["Explain how and why", "Apply to a case"]
    assert rep["cards"][0]["paths"] == ["ML/Margine geometrico.md"]        # re-attached from the tree
    assert rep["next"]["objective"] == "explain" and rep["readings"]["met_by_objective"] == {"explain": 0, "apply": 0}


def test_a_plan_with_nothing_chosen_carries_proposed_cards_and_a_legacy_reach_still_reads(vault):
    out = plans.write_plan("Auto", "notes", ["Margine geometrico"], objectives=[])
    p = plans.read_plan(out["path"])
    assert p["objectives_chosen"] is False and p["cards"][0]["source"] == "proposed" and p["objectives"] == ["explain"]
    vault.note("Plans/Old.md", "---\ntype: plan\nscope: notes\ntarget: Iperpiano\nreach: extended\n---\n# Old\n\n## Tree\n- [[Iperpiano]]\n")
    old = plans.read_plan("Plans/Old.md")
    assert old["objectives"] == ["explain", "apply"] and old["objectives_chosen"] is True and old["reach_level"] == 3


def test_steps_pass_and_plans_complete_per_objective(vault):
    from silica.kernel.report import quiz

    vault.note("Plans/Steps.md", "---\ntype: plan\nscope: steps\ntarget: Plans/Steps.md\nobjectives: [explain, apply]\ncreated: 2026-01-01\n---\n# Steps\n\n## Steps\n- [ ] [[Iperpiano]]: first\n\n## Sessions\n")
    p = plans.read_plan("Plans/Steps.md")
    quiz.record([{"path": "ML/Iperpiano.md", "correct": True, "rating": 3, "kind": "explain", "level": 3, "hints": 0, "objective": "explain"}])
    assert mastery.steps_passed(p) == [False]                    # apply still to verify
    assert plans.next_objective(p, "ML/Iperpiano.md") == "apply"
    quiz.record([{"path": "ML/Iperpiano.md", "correct": True, "rating": 3, "kind": "apply", "level": 3, "hints": 0, "objective": "apply"}])
    assert mastery.steps_passed(p) == [True] and plans.completion(p, quiz.entries()) is True
    line = quiz.entries()[-1]
    assert line["objective"] == "apply"                          # the ledger writer keeps the field


def test_assign_objectives_turns_a_recall_row_into_the_objective_the_note_lacks(vault):
    vault.note("Plans/P.md", "---\ntype: plan\nscope: notes\ntarget: Iperpiano\nobjectives: [explain, apply]\ncreated: 2026-01-01\n---\n# P\n\n## Tree\n- [[Iperpiano]]\n")
    p = plans.read_plan("Plans/P.md")
    rows = [{"path": "ML/Iperpiano.md", "kind": "recall", "level": 1}, {"path": "ML/Iperpiano.md", "kind": "predict", "level": 1}]
    plans.assign_objectives(p, rows)
    assert rows[0]["objective"] == "explain" and rows[0]["kind"] == "explain" and rows[0]["level"] == 3
    assert rows[1]["objective"] == "recall" and rows[1]["kind"] == "predict"   # a first attempt keeps its kind


# ------------------------------------------------------------------ key points (D28)

def test_key_points_are_the_distinct_grounded_claims_with_a_kind(vault):
    pts = items.key_points("ML/Margine geometrico.md")
    assert [p["i"] for p in pts] == [1, 2] and pts[0]["kind"] == "definition"
    assert all(p["line"] for p in pts)
    asks = items.ask_plan(pts, ["recall", "explain"], relations=2)
    assert [(a["point"], a["objective"], a["kind"]) for a in asks] == [(1, "recall", "recall"), (2, "explain", "explain"), (0, "explain", "structure")]
    only = items.ask_plan(pts, ["explain"], relations=2, only=[2])
    assert [(a["point"], a["objective"]) for a in only] == [(2, "explain")]   # a follow-up asks the open point only
    assert len(items.ask_plan([{"i": i, "kind": "claim"} for i in range(1, 20)], ["explain"])) == items.MAX_QUESTIONS


def test_a_note_session_builds_one_item_per_point_with_mechanical_fallback(vault):
    pts = items.key_points("ML/Margine geometrico.md")
    row = {"path": "ML/Margine geometrico.md", "kind": "explain", "level": 3, "pool": "note",
           "asks": items.ask_plan(pts, ["recall", "explain"], relations=2)}
    built = items.build_session([row], [], n=3)
    assert len(built) == 3
    assert built[0]["point"] == {"i": 1, "of": 2, "kind": "definition", "heading": "Margine geometrico", "line": pts[0]["note_line"]}
    assert built[0]["kind"] == "cloze" and built[0]["objective"] == "recall"
    assert built[1]["point"]["i"] == 2 and built[1]["objective"] == "explain"
    assert built[2]["kind"] == "structure" and built[2]["point"]["kind"] == "relation"
    cue = items.cue_payload(built)
    assert cue[0]["objective"] == "recall" and cue[0]["point"]["i"] == 1 and "answer" not in cue[0]


def test_the_model_contract_asks_one_item_per_point_and_parses_point_and_objective(vault, monkeypatch):
    pts = items.key_points("ML/Margine geometrico.md")
    row = {"path": "ML/Margine geometrico.md", "kind": "explain", "level": 3,
           "asks": items.ask_plan(pts, ["recall", "explain"], relations=0)}
    block = items.note_block(1, row)
    assert "POINTS:" in block and "ASK:" in block and "- POINT 1: KIND recall" in block and "OBJECTIVE explain" in block
    reply = ("ITEM 1\nPATH: ML/Margine geometrico.md\nKIND: recall\nLEVEL: 1\nOBJECTIVE: recall\nPOINT: 1\nANCHOR: Margine geometrico\n"
             "Q: Che cos'e' il margine?\nCRITERIA: la distanza\nANSWER: la distanza del punto piu' vicino\n"
             f"SPAN: {pts[0]['text']}\nH1: pensa alla distanza\nEND\n"
             "ITEM 2\nPATH: ML/Margine geometrico.md\nKIND: explain\nLEVEL: 3\nOBJECTIVE: explain\nPOINT: 2\nANCHOR: Definizione\n"
             "Q: Perche' limita l'errore?\nCRITERIA: il bound\nANSWER: perche' il bound dipende dal margine\n"
             f"SPAN: {pts[1]['text']}\nH1: pensa al bound\nEND\n")
    monkeypatch.setattr(items, "_llm", lambda *a, **k: (reply, "stop"))
    got = items.generate([row])
    assert [it["point"] for it in got["ML/Margine geometrico.md"]] == [1, 2]
    built = items.build_session([row], [], n=2)
    assert [it["q"] for it in built] == ["Che cos'e' il margine?", "Perche' limita l'errore?"]
    assert built[1]["grader"] == "model" and built[1]["point"]["i"] == 2


def test_a_title_opening_with_another_intentions_verb_is_named():
    """ADR-0081 §16: 6 of 21 cards opened with another intention's verb."""
    assert obj.reads_as("Richiama i fenomeni psicologici") == "recall"
    assert obj.reads_as("Apply the margin to a case") == "apply"
    assert obj.reads_as("Il modello TOTE") is None and obj.reads_as("") is None


def test_a_none_in_the_notes_language_is_no_misconception():
    entries = [{"path": "A.md", "ts": "2026-10-06T10:00:00+00:00", "misconception": m} for m in ("nessuna", "None.", "confonde S e R")]
    assert [m["misconception"] for m in mastery.misconceptions(["A.md"], entries)] == ["confonde S e R"]
