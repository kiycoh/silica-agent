# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A choice among four (ADR-0168): the wrong options are made with the item and
checked in code, a pick is graded by its key with no call, a right pick is Good
at most and never met without help, and a missed pick comes back on the same
four in a new order."""
# ruff: noqa: F811  the fixtures are imported, so every test that takes one reads as a redefinition
from __future__ import annotations

import json

import pytest

from silica.kernel.learn import grade, items, mastery, reasons
from tests.run_command import expansion as expand
from tests.test_learn_items import vault  # noqa: F401

LINE = "Il margine geometrico e' la distanza dall'iperpiano e limita l'errore atteso del classificatore"
RIGHT = "la distanza dall'iperpiano"


def test_a_wrong_option_that_repeats_the_answer_another_option_or_the_line_is_turned_away():
    wrong = ["La distanza dall'iperpiano.", "l'errore atteso", "il numero di vettori", "la norma dei pesi",
             "il numero di vettori!"]
    assert items.choices(RIGHT, wrong, [LINE]) == {}          # two pass of five: asked in writing
    got = items.choices(RIGHT, wrong + ["il bias del modello"], [LINE])
    assert sorted(got["options"]) == sorted([RIGHT, "il numero di vettori", "la norma dei pesi", "il bias del modello"])
    assert got["options"][got["key"]] == RIGHT
    # picked, the answer is the key with no grader after it: one the line does not hold is asked in writing
    assert items.choices("il numero di vettori di supporto", ["a b", "c d", "e f"], [LINE]) == {}
    again = items.reshuffled(got)
    assert sorted(again["options"]) == sorted(got["options"]) and again["options"][again["key"]] == RIGHT
    # the answer's form shared, a word apart is another answer; the answer with
    # a clause added is the answer (the first live reply, 2026-11-12)
    ten = "Memorize records ten kinds of question."
    live = items.choices(ten, ["Memorize records twelve kinds of question.", "Memorize records ten kinds of question, and levels.",
                               "Memorize records eight kinds of question.", "Memorize records an unlimited number of kinds."],
                         ["Memorize records ten kinds of question."])
    assert len(live["options"]) == 4 and "Memorize records ten kinds of question, and levels." not in live["options"]


REPLY = """ITEM 1
PATH: ML/Margine geometrico.md
KIND: {kind}
LEVEL: 1
ANCHOR: Margine geometrico
SPAN: Il margine geometrico e' la distanza del punto piu' vicino dall'iperpiano.
ANSWER: La distanza del punto piu' vicino dall'iperpiano.
Q: Che cosa misura il margine geometrico di un classificatore?
CRITERIA: the distance of the closest point
H1: Pensa al punto piu' vicino.
WRONG1: La distanza media dei punti dall'origine.
WRONG2: Il numero di errori sul training set.
WRONG3: L'angolo tra due iperpiani separatori.
END
"""


@pytest.mark.parametrize("kind,choosable", [("recall", True), ("explain", False)])
def test_the_options_come_with_the_item_in_the_one_call_for_the_four_kinds_only(vault, monkeypatch, kind, choosable):
    calls = []
    monkeypatch.setattr(items, "ask_model", lambda prompt, n: (calls.append(prompt), (REPLY.format(kind=kind), "stop"))[1])
    built = items.build_session([{"path": "ML/Margine geometrico.md", "kind": kind, "level": 1, "pool": "due"}])
    assert len(calls) == 1 and "WRONG1" in calls[0]
    it = built[0]
    assert it["kind"] == kind and it["grader"] == "model" and "wrong" not in it
    if choosable:
        assert it["options"][it["key"]] == "La distanza del punto piu' vicino dall'iperpiano." and len(it["options"]) == 4
    else:
        assert "options" not in it
    # the page gets the four unmarked; the REPL model gets none of them (G5)
    assert "options" not in items.cue_payload(built)[0]
    page = json.dumps(items.cue_payload(built, options=True))
    assert ('"options"' in page) is choosable and '"key"' not in page and "wrong_why" not in page


AGENTS = """---
title: Agenti
---

