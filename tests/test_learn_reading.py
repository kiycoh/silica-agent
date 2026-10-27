# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A plan step read as a card per note (ADR-0132 as amended by ADR-0134):
the cards in the order of their prerequisites, a lecture's outline at its
place with no call, each card's summary checked against its notes and closed
by a question whose answer the next note holds, the reader's answers kept
apart from the ledger, the practice with its notes in turn."""
from __future__ import annotations

import time

import pytest

import silica.tools.notes  # noqa: F401 — the write gate registers on import
from silica.kernel.learn import pick, plans, reading

NOTES = {
    "Neurone artificiale": "Il neurone artificiale calcola la somma pesata degli ingressi più un bias.[^L16]\n\n"
                           "La somma pesata passa alla funzione di attivazione che dà l'uscita del neurone.[^L16]\n\n"
                           "$$z = \\sum_{i} w_i x_i + b$$\n",
    "Funzione gradino": "La funzione gradino produce un'uscita binaria a seconda della soglia sulla somma.[^L16]\n\n"
                        "Il gradino non rispetta il modello biologico, che risponde in modo continuo.[^L16]\n",
    "Gradiente discendente": "Il gradiente discendente aggiorna i pesi nella direzione opposta al gradiente.[^L16]\n\n"
                             "Serve una funzione di errore differenziabile per calcolare il gradiente.[^L16]\n",
    "Condizioni per massimi e minimi": "Un punto stazionario è un minimo se l'Hessiano è definito positivo.[^L17]\n\n"
                                       "Nel punto stazionario il gradiente della funzione si annulla del tutto.[^L17]\n",
}
SPINE = "Lezione 5"
SPINE_TEXT = ("---\ntype: Note\nsection: (outline)\n---\n\n# Lezione 5\n\n> Il neurone e la sua discesa\n\n"
              "1. [[Neurone artificiale]]: il neurone.\n2. [[Gradiente discendente]]: la discesa.\n")
ORDER = list(NOTES)
# the spine sits third in the lectures, between the neuron and the descent
STEP = ORDER[:2] + [SPINE] + ORDER[2:]
PLAN = ("---\ntype: plan\nscope: steps\ntarget: Programma\nreach: relational\ncreated: '2026-10-20'\nby: '2026-11-10'\n---\n"
        "# Esame\n\n## Steps\n- [ ] Reti feedforward: " + ", ".join(f"[[{t}]]" for t in STEP) + " · 4 h\n\n## Sessions\n")

# by number, over the four notes that are not the spine: the descent needs the minima
NEEDS_REPLY = "1: none\n2: none\n**3:** 4\n4: none"


def card_reply(title: str) -> str:
    if title == "Neurone artificiale":
        return ("OPENING: none\n"
                "DEFINITION: Il neurone artificiale calcola la somma pesata degli ingressi più un bias [[Neurone artificiale]]. "
                "Fu inventato in una notte di tempesta da un poeta [[Wikipedia]].\n"
                "MECHANISM: La somma pesata passa alla funzione di attivazione che dà l'uscita del neurone [[Funzione gradino]].\n"
                "$$\nz = \\sum_{i} w_i x_i + b\n$$\n"
                "EXAMPLE: Con x = (1, 0) e w = (0,6; 0,4), b = -0,5 si ha z = 0,1.\n"
                "QUESTION: Che cosa fa una funzione a soglia con la somma pesata?\n"
                "ANSWER: La funzione gradino produce un'uscita binaria a seconda della soglia sulla somma.")
    if title == "Funzione gradino":
        return ("OPENING: L'uscita del neurone passa per una funzione [[Neurone artificiale]].\n"
                "DEFINITION: La funzione gradino produce un'uscita binaria a seconda della soglia sulla somma.\n"
                "MECHANISM: Il gradino non rispetta il modello biologico, che risponde in modo continuo.\n"
                "EXAMPLE: Con soglia 0 e somma 0,1 l'uscita è 1.\n"
                "QUESTION: Che cosa succede al gradiente in un punto stazionario?\n"
                "ANSWER: In un punto stazionario il gradiente vale zero.")
    if title == "Condizioni per massimi e minimi":
        return ("DEFINITION: Un punto stazionario è un minimo se l'Hessiano è definito positivo.\n"
                "MECHANISM: Nel punto stazionario il gradiente della funzione si annulla del tutto.\n"
                "EXAMPLE: f(x) = x² ha gradiente nullo in 0 e derivata seconda 2.\n"
                "QUESTION: Che cosa serve per calcolare il gradiente di un errore?\n"
                "ANSWER: Serve una funzione di errore differenziabile per calcolare il gradiente.")
    return ("**DEFINITION:** Il gradiente discendente aggiorna i pesi nella direzione opposta al gradiente.\n"
            "MECHANISM: Serve una funzione di errore differenziabile per calcolare il gradiente.\n"
            "EXAMPLE: Con un passo 0,1 il peso scende da 1 a 0,8.\n"
            "QUESTION: In che direzione si muovono i pesi?\n"
            "ANSWER: «Il gradiente discendente aggiorna i pesi nella direzione  opposta al gradiente»")


PROMPTS: list[str] = []


def fake_ask(prompt: str, max_tokens: int) -> str:
    PROMPTS.append(prompt)
    if prompt.startswith("These are the notes of one study step"):
        return NEEDS_REPLY
    title = prompt.split("The card summarises this note:\n### ", 1)[1].split("\n", 1)[0]
    return card_reply(title)


@pytest.fixture
def step_vault(tmp_vault, tmp_path, monkeypatch):
    import silica.kernel.report.quiz as quiz
    from silica.kernel.write import undo_journal

    monkeypatch.setattr(quiz, "log_path", lambda: tmp_path / "quiz.jsonl")
    monkeypatch.setattr(reading, "_log", lambda: tmp_path / "readings.jsonl")
    monkeypatch.setattr(undo_journal, "_DEFAULT_JOURNAL_PATH", tmp_path / "undo.db", raising=False)
    PROMPTS.clear()
    for title, body in NOTES.items():
        tmp_vault.note(f"Course/{title}.md", f"---\ntype: Note\n---\n\n# {title}\n\n{body}\n## Sources\n"
                                              "[^L16]: [[sources/Lezione 16]]\n[^L17]: [[sources/Lezione 17]]\n")
    tmp_vault.note(f"Course/{SPINE}.md", SPINE_TEXT)
    # the slides hold the notes' claims, so Learn has grounded lines to ask about
    for leaf, tag in (("Lezione 16", "[^L16]"), ("Lezione 17", "[^L17]")):
        said = [ln.replace(tag, "") for body in NOTES.values() for ln in body.splitlines() if tag in ln]
        tmp_vault.note(f"sources/{leaf}.md", "---\ntype: Source\n---\n\n" + "\n\n".join(said) + "\n")
    tmp_vault.note("Plans/Esame.md", PLAN)
    return tmp_vault


def _notes():
    return reading.step_notes(plans.find_plan("Plans/Esame.md"), 0)


# ------------------------------------------------------------------ the order

def test_a_card_moves_up_only_when_a_prerequisite_forces_it_and_a_spine_keeps_its_place(step_vault):
    out = reading.order_cards(_notes(), fake_ask)
    assert out["order"] == "prerequisites"
    assert [c["title"] for c in out["cards"]] == ["Neurone artificiale", "Funzione gradino", SPINE,
                                                 "Condizioni per massimi e minimi", "Gradiente discendente"]
    moved = out["cards"][3]
    assert moved["needed_by"] == ["Gradiente discendente"] and out["cards"][4]["needs"] == ["Condizioni per massimi e minimi"]
    assert out["cards"][2]["spine"] and not any(c["spine"] for i, c in enumerate(out["cards"]) if i != 2)
    # the needs call lists the four notes by number, the spine left out, each with its opening
    assert len(PROMPTS) == 1 and SPINE not in PROMPTS[0] and "4. Condizioni per massimi e minimi: Un punto" in PROMPTS[0]


def test_no_model_or_a_failed_needs_call_is_the_lecture_order(step_vault):
    for ask in (None, lambda p, m: 1 / 0):
        out = reading.order_cards(_notes(), ask)
        assert out["order"] == "lecture" and [c["title"] for c in out["cards"]] == STEP


def test_a_cycle_keeps_the_lecture_order_and_says_so():
    raw = [{"title": "A", "notes": ["Neurone artificiale"], "needs": ["Funzione gradino"]},
           {"title": "B", "notes": ["Funzione gradino"], "needs": ["Neurone artificiale"]}]
    out = reading.order_sections(ORDER[:2], raw)
    assert out["order"] == "lecture" and [s["title"] for s in out["sections"]] == ["A", "B"]
    assert all(not s["needed_by"] for s in out["sections"])


def test_only_the_steps_notes_count_each_once():
    raw = [{"title": t, "notes": [t, "Inventata"], "needs": ["Inventata"]} for t in ORDER + ORDER[:1]]
    out = reading.order_sections(ORDER, raw)
    assert [s["notes"] for s in out["sections"]] == [[t] for t in ORDER]


def test_needs_are_read_by_number():
    assert reading.parse_needs("1: none\n**2:** 3, 2, 9\nNote 3: 1\nnoise", 3) == {1: [2], 2: [0]}


# ------------------------------------------------------------------ a card

def test_fields_read_through_bold_labels_and_keep_their_lines():
    f = reading.parse_fields("**DEFINITION:** uno\n- MECHANISM: passo 1\npasso 2\nQUESTION: perché?")
    assert f == {"DEFINITION": "uno", "MECHANISM": "passo 1\npasso 2", "QUESTION": "perché?"}


def test_a_link_stays_only_when_it_names_a_note_of_the_step():
    titles = ["Neurone artificiale", "Funzione gradino"]
    text = "Uno [[neurone artificiale|il neurone]], due [[Wikipedia]], tre [[Altro|testo]], quattro [[Funzione gradino#Uscita]]."
    assert reading.keep_step_links(text, titles) == ("Uno [[Neurone artificiale]], due Wikipedia, tre testo, "
                                                     "quattro [[Funzione gradino]].")


def test_a_sentence_the_notes_do_not_hold_is_marked_and_kept():
    held = NOTES["Neurone artificiale"]
    text, n = reading.mark_unheld("Il neurone artificiale calcola la somma pesata degli ingressi più un bias. "
                                  "Fu inventato in una notte di tempesta da un poeta.", held)
    assert n == 1 and text.endswith(reading.NOT_HELD) and text.startswith("Il neurone artificiale calcola")
    assert reading.mark_unheld("$$z = \\sum_{i} w_i x_i + b$$", held)[1] == 0
    assert reading.mark_unheld("$$z = \\prod_{i} w_i$$", held)[1] == 1


def test_a_formula_over_three_lines_is_one_line_and_held_when_the_note_has_it():
    """The gradient update copied verbatim from its note was marked: its line
    had lost its `$$` fences and was judged by LaTeX words (2026-10-26)."""
    text = reading.collapse_display("Prima.\n$$\nz = \\sum_{i} w_i x_i + b\n$$\nDopo.")
    assert text == "Prima.\n$$z = \\sum_{i} w_i x_i + b$$\nDopo."
    marked, n = reading.mark_unheld(text, NOTES["Neurone artificiale"])
    assert n == 0 and reading.NOT_HELD not in marked


def test_a_formula_keeping_a_chains_ends_is_held_and_one_off_the_chain_is_not():
    note = "$$\\frac{\\partial f}{\\partial w_{j}} = \\frac{\\partial \\sum_r w_r a_r}{\\partial w_j} = a_{j}$$"
    assert reading.mark_unheld("$\\frac{\\partial f}{\\partial w_j} = a_j$", note)[1] == 0
    assert reading.mark_unheld("$\\frac{\\partial f}{\\partial w_j} = b_j$", note)[1] == 1
    assert reading.mark_unheld("$a_j = \\frac{\\partial f}{\\partial w_j}$", note)[1] == 1


def test_an_answer_is_shown_only_when_the_note_holds_it():
    note = "Il gradiente **discendente** aggiorna i pesi nella direzione opposta al [[Gradiente|gradiente]].\n\n$$w' = w - \\eta \\nabla E$$"
    assert reading.answer_held("«Il gradiente discendente aggiorna i pesi nella direzione  opposta al gradiente»", note)
    assert reading.answer_held("$w' = w - \\eta\\nabla E$.", note)
    assert not reading.answer_held("I pesi vanno nella direzione opposta al gradiente.", note)
    assert not reading.answer_held("pesi", note)


def test_a_card_is_written_from_its_note_and_asks_toward_the_next_one(step_vault):
    cards = reading.order_cards(_notes())["cards"]
    one = reading.write_card(0, cards, "Italian", fake_ask)
    assert one["answer"] == "La funzione gradino produce un'uscita binaria a seconda della soglia sulla somma."
    assert one["answer_from"] == "Funzione gradino" and one["question"].startswith("Che cosa fa")
    assert "[[Neurone artificiale]]" in one["text"] and "[[Funzione gradino]]" in one["text"]
    assert "[[Wikipedia]]" not in one["text"] and "poeta Wikipedia" in one["text"]
    # one sentence the notes do not hold, and none for a link's title alone
    assert one["marked"] == 1 and one["text"].count(reading.NOT_HELD) == 1
    assert "$$z = \\sum_{i} w_i x_i + b$$" in one["text"]
    # the next card that is not a spine: the call saw "Gradiente discendente", not the outline
    two = reading.write_card(1, cards, "Italian", fake_ask)
    assert f"### {SPINE}" not in PROMPTS[-1] and "### Gradiente discendente" in PROMPTS[-1]
    assert two["answer_from"] == "Gradiente discendente" and two["answer"] == ""   # paraphrased: not shown
    last = reading.write_card(4, cards, "Italian", fake_ask)
    assert "This is the last card" in PROMPTS[-1] and last["answer_from"] == "Condizioni per massimi e minimi"


def test_a_spine_card_is_its_lede_and_points_with_no_call(step_vault):
    cards = reading.order_cards(_notes())["cards"]
    sp = reading.spine_card(cards[2])
    assert sp["spine"] and sp["text"] == "Il neurone e la sua discesa"
    assert sp["points"] == ["Neurone artificiale", "Gradiente discendente"] and not sp["question"]


# ------------------------------------------------------------------ a card the reader writes

NOTE = ("---\ntype: Note\n---\n\n# Norme\n\n<!-- silica: valid_from=2026-03-03 -->\n\n"
        "> Una norma vettoriale è una funzione che misura la lunghezza di un vettore, con proprietà di positività.\n\n"
        "> Le norme misurano la grandezza di un vettore.\n\n"
        "A volte serve misurare la dimensione di un vettore. Nel [[Machine learning|machine learning]] usiamo una norma. "
        "La norma L1 è spesso preferita per favorire la sparsità. La norma L2 è detta norma euclidea.\n")


def test_a_card_mostly_copied_from_its_note_is_a_copy_and_a_rewrite_is_not():
    copied = ("Nel machine learning usiamo una **norma**. La norma L1 è spesso preferita per favorire la sparsità.\n"
              "La norma L2 è detta norma euclidea. Io la uso per i vettori lunghi.")
    assert reading.copy_share(copied, NOTE) == (3, 4) and reading.is_copy(copied, NOTE)
    own = ("Una norma dice quanto è lungo un vettore. La L1 somma i valori assoluti e spinge verso vettori sparsi. "
           "La norma L2 è detta norma euclidea.")
    assert reading.copy_share(own, NOTE) == (1, 3) and not reading.is_copy(own, NOTE)
    assert not reading.is_copy("", NOTE)


def test_the_central_claim_is_the_first_lede_line_and_a_card_must_say_it():
    lede = reading.note_lede(NOTE, is_text=True)
    assert lede.startswith("Una norma vettoriale è una funzione") and lede.endswith("positività.")
    assert reading.holds_lede("La norma vettoriale è una funzione: misura la lunghezza di un vettore, ed è positiva.", lede)
    assert not reading.holds_lede("La norma L1 favorisce la sparsità dei pesi.", lede)
    assert reading.note_lede("# T\n\nPrima frase del testo. Seconda frase.", is_text=True) == "Prima frase del testo."


# ------------------------------------------------------------------ the note

def test_a_reading_is_written_beside_the_plan_read_back_and_never_material(step_vault):
    from silica.kernel.report import learner

    out = reading.write_reading("Plans/Esame.md", 0, ask=fake_ask)
    assert out["path"] == "Plans/Esame - readings/01 Reti feedforward.md"
    assert out["order"] == "prerequisites" and out["cards"] == 5 and out["questions"] == 4 and out["answered"] == 3
    # one needs call and one call a card that is not a spine
    assert len(PROMPTS) == 1 + 4
    st = reading.state("Plans/Esame.md", 0)
    assert st["exists"] and not st["stale"] and st["questions"] == reading.session_size(5)
    cards = st["cards"]
    assert [c["title"] for c in cards] == ["Neurone artificiale", "Funzione gradino", SPINE,
                                          "Condizioni per massimi e minimi", "Gradiente discendente"]
    first = cards[0]
    assert first["note"] == "Neurone artificiale" and first["question"].startswith("Che cosa fa")
    assert first["answer"].startswith("La funzione gradino") and first["answer_from"] == "Funzione gradino"
    assert reading.NOT_HELD in first["text"] and first["example"].startswith("Con x")
    assert cards[1]["question"] and not cards[1]["answer"]
    assert cards[2]["spine"] and cards[2]["points"] == ["Neurone artificiale", "Gradiente discendente"]
    assert cards[3]["needed_by"] == ["Gradiente discendente"]
    meta = learner._notes_meta(plans._vault())[out["path"]]
    assert meta["kind"] == "reading" and not learner.is_material(meta)
    text = (plans._vault() / out["path"]).read_text(encoding="utf-8")
    assert "type: reading" in text and "AI: true" in text and "notes_hash:" in text
    assert "> [!question] To finish the card" in text and "Do not confuse" not in text


def test_a_reading_written_before_the_cards_still_reads_as_cards():
    old = ("---\ntype: reading\n---\n# Step\n\n2 sections.\n\n## 1. Il neurone\n\n"
           "Notes: [[Neurone artificiale]], [[Funzione gradino]]\n\n> [!question] Before you read\n> Che cosa calcola?\n\n"
           "**Definition.** ==Il neurone calcola la somma.== \n\n**Mechanism.** Passo uno.\nPasso due.\n\n"
           "**Example** (worked by Silica, not from the notes). x = 1.\n\n**Do not confuse.** Non è il gradino.\n\n"
           "> [!example] In your own words\n> Spiega i pesi.\n\n## 2. La discesa\n\nNotes: [[Gradiente discendente]]\n\n"
           "**Definition.** Scende.\n")
    cards = reading.parse(old)
    assert [c["title"] for c in cards] == ["Il neurone", "La discesa"]
    c = cards[0]
    assert c["notes"] == ["Neurone artificiale", "Funzione gradino"] and c["note"] == "Neurone artificiale"
    assert c["text"].startswith("==Il neurone calcola la somma.==") and "Passo due." in c["text"]
    assert "Do not confuse. Non è il gradino." in c["text"] and c["example"] == "x = 1."
    assert not c["question"] and "Spiega" not in c["text"] and "Che cosa calcola" not in c["text"]
    assert cards[1]["text"] == "Scende."


def test_a_changed_note_makes_the_reading_stale_and_a_rewrite_starts_the_readers_answers_over(step_vault):
    out = reading.write_reading("Plans/Esame.md", 0, ask=fake_ask)
    reading.record(out["path"], 1, "seen", "")
    reading.record(out["path"], 1, "answer", "la soglia")
    reading.record(out["path"], 2, "seen", "")
    st = reading.state("Plans/Esame.md", 0)
    assert st["answers"]["1"]["answer"]["text"] == "la soglia" and set(st["answers"]["2"]) == {"seen"}
    with pytest.raises(ValueError):
        reading.record(out["path"], 1, "guess", "x")
    step_vault.note("Course/Funzione gradino.md", "---\ntype: Note\n---\n\n# Funzione gradino\n\nCambiata del tutto.\n")
    assert reading.state("Plans/Esame.md", 0)["stale"]
    time.sleep(1.1)   # `written` is in whole seconds
    reading.write_reading("Plans/Esame.md", 0, ask=fake_ask)
    st = reading.state("Plans/Esame.md", 0)
    assert not st["stale"] and st["answers"] == {}


# ------------------------------------------------------------------ the practice

def test_a_steps_questions_take_turns_between_notes():
    items = [{"path": p} for p in "AAAABBC"]
    out = pick.alternate_notes(items)
    seq = [it["path"] for it in out]
    assert sorted(seq) == sorted("AAAABBC") and [it["i"] for it in out] == list(range(1, 8))
    for a, b in zip(seq, seq[1:]):
        assert a != b or all(x == a for x in seq[seq.index(b):])
    assert seq[0] == "A"


# ------------------------------------------------------------------ the routes

@pytest.fixture
def client(step_vault, monkeypatch):
    from fastapi.testclient import TestClient

    from silica.kernel.learn import grade, items
    from silica.ui.web import learn_routes, server

    monkeypatch.setattr(items, "_llm", lambda *a, **k: ("", "stop"))
    monkeypatch.setattr(grade, "_llm", lambda *a, **k: ("", "stop"))
    monkeypatch.setattr(reading, "_ask", fake_ask)
    reading._JOBS.clear()
    server._reset_session()
    learn_routes._reset()
    return TestClient(server.app, headers={"Origin": "http://testserver"})


def test_the_routes_write_a_reading_keep_the_readers_words_and_start_its_practice(client):
    c = client
    st = c.get("/learn/reading", params={"plan": "Plans/Esame.md", "step": 0}).json()
    assert st["exists"] is False and st["label"] == "Reti feedforward" and len(st["notes"]) == 5
    job = c.post("/learn/reading/write", json={"plan": "Plans/Esame.md", "step": 0}).json()
    assert job["state"] in ("running", "done")
    for _ in range(100):
        st = c.get("/learn/reading", params={"plan": "Plans/Esame.md", "step": 0}).json()
        if st["job"] and st["job"]["state"] != "running":
            break
        time.sleep(0.05)
    assert st["job"]["state"] == "done" and st["exists"] and len(st["cards"]) == 5
    assert c.post("/learn/reading/answer", json={"plan": "Plans/Esame.md", "step": 0, "section": 1, "kind": "seen",
                                                "text": ""}).status_code == 200
    r = c.post("/learn/reading/answer", json={"plan": "Plans/Esame.md", "step": 0, "section": 1, "kind": "answer",
                                             "text": "", "unknown": True})
    assert r.status_code == 200 and r.json()["unknown"] is True
    assert c.post("/learn/reading/answer", json={"plan": "Plans/Esame.md", "step": 0, "section": 1,
                                                "kind": "guess", "text": "x"}).status_code == 400
    st = c.get("/learn/reading", params={"plan": "Plans/Esame.md", "step": 0}).json()
    assert st["answers"]["1"]["answer"]["unknown"] is True and st["answers"]["1"]["seen"]
    s = c.post("/learn/session", json={"plan": "Plans/Esame.md", "step": 0}).json()
    assert s["origin"]["label"] == "Esame · Reti feedforward"
    paths = [it["path"] for it in s["items"]]
    assert paths and set(paths) <= {f"Course/{t}.md" for t in STEP}
    for a, b in zip(paths, paths[1:]):
        assert a != b or all(x == a for x in paths[paths.index(b):])
    note = c.get("/learn/note", params={"path": "Course/Gradiente discendente.md"}).json()
    assert note["reading"] == {"plan": "Plans/Esame.md", "step": 0}
    from silica.kernel.report import quiz

    assert quiz.entries() == []   # a card's answer is never an answer in the ledger


def test_a_reading_for_a_step_that_is_not_there_is_refused(client):
    assert client.get("/learn/reading", params={"plan": "Plans/Esame.md", "step": 5}).status_code == 404
    assert client.post("/learn/reading/write", json={"plan": "Plans/Esame.md", "step": 5}).status_code == 400
    assert client.post("/learn/reading/write", json={"plan": "Plans/Nope.md", "step": 0}).status_code == 404
