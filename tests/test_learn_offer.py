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
    """The research report's rule (difficulty-detection-and-study-aids, 8):
    confidence is read after CALIBRATION_MIN eligible answers; a miss while
    sure is a blind spot and gets a contrast table first; right while unsure
    gets nothing, the scheduler brings it back."""
    from silica.kernel.learn.mastery import CALIBRATION_MIN

    history = [_e(f"ML/N{i}.md", f"2026-10-0{i % 9 + 1}", True, conf=70, grader="model") for i in range(CALIBRATION_MIN)]
    sure_wrong = [_e("ML/SVM.md", "2026-10-21", False, conf=90, grader="model")]
    assert offer.struggles(["ML/SVM.md"], sure_wrong) == {}                     # not calibrated yet
    assert offer.struggles(["ML/SVM.md"], history + sure_wrong) == {"ML/SVM.md": "you were sure about SVM and missed it"}
    unsure_right = [_e("ML/SVM.md", "2026-10-21", True, conf=25, grader="model")]
    assert offer.struggles(["ML/SVM.md"], history + unsure_right) == {}
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
