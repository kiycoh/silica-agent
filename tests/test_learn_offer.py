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
    assert [c["command"] for c in o["commands"]] == ['/quiz "ML/SVM.md" --n=3', '/diagram "SVM"']


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

    # a turn that read nothing still has the note the reader named
    monkeypatch.setattr(srv, "read_refs", lambda messages: [])
    o = srv._study_offer("Non ho capito cosa sono i punti di supporto in una SVM", [])
    assert o and o["note"] == "ML/Punti di supporto.md"
    assert srv._study_offer("cosa sono i punti di supporto?", []) is None   # no word saying it was unclear
