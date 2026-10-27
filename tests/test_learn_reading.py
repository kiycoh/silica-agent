# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A plan step read as one explanation (ADR-0132): sections grouped and put
in the order of their prerequisites, each checked against its notes, the
reader's words kept apart from the ledger, the practice with its notes in
turn."""
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
ORDER = list(NOTES)
PLAN = ("---\ntype: plan\nscope: steps\ntarget: Programma\nreach: relational\ncreated: '2026-10-20'\nby: '2026-11-10'\n---\n"
        "# Esame\n\n## Steps\n- [ ] Reti feedforward: " + ", ".join(f"[[{t}]]" for t in ORDER) + " · 4 h\n\n## Sessions\n")

ORDER_REPLY = """SECTION: Il neurone e la sua uscita
NOTES: Neurone artificiale ; Funzione gradino
NEEDS: none
**SECTION:** Discesa del gradiente
NOTES: Gradiente discendente
NEEDS: Condizioni per massimi e minimi
SECTION: Massimi e minimi
NOTES: Condizioni per massimi e minimi ; Una nota che non esiste
NEEDS: none"""


def section_reply(title: str) -> str:
    if "neurone" in title.lower():
        return ("DEFINITION: Il neurone artificiale calcola la somma pesata degli ingressi più un bias. "
                "Fu inventato in una notte di tempesta da un poeta.\n"
                "MECHANISM: La somma pesata passa alla funzione di attivazione che dà l'uscita del neurone.\n"
                "$$z = \\sum_{i} w_i x_i + b$$\n"
                "EXAMPLE: Con x = (1, 0) e w = (0,6; 0,4), b = -0,5 si ha z = 0,1.\n"
                "CONTRAST: Il gradino non rispetta il modello biologico, che risponde in modo continuo.\n"
                "GUESS: Che cosa calcola il neurone con i suoi ingressi?\n"
                "ANSWER: Il neurone artificiale calcola la somma pesata degli ingressi più un bias.\n"
                "OWN: Spiega con parole tue a che cosa servono i pesi.")
    if "minimi" in title.lower():
        return ("DEFINITION: Un punto stazionario è un minimo se l'Hessiano è definito positivo.\n"
                "MECHANISM: Nel punto stazionario il gradiente della funzione si annulla del tutto.\n"
                "EXAMPLE: f(x) = x² ha gradiente nullo in 0 e derivata seconda 2.\n"
                "CONTRAST: none\nGUESS: Quando un punto stazionario è un minimo?\n"
                "ANSWER: Un punto stazionario è un minimo se l'Hessiano è definito positivo.\n"
                "OWN: Spiega che cosa dice l'Hessiano.")
    return ("**DEFINITION:** Il gradiente discendente aggiorna i pesi nella direzione opposta al gradiente.\n"
            "MECHANISM: Serve una funzione di errore differenziabile per calcolare il gradiente.\n"
            "EXAMPLE: Con un passo 0,1 il peso scende da 1 a 0,8.\n"
            "CONTRAST: none\n"
            "GUESS: In che direzione si muovono i pesi?\n"
            "ANSWER: direzione opposta al gradiente\n"
            "OWN: Spiega perché serve un errore differenziabile.")


def fake_ask(prompt: str, max_tokens: int) -> str:
    if prompt.startswith("These are the notes of one study step"):
        return ORDER_REPLY
    if prompt.startswith("These are the sections of one explanation"):
        return "1: none\n2: none\n3: 2"
    title = prompt.split('The section is "', 1)[1].split('"', 1)[0]
    return section_reply(title)


@pytest.fixture
def step_vault(tmp_vault, tmp_path, monkeypatch):
    import silica.kernel.report.quiz as quiz
    from silica.kernel.write import undo_journal

    monkeypatch.setattr(quiz, "log_path", lambda: tmp_path / "quiz.jsonl")
    monkeypatch.setattr(reading, "_log", lambda: tmp_path / "readings.jsonl")
    monkeypatch.setattr(undo_journal, "_DEFAULT_JOURNAL_PATH", tmp_path / "undo.db", raising=False)
    for title, body in NOTES.items():
        tmp_vault.note(f"Course/{title}.md", f"---\ntype: Note\n---\n\n# {title}\n\n{body}\n## Sources\n"
                                              "[^L16]: [[sources/Lezione 16]]\n[^L17]: [[sources/Lezione 17]]\n")
    # the slides hold the notes' claims, so Learn has grounded lines to ask about
    for leaf, tag in (("Lezione 16", "[^L16]"), ("Lezione 17", "[^L17]")):
        said = [ln.replace(tag, "") for body in NOTES.values() for ln in body.splitlines() if tag in ln]
        tmp_vault.note(f"sources/{leaf}.md", "---\ntype: Source\n---\n\n" + "\n\n".join(said) + "\n")
    tmp_vault.note("Plans/Esame.md", PLAN)
    return tmp_vault


# ------------------------------------------------------------------ the order

def test_a_section_moves_up_only_when_a_prerequisite_forces_it():
    raw = reading.parse_sections(ORDER_REPLY)
    out = reading.order_sections(ORDER, raw)
    assert out["order"] == "prerequisites"
    assert [s["title"] for s in out["sections"]] == ["Il neurone e la sua uscita", "Massimi e minimi", "Discesa del gradiente"]
    moved = out["sections"][1]
    assert moved["notes"] == ["Condizioni per massimi e minimi"] and moved["needed_by"] == ["Gradiente discendente"]
    assert out["sections"][0]["needed_by"] == [] and out["sections"][2]["needs"] == ["Condizioni per massimi e minimi"]


def test_a_cycle_keeps_the_lecture_order_and_says_so():
    raw = [{"title": "A", "notes": ["Neurone artificiale"], "needs": ["Funzione gradino"]},
           {"title": "B", "notes": ["Funzione gradino"], "needs": ["Neurone artificiale"]}]
    out = reading.order_sections(ORDER[:2], raw)
    assert out["order"] == "lecture" and [s["title"] for s in out["sections"]] == ["A", "B"]
    assert all(not s["needed_by"] for s in out["sections"])


def test_only_the_steps_notes_count_each_once_and_a_note_left_out_stands_alone():
    raw = [{"title": "Uno", "notes": ["Funzione gradino", "Neurone artificiale", "Funzione gradino", "Inventata"], "needs": []}]
    out = reading.order_sections(ORDER, raw)
    titles = [s["title"] for s in out["sections"]]
    assert titles[0] == "Uno" and out["sections"][0]["notes"] == ["Funzione gradino", "Neurone artificiale"]
    assert titles[1:] == ["Gradiente discendente", "Condizioni per massimi e minimi"]
    assert sum(len(s["notes"]) for s in out["sections"]) == len(ORDER)


def test_a_section_of_too_many_notes_splits_in_its_own_order():
    many = [f"N{i}" for i in range(10)]
    out = reading.order_sections(many, [{"title": "Tutto", "notes": many, "needs": []}])
    assert [s["notes"] for s in out["sections"]] == [many[:4], many[4:8], many[8:]]
    assert [s["title"] for s in out["sections"]] == ["Tutto", "N4", "N8"]


def test_needs_named_by_number_in_a_second_call_move_a_section():
    """The local 9B wrote "NEEDS: none" everywhere when asked to group and
    order at once (2026-10-26); asked by number, a need moves its section."""
    assert reading.parse_needs("1: none\n**2:** 3, 2, 9\nSection 3: 1\nnoise", 3) == {1: [2], 2: [0]}
    raw = [{"title": t, "notes": [t], "needs": []} for t in ORDER]
    first = reading.order_sections(ORDER, raw)
    assert [s["title"] for s in first["sections"]] == ORDER
    moved = reading.add_needs(first, ORDER, lambda p, m: "3: 4")
    assert [s["title"] for s in moved["sections"]] == [ORDER[0], ORDER[1], ORDER[3], ORDER[2]]
    assert moved["sections"][2]["needed_by"] == [ORDER[2]]
    assert reading.add_needs(first, ORDER, lambda p, m: 1 / 0) is first


def test_no_reply_is_one_section_a_note_in_lecture_order():
    out = reading.order_sections(ORDER, [])
    assert [s["notes"] for s in out["sections"]] == [[t] for t in ORDER]


# ------------------------------------------------------------------ a section

def test_fields_read_through_bold_labels_and_keep_their_lines():
    f = reading.parse_fields("**DEFINITION:** uno\n- MECHANISM: passo 1\npasso 2\nGUESS: perché?")
    assert f == {"DEFINITION": "uno", "MECHANISM": "passo 1\npasso 2", "GUESS": "perché?"}


def test_a_sentence_the_notes_do_not_hold_is_marked_and_kept():
    held = NOTES["Neurone artificiale"]
    text, n = reading.mark_unheld("Il neurone artificiale calcola la somma pesata degli ingressi più un bias. "
                                  "Fu inventato in una notte di tempesta da un poeta.", held)
    assert n == 1 and text.endswith(reading.NOT_HELD) and text.startswith("Il neurone artificiale calcola")
    text, n = reading.mark_unheld("$$z = \\sum_{i} w_i x_i + b$$", held)
    assert n == 0
    text, n = reading.mark_unheld("$$z = \\prod_{i} w_i$$", held)
    assert n == 1


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


def test_the_answer_is_highlighted_exactly_or_by_its_words():
    t, hit = reading.highlight_answer("Prima frase. Il gradiente discendente aggiorna i pesi nella direzione opposta al gradiente.",
                                      "aggiorna i pesi nella direzione opposta al gradiente")
    assert hit and "==aggiorna i pesi nella direzione opposta al gradiente==" in t
    t, hit = reading.highlight_answer("Il gradiente discendente aggiorna i pesi nella direzione opposta.",
                                      "I pesi vanno nella direzione opposta, aggiornati dal gradiente discendente")
    assert hit and t.startswith("==Il gradiente discendente")
    t, hit = reading.highlight_answer("Niente in comune qui.", "Gradiente discendente e pesi")
    assert not hit and "==" not in t


# ------------------------------------------------------------------ the note

def test_a_reading_is_written_beside_the_plan_read_back_and_never_material(step_vault):
    from silica.kernel.report import learner

    out = reading.write_reading("Plans/Esame.md", 0, ask=fake_ask)
    assert out["path"] == "Plans/Esame - readings/01 Reti feedforward.md"
    assert out["order"] == "prerequisites" and out["sections"] == 3 and out["marked"] == 1
    st = reading.state("Plans/Esame.md", 0)
    assert st["exists"] and not st["stale"] and st["questions"] == reading.SESSION_MIN
    secs = st["sections"]
    assert [s["title"] for s in secs] == ["Il neurone e la sua uscita", "Massimi e minimi", "Discesa del gradiente"]
    first = secs[0]
    assert first["notes"] == ["Neurone artificiale", "Funzione gradino"]
    assert first["guess"] == "Che cosa calcola il neurone con i suoi ingressi?"
    assert "==Il neurone artificiale calcola la somma pesata degli ingressi più un bias.==" in first["definition"]
    assert reading.NOT_HELD in first["definition"] and "\n" in first["mechanism"]
    assert first["example"].startswith("Con x") and first["own"].startswith("Spiega")
    assert secs[1]["needed_by"] == ["Gradiente discendente"]
    meta = learner._notes_meta(plans._vault())[out["path"]]
    assert meta["kind"] == "reading" and not learner.is_material(meta)
    text = (plans._vault() / out["path"]).read_text(encoding="utf-8")
    assert "type: reading" in text and "AI: true" in text and "notes_hash:" in text


def test_a_changed_note_makes_the_reading_stale_and_a_rewrite_starts_the_readers_words_over(step_vault):
    out = reading.write_reading("Plans/Esame.md", 0, ask=fake_ask)
    reading.record(out["path"], 1, "guess", "la somma pesata")
    reading.record(out["path"], 1, "own", "i pesi pesano gli ingressi")
    st = reading.state("Plans/Esame.md", 0)
    assert st["answers"]["1"]["guess"]["text"] == "la somma pesata" and st["answers"]["1"]["own"]
    step_vault.note("Course/Funzione gradino.md", "---\ntype: Note\n---\n\n# Funzione gradino\n\nCambiata del tutto.\n")
    assert reading.state("Plans/Esame.md", 0)["stale"]
    time.sleep(1.1)   # `written` is in whole seconds
    reading.write_reading("Plans/Esame.md", 0, ask=fake_ask)
    st = reading.state("Plans/Esame.md", 0)
    assert not st["stale"] and st["answers"] == {}


def test_a_failed_ordering_call_still_writes_one_section_a_note(step_vault):
    def ask(prompt, max_tokens):
        if prompt.startswith("These are the notes"):
            raise RuntimeError("provider down")
        return section_reply("neurone")

    out = reading.write_reading("Plans/Esame.md", 0, ask=ask)
    assert out["order"] == "lecture" and out["sections"] == len(ORDER)


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
    server._reset_session()
    learn_routes._reset()
    return TestClient(server.app, headers={"Origin": "http://testserver"})


def test_the_routes_write_a_reading_keep_the_readers_words_and_start_its_practice(client):
    c = client
    st = c.get("/learn/reading", params={"plan": "Plans/Esame.md", "step": 0}).json()
    assert st["exists"] is False and st["label"] == "Reti feedforward" and len(st["notes"]) == 4
    job = c.post("/learn/reading/write", json={"plan": "Plans/Esame.md", "step": 0}).json()
    assert job["state"] in ("running", "done")
    for _ in range(100):
        st = c.get("/learn/reading", params={"plan": "Plans/Esame.md", "step": 0}).json()
        if st["job"] and st["job"]["state"] != "running":
            break
        time.sleep(0.05)
    assert st["job"]["state"] == "done" and st["exists"] and len(st["sections"]) == 3
    r = c.post("/learn/reading/answer", json={"plan": "Plans/Esame.md", "step": 0, "section": 1, "kind": "guess",
                                             "text": "", "unknown": True})
    assert r.status_code == 200 and r.json()["unknown"] is True
    assert c.post("/learn/reading/answer", json={"plan": "Plans/Esame.md", "step": 0, "section": 1,
                                                "kind": "grade", "text": "x"}).status_code == 400
    st = c.get("/learn/reading", params={"plan": "Plans/Esame.md", "step": 0}).json()
    assert st["answers"]["1"]["guess"]["unknown"] is True
    s = c.post("/learn/session", json={"plan": "Plans/Esame.md", "step": 0}).json()
    assert s["origin"]["label"] == "Esame · Reti feedforward"
    paths = [it["path"] for it in s["items"]]
    assert paths and set(paths) <= {f"Course/{t}.md" for t in ORDER}
    for a, b in zip(paths, paths[1:]):
        assert a != b or all(x == a for x in paths[paths.index(b):])
    note = c.get("/learn/note", params={"path": "Course/Gradiente discendente.md"}).json()
    assert note["reading"] == {"plan": "Plans/Esame.md", "step": 0}
    from silica.kernel.report import quiz

    assert quiz.entries() == []   # a guess is never an answer in the ledger


def test_a_reading_for_a_step_that_is_not_there_is_refused(client):
    assert client.get("/learn/reading", params={"plan": "Plans/Esame.md", "step": 5}).status_code == 404
    assert client.post("/learn/reading/write", json={"plan": "Plans/Esame.md", "step": 5}).status_code == 400
    assert client.post("/learn/reading/write", json={"plan": "Plans/Nope.md", "step": 0}).status_code == 404


# ------------------------------------------------------------------ the job survives a restart (ADR-0135 point 1)

def _until_written(c):
    for _ in range(100):
        st = c.get("/learn/reading", params={"plan": "Plans/Esame.md", "step": 0}).json()
        if st["job"] and st["job"]["state"] != "running":
            return st
        time.sleep(0.05)
    return st


def test_a_reading_job_is_a_record_the_chat_tray_never_lists_and_a_restart_interrupts(client):
    from silica.ui.web import jobs

    c = client
    assert c.post("/learn/reading/write", json={"plan": "Plans/Esame.md", "step": 0}).json()["state"] in ("running", "done")
    st = _until_written(c)
    assert st["job"]["state"] == "done" and st["job"]["path"]
    recs = [r for r in jobs._all() if r.get("kind") == "reading"]
    assert len(recs) == 1 and recs[0]["plan"] == "Plans/Esame.md" and recs[0]["step"] == 0
    assert c.get("/jobs").json()["jobs"] == []            # the chat's tray: what waits on the next message
    assert c.post(f"/jobs/{recs[0]['id']}/retry").status_code == 409

    # A process killed mid-write: the record says running and nothing runs it.
    reading._write_job(dict(recs[0], state="running", finished=None))
    assert c.get("/jobs/running").json()["jobs"][0]["kind"] == "reading"   # the release drain sees it
    assert jobs.recover() == 1
    st = c.get("/learn/reading", params={"plan": "Plans/Esame.md", "step": 0}).json()
    assert st["job"]["state"] == "error" and st["job"]["error"] == reading.INTERRUPTED

    # Writing it again replaces the stopped record: one record a step.
    c.post("/learn/reading/write", json={"plan": "Plans/Esame.md", "step": 0})
    assert _until_written(c)["job"]["state"] == "done"
    assert len([r for r in jobs._all() if r.get("kind") == "reading"]) == 1
