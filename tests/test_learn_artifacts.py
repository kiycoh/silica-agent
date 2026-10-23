# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cards and slides stand on the notes they cite
(silica/kernel/learn/artifacts.py): a card that does not is dropped before the
deck is shown, a slide point that does not is named under the answer."""
from silica.kernel.learn import artifacts


SLACK = ("# Variabili slack\n\nLe variabili slack misurano la sovrapposizione tra le classi.\n"
         "$\\xi_i = \\max(0, \\gamma - y_i f(x_i))$\n")


def _reply(monkeypatch, *replies):
    """The model's replies, in order; the prompts it was given, kept."""
    seen = []
    queue = list(replies)
    monkeypatch.setattr(artifacts, "_card_reply", lambda prompt, retry=False: (seen.append((prompt, retry)), queue.pop(0))[1])
    return seen


def test_cards_are_parsed_checked_and_written_in_the_plugin_s_form(tmp_vault, monkeypatch):
    """A card is kept when its cited note holds its answer: one the note does
    not hold, one citing no note, a line with no answer half and a repeated
    question never reach the deck (cite-or-drop, research recommendation 1)."""
    tmp_vault.note("ML/Variabili slack di margine.md", SLACK)
    seen = _reply(monkeypatch,
                  "Cosa misurano le variabili slack? :: La sovrapposizione tra le classi [[Variabili slack di margine]]\n"
                  "Question: Come si definisce xi? Answer: $\\xi_i = \\max(0, \\gamma - y_i f(x_i))$ [[Variabili slack di margine]]\n"
                  "Chi le ha inventate? :: Vapnik nel 1963 a Mosca [[Variabili slack di margine]]\n"
                  "Cosa sono? :: Pesi della rete [[Nota che non esiste]]\n"
                  "Una variabile slack misura la sovrapposizione: [[Variabili slack di margine]]\n"
                  "Cosa misurano le variabili slack? :: La sovrapposizione [[Variabili slack di margine]]\n")
    md = artifacts.flashcards(["Variabili slack di margine"], n=8)
    assert md.startswith("## Flashcards: [[Variabili slack di margine]]")
    assert ("- Cosa misurano le variabili slack? :: La sovrapposizione tra le classi [[Variabili slack di margine]]"
            in md)
    assert "- Come si definisce xi? :: $\\xi_i" in md and "Vapnik" not in md and "Pesi della rete" not in md
    assert "_2 of 4 cards dropped: the note each cites does not hold its answer._" in md
    assert md.endswith(artifacts.PRACTICE_LINE) and len(seen) == 1
    assert "question? :: answer [[note title]]" in seen[0][0] and "Write in Italian" in seen[0][0]


def test_a_reply_with_no_card_is_asked_again_once(tmp_vault, monkeypatch):
    tmp_vault.note("ML/Variabili slack di margine.md", SLACK)
    seen = _reply(monkeypatch, "Ecco le flashcard!", "Cosa misurano? :: La sovrapposizione tra le classi [[Variabili slack di margine]]")
    md = artifacts.flashcards(["Variabili slack di margine"], n=4)
    assert [r for _p, r in seen] == [False, True] and ":: La sovrapposizione tra le classi" in md
    _reply(monkeypatch, "niente", "ancora niente")
    assert "No card held" in artifacts.flashcards(["Variabili slack di margine"], n=4)
    assert artifacts.flashcards(["Nessuna nota"]).startswith("No note or folder found")


def test_saved_cards_go_through_the_write_tool(tmp_vault, monkeypatch):
    import silica.tools.notes as notes

    tmp_vault.note("ML/Variabili slack di margine.md", SLACK)
    _reply(monkeypatch, "Cosa misurano? :: La sovrapposizione tra le classi [[Variabili slack di margine]]")
    wrote = {}
    monkeypatch.setattr(notes, "silica_write_note", lambda **kw: (wrote.update(kw), {"path": kw["path"]})[1])
    md = artifacts.flashcards(["Variabili slack di margine"], save="ML/Slack cards")
    assert wrote["path"] == "ML/Slack cards.md" and wrote["body"].startswith("#flashcards\n\n")
    assert "Cosa misurano? :: La sovrapposizione tra le classi [[Variabili slack di margine]]" in wrote["body"]
    assert "- " not in wrote["body"] and "Saved as [[Slack cards]]" in md


def test_slide_points_are_checked_against_the_slide_s_note(tmp_vault):
    tmp_vault.note("ML/Lezione 13.md", "# Lezione 13\n\nIl margine geometrico è la distanza dal piano.\n"
                   "Il percettrone aggiorna i pesi sugli errori.\n")
    deck = ("# Lezione 13\n\n---\n\n## Margini\n- Il margine geometrico è la distanza dal piano\n"
            "- Le reti convolutive usano il pooling\n\n[[Lezione 13]]\n\n---\n\n## Percettrone\n"
            "- Il percettrone aggiorna i pesi sugli errori\n\n[[Lezione 13]]\n")
    assert artifacts.ungrounded_bullets(deck) == (1, 3)
    assert artifacts.grounding_notes('/slides "Lezione 13"', deck) == [
        {"kind": "grounding", "text": "slide points not found in their note: 1 of 3"}]


def test_a_formula_answer_is_judged_by_its_formula():
    note = "# Slack\n\nUn punto e' esterno al margine quando $y_i f(x_i) > \\gamma$.\n"
    assert artifacts._grounded("Quando $y_i f(x_i) > \\gamma$", note)
    assert not artifacts._grounded("Quando $y_i f(x_i) < 0$", note)


def test_the_same_formula_written_two_ways_is_held():
    note = "$$\\xi_{i} = \\max \\left(0, \\gamma - y_{i} f \\left(\\boldsymbol{x}^{(i)}\\right)\\right)$$"
    assert artifacts._grounded("$\\xi_i = \\max(0, \\gamma - y_i f(x^{(i)}))$", note)
    assert not artifacts._grounded("$\\xi_i = \\max(0, 1 - y_i f(x^{(i)}))$", note)


def test_a_folder_hands_over_its_text_within_one_budget(tmp_vault, monkeypatch):
    """A folder whose notes carry no key point is read whole, all notes
    together within NOTE_CHARS, and the prompt names only the notes it read."""
    for i in range(30):   # lines too short to be a key point: the notes are read whole
        tmp_vault.note(f"ML/Nota {i:02d}.md", f"# Nota {i:02d}\n\n" + "una riga breve\n\n" * 300)
    seen = _reply(monkeypatch, "Cosa? :: parola [[Nota 00]]")
    artifacts.flashcards(["ML/"], n=3)
    prompt = seen[0][0]
    assert len(prompt) < artifacts.NOTE_CHARS + 3000
    assert "[[Nota 00]]" in prompt and "Nota 29" not in prompt


def test_long_points_stop_at_the_text_budget(tmp_vault, monkeypatch):
    from silica.kernel.learn import items

    tmp_vault.note("ML/Lunga.md", "# Lunga\n")
    monkeypatch.setattr(items, "key_points", lambda path, text=None: [{"text": "x" * 1000}] * 40)
    _titles, points = artifacts.study_points(["Lunga"])
    assert len(points) < 40 and sum(map(len, points)) < artifacts.STUDY_CHARS + 1100
