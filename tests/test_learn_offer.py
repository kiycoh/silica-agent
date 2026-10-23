# SPDX-License-Identifier: AGPL-3.0-or-later
"""The study offer under a chat answer (silica/kernel/learn/offer.py): silent
unless the ledger or the reader's words say the topic is hard."""
from silica.kernel.learn import offer


def _e(path, ts, correct, **kw):
    return {"path": path, "ts": ts, "correct": correct, **kw}


def test_silent_without_a_struggle_or_a_word_saying_so():
    reads = ["ML/SVM.md"]
    assert offer.study_offer("cos'è una SVM?", reads, entries=[]) is None
    ok = [_e("ML/SVM.md", "2026-10-20", True), _e("ML/SVM.md", "2026-10-21", True)]
    assert offer.study_offer("cos'è una SVM?", reads, entries=ok) is None


def test_two_misses_of_the_last_three_offer_practice_on_that_note():
    reads = ["ML/Kernel trick.md", "ML/SVM.md"]
    led = [_e("ML/SVM.md", "2026-10-18", True), _e("ML/SVM.md", "2026-10-20", False),
           _e("ML/SVM.md", "2026-10-21", False), _e("ML/Kernel trick.md", "2026-10-21", True)]
    o = offer.study_offer("e il duale?", reads, entries=led)
    assert o["kind"] == "study" and o["note"] == "ML/SVM.md"
    assert o["text"] == "you missed SVM in recent practice"
    # a note with no shape to read (none on disk here): practice, the explanation, a diagram, cards
    assert [c["command"] for c in o["commands"]] == ['/quiz "ML/SVM.md" --n=3', '/explain "SVM"', '/diagram "SVM"',
                                                     '/flashcards "SVM" --n=8']


def test_a_note_last_answered_by_being_told_counts_too():
    led = [_e("ML/PCA.md", "2026-10-21", True, told=True)]
    assert offer.struggling(["ML/PCA.md"], led) == ["ML/PCA.md"]


def test_saying_it_was_not_clear_is_enough_and_old_misses_do_not_count():
    reads = ["ML/CCA.md"]
    old = [_e("ML/CCA.md", "2026-09-01", False), _e("ML/CCA.md", "2026-09-02", False),
           _e("ML/CCA.md", "2026-10-01", True), _e("ML/CCA.md", "2026-10-02", True),
           _e("ML/CCA.md", "2026-10-03", True)]
    assert offer.study_offer("ok", reads, entries=old) is None
    o = offer.study_offer("non ho capito, spiegami meglio", reads, entries=old)
    assert o["text"] == "you said this was not clear" and o["note"] == "ML/CCA.md"
    assert offer.says_lost("I'm lost here") and not offer.says_lost("capisco bene")
    assert offer.says_lost("la formulazione lagrangiana: non l'ho capita")


def test_a_source_leaf_is_never_the_note_offered():
    led = [_e("sources/x/L1.md", "2026-10-20", False), _e("sources/x/L1.md", "2026-10-21", False)]
    assert offer.study_offer("?", ["sources/x/L1.md"], entries=led) is None