# Agenti

Un **agente reattivo** sceglie la sua azione dalla sola percezione corrente, senza memoria.

## Tipi

Un **agente basato su obiettivi** sceglie l'azione che lo avvicina a uno stato desiderato.

Un **agente basato sull'utilità** pesa gli stati raggiungibili con una misura di preferenza.

Ogni agente percepisce e agisce in un ambiente, come descritto in [[Ambiente]].
"""


def test_a_cloze_takes_its_wrong_options_from_its_own_note_with_no_call(tmp_vault, monkeypatch):
    tmp_vault.note("AI/Agenti.md", AGENTS)
    monkeypatch.setattr(items, "ask_model", lambda *a, **k: pytest.fail("a cloze makes no call"))
    it = items.cloze("AI/Agenti.md")
    assert it["answer"] == "agente reattivo"
    assert sorted(it["options"]) == sorted(["agente reattivo", "agente basato su obiettivi",
                                            "agente basato sull'utilità", "Ambiente"])
    assert it["wrong_why"]["Ambiente"] == "This note links to Ambiente, another note."
    assert "under Tipi" in it["wrong_why"]["agente basato su obiettivi"]


def test_a_cloze_option_is_a_note_name_or_one_bold_term_never_link_syntax_or_the_text_between_two():
    # seen 2026-11-12 on the docs vault: "Nucleate#The Sources block" and "menu (" were offered as answers
    tmp = "Un **agente reattivo** non ricorda, vedi [[Ambiente#Tipi di ambiente]].\n\nIl **+** menu (**riassumi**, **spiega**) e [[#Tipi]].\n"
    wrong, why = items._cloze_wrong("AI/Agenti.md", tmp)
    assert wrong == ["agente reattivo", "riassumi", "spiega", "Ambiente"]
    assert why["Ambiente"] == "This note links to Ambiente, another note."


def test_a_pick_is_graded_by_its_key_with_the_caps_of_a_recognition():
    it = {"options": ["a", "b", "c", "d"], "key": 2, "wrong_why": {"b": "b is another note."}, "claim": "c", "span": "s"}
    right = grade.choice_grade(it, pick=2, conf=90, hints=0)
    assert (right["grader"], right["correct"], right["rating"]) == ("choice", True, grade.GOOD)    # never Easy
    assert grade.choice_grade(it, pick=2, conf=25, hints=0)["rating"] == grade.HARD             # one in four by luck
    assert grade.choice_grade(it, pick=2, conf=70, hints=1)["rating"] == grade.HARD             # a hint, as ever
    wrong = grade.choice_grade(it, pick=1, conf=90, hints=0)
    assert (wrong["correct"], wrong["rating"], wrong["feedback_line"]) == (False, grade.AGAIN, "b is another note.")
    assert grade.choice_grade(it, pick=0, conf=90, hints=0)["feedback_line"] == ""   # no known origin, no line
    # a right pick is evidence for the schedule, not met without help, and out of calibration
    line = {"path": "x.md", "correct": True, "rating": 3, "grader": "choice", "conf": 90, "kind": "recall"}
    assert reasons.outcome(line) == "assisted" and not mastery.unassisted(line) and not mastery.eligible(line)
    assert reasons.outcome({**line, "grader": "model"}) == "met"


def test_quiz_choice_asks_among_four_and_records_the_pick_as_a_choice():
    out = expand("/quiz Concepts/ML --choice")
    assert "four numbered options" in out and 'grader "choice"' in out and "`Concepts/ML`" in out
    assert "--choice" not in out and "Mix recall" not in out
    assert "four numbered options" not in expand("/quiz Concepts/ML")


# ---------------------------------------------------------------- the session

pytest.importorskip("fastapi")

from tests.test_learn_panel import _start, client  # noqa: E402,F401

OPTS = ["l'errore atteso", "il numero di vettori", RIGHT, "la norma dei pesi"]


def test_a_session_asks_among_four_and_records_the_keys_rating(client, tmp_path):
    from silica.kernel.learn import session as learn_session

    c, _ = client
    s = _start(c)
    live = learn_session._LIVE
    first = live["items"][0]
    first.update(options=list(OPTS), key=2)
    page = c.get("/learn/session", params={"id": s["id"]}).json()
    assert page["items"][0]["options"] == OPTS and "key" not in page["items"][0]
    assert c.post("/learn/answer", json={"id": s["id"], "i": first["i"], "pick": 4, "conf": 70}).status_code == 400
    v = c.post("/learn/answer", json={"id": s["id"], "i": first["i"], "pick": 2, "conf": 70}).json()
    assert (v["grader"], v["correct"], v["rating"], v["key"], v["pick"]) == ("choice", True, 3, 2, 2)
    r = c.post("/learn/rate", json={"id": s["id"], "i": first["i"], "rating": 4}).json()
    assert r["rating"] == 3 and r["unassisted"] is False and "added" not in r   # Easy from the page is not taken
    line = json.loads((tmp_path / "quiz.jsonl").read_text().splitlines()[-1])
    assert line["grader"] == "choice" and line["rating"] == 3 and line["correct"] is True
    rc = c.post("/learn/close", json={"id": s["id"]}).json()
    row = next(x for x in rc["items"] if x["i"] == first["i"])
    assert row["status"] == "assisted" and row["marker"] == "chosen among four"


def test_a_missed_pick_comes_back_on_the_same_four_in_a_new_order(client, monkeypatch):
    from silica.kernel.learn import session as learn_session

    c, _ = client
    s = _start(c)
    live = learn_session._LIVE
    first = live["items"][0]
    first.update(options=list(OPTS), key=2)
    monkeypatch.setattr(items, "build_session", lambda *a, **k: pytest.fail("a missed pick is not rebuilt"))
    c.post("/learn/answer", json={"id": s["id"], "i": first["i"], "pick": 0, "conf": 90})
    r = c.post("/learn/rate", json={"id": s["id"], "i": first["i"], "rating": 3}).json()
    assert r["rating"] == 1 and r["correct"] is False
    added = r["added"][0]
    assert sorted(added["options"]) == sorted(OPTS) and "key" not in added and added["retest"] is True
    again = live["items"][-1]
    assert again["options"][again["key"]] == RIGHT and again["q"] == first["q"]


def test_a_pick_never_becomes_the_written_answer_and_a_reloaded_right_pick_reads_with_help():
    """Seen in the running app, 2026-11-12: a pick, then "Answer by writing",
    put the option's text in the box, and submitting it was graded by the model
    as met without help and Easy. And after a reload the previous row of a
    right pick read "met" with the full mark."""
    from tests.webassets import WEB

    js = (WEB / "app-learn.js").read_text(encoding="utf-8")
    pick = js[js.index("function lrPick("):js.index("function lrConfParts(")]
    assert "s.answers" not in pick
    # the pick's text reaches the reveal and the previous row from the verdict, as after a reload
    submit = js[js.index("async function lrSubmit("):js.index("async function lrSelfGradeFallback(")]
    assert "s.answers[it.i] = v.options" in submit
    # a pending pick is still something a reload or End session would lose
    assert "picks?.[" in js[js.index("function lrEnd("):js.index("async function lrClose(")]
    assert "picks?.[" in js[js.index('window.addEventListener("beforeunload"'):][:400]
    prev = js[js.index("function lrPrevRow("):js.index("function lrInputParts(")]
    assert "res.unassisted" in prev
    # while a pick is posted the sheet says what was chosen, and promises no 20 s self-rating
    grading = js[js.index("function lrGradingParts("):js.index("function lrMic(")]
    assert '"you chose"' in grading and "if (picked === undefined) g.append(bar" in grading