def test_the_turn_names_its_reads_by_title_and_the_offer_resolves_them(monkeypatch):
    """A chat turn names a read note as its tool call did, often a bare title;
    the rule needs the path the ledger keys it by (seen in the app: no offer
    under an answer that had read "Teorema di Novikoff")."""
    import silica.kernel.recall.mindmap as mm
    import silica.ui.web.server as srv

    monkeypatch.setattr(srv, "read_refs", lambda messages: ["Teorema di Novikoff"])
    monkeypatch.setattr(mm, "answer_resolver",
                        lambda refs, graph=None: (None, ["ML/Teorema di Novikoff.md"] if refs == ["Teorema di Novikoff"] else []))
    monkeypatch.setattr(mm, "_driver_graph", lambda: ({}, {"ML/Punti di supporto.md": "Punti di supporto"}, None))
    monkeypatch.setattr("silica.kernel.user_memory.incognito", lambda: False)
    monkeypatch.setattr("silica.kernel.report.quiz.entries", lambda: [])
    # the offer log's caps and declines are tested on their own
    monkeypatch.setattr(offer, "log_offer", lambda *a, **kw: None)
    o = srv._study_offer("non ho capito il teorema", [])
    assert o and o["note"] == "ML/Teorema di Novikoff.md"
    # ...but the note the message names comes before a neighbour the turn read first
    monkeypatch.setattr(mm, "_driver_graph", lambda: ({}, {"ML/Punti di supporto.md": "Punti di supporto",
                                                          "ML/Teorema di Novikoff.md": "Teorema di Novikoff"}, None))
    o = srv._study_offer("non ho capito i punti di supporto", [])
    assert o and o["note"] == "ML/Punti di supporto.md"
    monkeypatch.setattr(mm, "_driver_graph", lambda: ({}, {"ML/Punti di supporto.md": "Punti di supporto"}, None))

    # a turn that read nothing still has the note the reader named
    monkeypatch.setattr(srv, "read_refs", lambda messages: [])
    o = srv._study_offer("Non ho capito cosa sono i punti di supporto in una SVM", [])
    assert o and o["note"] == "ML/Punti di supporto.md"
    assert srv._study_offer("cosa sono i punti di supporto?", []) is None   # no word saying it was unclear


def test_a_study_plan_asked_of_the_chat_points_at_learn():
    """The chat wrote its own plan, dated "about 17 weeks" for an exam 18
    days away, while Learn tracked one (2026-10-23)."""
    plans = [{"name": "Esame Machine Learning", "path": "Plans/Esame Machine Learning.md", "by": "2026-11-10",
              "status": "active"}]
    o = offer.plan_offer("Prepara un piano di studio fino all'esame del 10 novembre", plans)
    assert o["text"] == "Learn tracks a plan for this: Esame Machine Learning, by 2026-11-10"
    assert o["commands"] == [{"label": "open it", "href": "#learn/plan/Plans%2FEsame%20Machine%20Learning.md"},
                             {"label": "continue it", "command": "/learn"}]
    assert offer.plan_offer("help me make a study plan", [])["commands"] == [{"label": "write it as a plan", "href": "#learn/new"}]
    assert offer.plan_offer("cos'è il dropout?", plans) is None



def test_a_confident_miss_counts_once_confidence_is_calibrated():
    """Confidence is read after CALIBRATION_MIN eligible answers (research
    report difficulty-detection-and-study-aids, 8): a miss while sure is a
    blind spot and gets a contrast table first; right while unsure counts too,
    as it predicted the next miss on Eedi's students (43.6%, owner 2026-10-23)."""
    from silica.kernel.learn.mastery import CALIBRATION_MIN

    history = [_e(f"ML/N{i}.md", f"2026-10-0{i % 9 + 1}", True, conf=70, grader="model") for i in range(CALIBRATION_MIN)]
    sure_wrong = [_e("ML/SVM.md", "2026-10-21", False, conf=90, grader="model")]
    assert offer.struggles(["ML/SVM.md"], sure_wrong) == {}                     # not calibrated yet
    assert offer.struggles(["ML/SVM.md"], history + sure_wrong) == {"ML/SVM.md": "you were sure about SVM and missed it"}
    unsure_right = [_e("ML/SVM.md", "2026-10-21", True, conf=25, grader="model")]
    assert offer.struggles(["ML/SVM.md"], history + unsure_right) == {"ML/SVM.md": "you got SVM right but were unsure"}
    assert offer.struggles(["ML/SVM.md"], unsure_right) == {}                    # not calibrated yet
    sure_right = [_e("ML/SVM.md", "2026-10-21", True, conf=90, grader="model")]
    assert offer.struggles(["ML/SVM.md"], history + sure_right) == {}
    o = offer.study_offer("e il duale?", ["ML/SVM.md"], entries=history + sure_wrong)
    assert [c["label"] for c in o["commands"]][:3] == ["practise it", "as a table", "explain it"]



def test_the_aids_follow_the_notes_shape():
    """Practice first, the explanation for a reader who said it was not clear,
    then what the note's shape asks for: a table, a diagram, a map, cards, slides."""
    table = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"
    labels = lambda **kw: [a["label"] for a in offer.aids("ML/X.md", "X", **kw)]
    assert labels(text=table, state="lost") == ["practise it", "explain it", "as a table"]
    linked = " ".join(f"[[N{i}]]" for i in range(6)) + "\n## A\n## B\n## C\n"
    assert labels(text=linked) == ["practise it", "explain it", "map it", "slides"]
    steps = "1. uno\n2. due\n3. tre\n$$x$$ $$y$$\n"
    assert "as a diagram" in labels(text=steps) and len(labels(text=table + linked + steps, state="lost")) == offer.AIDS_SHOWN

    spine = '---\nsection: "(outline)"\n---\n1. [[A]]\n2. [[B]]\n3. [[C]]\n4. [[D]]\n5. [[E]]\n'
    assert labels(text=spine)[:3] == ["practise it", "explain it", "slides"]



def test_two_offers_a_chat_none_on_a_note_ignored_this_week(monkeypatch, tmp_path):
    """The research report's section 8: silent after two offers in a session,
    and on a note whose offer was declined or ignored in the last 7 days."""
    import time
    from datetime import datetime, timedelta, timezone

    now = time.time()
    iso = lambda days: (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=days)).isoformat()
    shown = [{"ts": iso(0), "chat": "a", "note": "ML/X.md", "shown": True},
             {"ts": iso(0), "chat": "a", "note": "ML/Y.md", "shown": True}]
    assert not offer.allowed("a", "ML/Z.md", now=now, records=shown)          # two shown in chat a
    assert offer.allowed("b", "ML/Z.md", now=now, records=shown)
    assert not offer.allowed("b", "ML/X.md", now=now, records=shown)          # X ignored in chat a this week
    acted = shown + [{"ts": iso(0), "chat": "a", "note": "ML/X.md", "acted": True}]
    assert offer.allowed("b", "ML/X.md", now=now, records=acted)               # ...unless it was used
    old = [{"ts": iso(9), "chat": "a", "note": "ML/X.md", "shown": True}]
    assert offer.allowed("b", "ML/X.md", now=now, records=old)                 # a week has passed


def test_escalation_makes_the_answer_the_aid():
    led = [_e("ML/SVM.md", f"2026-10-2{i}", False) for i in range(3)]
    assert offer.escalation("ML/SVM.md", "c", "e il duale?", [], led, records=[]) == "3 misses in a row"
    ignored = [{"ts": "2026-10-20T00:00:00+00:00", "chat": "c", "note": "ML/SVM.md", "shown": True}]
    two_more = [_e("ML/SVM.md", "2026-10-21", False), _e("ML/SVM.md", "2026-10-22", False)]
    assert offer.escalation("ML/SVM.md", "c", "ok", [], two_more, records=ignored) == "two more misses after an offer went unused"
    assert offer.escalation("ML/SVM.md", "c", "non ho capito ancora", ["non ho capito"], [], records=[]) == \
        "still unclear after an answer"
    assert offer.escalation("ML/SVM.md", "c", "grazie", [], [], records=[]) == ""
    block = offer.escalation_block("SVM", "3 misses in a row")
    assert "[[SVM]]" in block and "worked example" in block and "Mermaid" in block


def test_replay_reads_whether_the_rule_fires_before_misses():
    led = [_e("ML/A.md", "2026-10-01", False), _e("ML/A.md", "2026-10-02", False), _e("ML/A.md", "2026-10-03", False),
           _e("ML/B.md", "2026-10-01", True), _e("ML/B.md", "2026-10-02", True)]
    r = offer.replay(led, records=[{"shown": True}, {"acted": True}])
    assert r["fired"] == {"wrong": 1, "right": 0} and r["silent"] == {"wrong": 1, "right": 1}
    assert r["miss_rate_when_fired"] == 1.0 and r["offers_shown"] == 1 and r["offers_acted"] == 1


def test_the_rule_reads_a_public_log_of_real_students(tmp_path):
    """scripts/validate_study_offer.py --assistments: each student's answers on
    a skill a ledger of its own, scaffolding rows left out, an answer shown
    read as told."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "validate_study_offer", Path(__file__).resolve().parent.parent / "scripts" / "validate_study_offer.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    rows = ["order_id,user_id,skill_id,original,correct,bottom_hint"]
    # student 1 on skill A: wrong, wrong, wrong, wrong, right; a scaffold row in between is not an answer
    for o, c in zip(range(1, 6), (0, 0, 0, 0, 1)):
        rows.append(f"{o},1,A,1,{c},0")
    rows.append("3,1,A,0,1,0")
    # student 2 on skill A: right, right, shown the answer, right
    for o, (c, b) in zip(range(10, 14), ((1, 0), (1, 0), (0, 1), (1, 0))):
        rows.append(f"{o},2,A,1,{c},{b}")
    csv_path = tmp_path / "log.csv"
    csv_path.write_text("\n".join(rows) + "\n", encoding="ISO-8859-15")
    r = script.assistments(str(csv_path))
    assert r["students"] == 2 and r["answers"] == 9
    # fired before s1's answers 3, 4, 5 (two or more misses in three) and s2's last (told)
    assert r["fired"]["n"] == 4 and r["fired"]["miss_rate"] == 0.5
    assert r["silent"]["n"] == 3 and r["silent"]["miss_rate"] == round(2 / 3, 3)
    assert r["after_3_misses"]["n"] == 2 and r["after_3_misses"]["miss_rate"] == 0.5
    assert script.wilson(0, 0) is None and script.wilson(5, 10)[0] < 0.5 < script.wilson(5, 10)[1]


def test_the_rule_shows_unless_told_hosted_too(monkeypatch):
    """The owner chose visible on the hosted pilot (2026-10-23); shadow and off
    stay one variable away."""
    monkeypatch.delenv("SILICA_STUDY_OFFER", raising=False)
    monkeypatch.setenv("SILICA_HOSTED", "1")
    assert offer.mode() == "on"
    monkeypatch.setenv("SILICA_STUDY_OFFER", "shadow")
    assert offer.mode() == "shadow"
    monkeypatch.setenv("SILICA_STUDY_OFFER", "nonsense")
    assert offer.mode() == "on"


SPINE = "---\nsection: (outline)\n---\n# Lezione 14\n\n## Margini\n- [[Margine funzionale]]\n## Slack\n- [[Variabili slack]]\n"


def test_the_escalated_aid_takes_the_form_the_note_calls_for():
    assert "4 to 6 slides" in offer.escalation_block("Lezione 14", "still unclear after an answer", SPINE)
    table = "# Confronto\n\n| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"
    assert "one Markdown table, one row each" in offer.escalation_block("Confronto", "3 misses in a row", table)
    assert "worked example" in offer.escalation_block("Breve", "3 misses in a row", "# Breve\n\nUna riga.\n")


def test_the_chat_escalation_in_shadow_logs_and_adds_nothing(monkeypatch, tmp_path):
    import json

    import silica.kernel.recall.mindmap as mm
    import silica.ui.web.server as srv
    from silica.kernel.learn import items

    monkeypatch.setattr(offer, "_log_path", lambda: tmp_path / "offers.jsonl")
    monkeypatch.setattr("silica.kernel.user_memory.incognito", lambda: False)
    monkeypatch.setattr(mm, "_driver_graph", lambda: ({}, {"ML/Lezione 14.md": "Lezione 14"}, None))
    monkeypatch.setattr("silica.kernel.report.quiz.entries", lambda: [])
    monkeypatch.setattr(items, "read_note", lambda path: SPINE)
    lost = [{"role": "user", "content": "non ho capito la Lezione 14"}]
    monkeypatch.setenv("SILICA_STUDY_OFFER", "shadow")
    assert srv._escalation("non ho ancora capito la Lezione 14", lost) == ""
    rec = json.loads((tmp_path / "offers.jsonl").read_text().splitlines()[-1])
    assert rec["escalated"] and rec["displayed"] is False and rec["why"] == "still unclear after an answer"
    monkeypatch.setenv("SILICA_STUDY_OFFER", "on")
    block = srv._escalation("non ho ancora capito la Lezione 14", lost)
    assert "4 to 6 slides" in block and "READ-ONLY" in block and "give no answer" in block


def test_still_not_following_is_read_as_lost():
    for said in ("non ho ancora capito la Lezione 14", "continuo a non capire il duale", "non mi è ancora chiaro",
                 "non mi e' del tutto chiaro", "I still don't get it", "I'm still lost"):
        assert offer.says_lost(said), said
    assert not offer.says_lost("ho capito, grazie") and not offer.says_lost("capisco il kernel trick")


def test_said_unclear_the_offer_is_about_the_note_named_not_one_missed_elsewhere():
    led = [_e("ML/Slack.md", f"2026-10-2{i}", False) for i in range(3)]
    o = offer.study_offer("non ho capito la Lezione 14", ["ML/Lezione 14.md", "ML/Slack.md"], led)
    assert o["note"] == "ML/Lezione 14.md" and o["text"] == "you said this was not clear"
    o = offer.study_offer("e il duale?", ["ML/Lezione 14.md", "ML/Slack.md"], led)
    assert o["note"] == "ML/Slack.md"


def test_saying_it_was_unclear_never_becomes_a_skill(monkeypatch, tmp_path):
    import silica.ui.web.server as srv
    from silica.kernel import proposed_skills as ps

    monkeypatch.setattr(srv.CONFIG, "vault_path", str(tmp_path))
    monkeypatch.setattr("silica.kernel.user_memory.incognito", lambda: False)
    monkeypatch.setattr(ps, "detect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    assert srv._skill_after_turn("non ho capito la Lezione 14", []) == (None, None)


def test_the_shape_is_read_on_the_body_not_the_frontmatter_or_the_links():
    """A nucleated note's YAML lists and its Relations bullets are not items to
    compare: AdaGrad, an algorithm written in inline formulas, is a diagram."""
    adagrad = ("---\nrelated:\n  - \"[[A]]\"\n  - \"[[B]]\"\n  - \"[[C]]\"\ntags:\n  - dl\nsources:\n  - \"x.md\"\n---\n"
               "# AdaGrad\n\nRichiede $\\epsilon$, $\\theta$, $\\delta$.\nAccumula $r \\leftarrow r + g \\odot g$.\n"
               "## Relations\n- instance of [[A]]\n- uses [[B]]\n- part of [[C]]\n- see [[D]]\n## Sources\n[[x]]\n")
    assert offer.shape_forms(adagrad)[0] == "diagram"
    hub = "# Hub\n\nVedi [[A]] e [[B]].\n## Relations\n- [[C]]\n- [[D]]\n- [[E]]\n- [[F]]\n"
    assert offer.shape_forms(hub) == ["map"]


def _validate_script():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "validate_study_offer", Path(__file__).resolve().parent.parent / "scripts" / "validate_study_offer.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script


def test_the_confidence_branch_reads_a_log_with_confidence(tmp_path):
    """--eedi: calibration over the student's whole ledger, then a confident
    miss fires alone; a right answer given sure is counted apart."""
    script = _validate_script()
    meta, train = tmp_path / "data" / "metadata", tmp_path / "data" / "train_data"
    meta.mkdir(parents=True)
    train.mkdir(parents=True)
    (meta / "subject_metadata.csv").write_text("SubjectId,Name,ParentId,Level\n3,Maths,NULL,0\n10,Algebra,3,3\n")
    (meta / "question_metadata_task_3_4.csv").write_text('QuestionId,SubjectId\n1,"[3, 10]"\n')
    answers = [(i, True, 75.0) for i in range(1, 12)] + [(12, False, 100.0), (13, False, "")]
    (meta / "answer_metadata_task_3_4.csv").write_text(
        "AnswerId,DateAnswered,Confidence,GroupId,QuizId,SchemeOfWorkId\n"
        + "".join(f"{a},2020-01-01 10:{a:02d}:00.000,{c},1,1,1\n" for a, _ok, c in answers))
    (train / "train_task_3_4.csv").write_text(
        "QuestionId,UserId,AnswerId,IsCorrect,CorrectAnswer,AnswerValue\n"
        + "".join(f"1,7,{a},{int(ok)},1,1\n" for a, ok, _c in answers))
    r = script.eedi(str(tmp_path))
    assert r["students"] == 1 and r["answers"] == 13 and r["with_confidence"] == 12
    assert r["misses_fired"]["n"] == 0 and r["silent"]["n"] == 12
    assert r["blind_only"] == {"n": 1, "miss_rate": 1.0, "ci95": script.wilson(1, 1)}
    assert r["sure_right"]["n"] == 11 and r["sure_right"]["miss_rate"] == round(1 / 11, 3)


def test_an_aid_s_effect_is_read_off_its_randomized_experiments(tmp_path):
    script = _validate_script()
    rows = ["experiment_id,independent_measure_pair,condition,normalized_student_learning,student_prior_average_correctness"]
    rows += [f"E1,Answer Only vs. Hint,0,{y},0.5" for y in (0, 0, 1, 1)]
    rows += [f"E1,Answer Only vs. Hint,1,{y},0.5" for y in (1, 1, 2, 2)]
    rows += ["E2,No Choice vs. Choice,1,9,0.5"]
    path = tmp_path / "exp.csv"
    path.write_text("\n".join(rows) + "\n")
    r = script.experiments(str(path))
    assert list(r) == ["Answer Only vs. Hint"]
    assert r["Answer Only vs. Hint"]["all"]["d"] == round(1 / (1 / 3) ** 0.5, 3)
    assert r["Answer Only vs. Hint"]["all"]["experiments"] == 1 and r["Answer Only vs. Hint"]["all"]["students"] == 8


def test_jev_reads_what_the_word_list_misses(monkeypatch):
    """The list first, instant; Jev on a short message it did not match, once
    per message; nothing asked without a decision model or for a command."""
    import silica.agent.decide as dec

    calls = []

    def fake(state, questions, **kw):
        calls.append(state["message"])
        p = 0.9 if "perso" in state["message"] else 0.2
        return {"lost": {"type": "choice", "choice": "yes" if p > 0.5 else "no", "probabilities": {"yes": p, "no": 1 - p}}}

    monkeypatch.setattr(dec, "decide", fake)
    monkeypatch.setattr("silica.config.CONFIG.decision_model", "typesafe/jev-1.13-20260917")
    offer._jev_lost.cache_clear()
    assert offer.says_lost("mi sono perso sul duale")
    assert not offer.says_lost("cos'è il duale?")
    assert offer.says_lost("non ho capito il duale")            # the list, no call
    offer.says_lost("mi sono perso sul duale")
    assert calls == ["mi sono perso sul duale", "cos'è il duale?"]
    assert not offer.says_lost("/quiz il duale") and not offer.says_lost("perso " * 60)
    monkeypatch.setattr("silica.config.CONFIG.decision_model", "")
    offer._jev_lost.cache_clear()
    assert not offer.says_lost("mi sono perso sul duale")       # no decision model: the list alone
