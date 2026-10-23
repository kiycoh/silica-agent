# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cards and slides a turn wrote stand on the notes they cite
(silica/kernel/learn/artifacts.py): what does not is named under the answer."""
from silica.kernel.learn import artifacts


def test_a_card_whose_answer_its_note_does_not_hold_is_named(tmp_vault):
    tmp_vault.note("ML/Variabili slack di margine.md",
                   "# Variabili slack\n\nLe variabili slack misurano la sovrapposizione tra le classi.\n"
                   "$\\xi_i = \\max(0, \\gamma - y_i f(x_i))$\n")
    answer = ("- Cosa misurano le variabili slack? :: La sovrapposizione tra le classi [[Variabili slack di margine]]\n"
              "- Come si definisce ξ? :: $\\xi_i = \\max(0, \\gamma - y_i f(x_i))$ [[Variabili slack di margine]]\n"
              "- Chi le ha inventate? :: Vapnik nel 1963 a Mosca [[Variabili slack di margine]]\n"
              "- Cosa sono? :: Pesi della rete [[Nota che non esiste]]\n")
    assert artifacts.ungrounded_cards(answer) == [3, 4]
    notes = artifacts.grounding_notes('/flashcards "Variabili slack di margine"', answer)
    assert notes == [{"kind": "grounding", "text": "cards whose answer is not in their note: 3 · 4"}]
    assert artifacts.grounding_notes("cosa sono le slack?", answer) == []   # only the artifact commands


def test_slide_points_are_checked_against_the_slide_s_note(tmp_vault):
    tmp_vault.note("ML/Lezione 13.md", "# Lezione 13\n\nIl margine geometrico è la distanza dal piano.\n"
                   "Il percettrone aggiorna i pesi sugli errori.\n")
    deck = ("# Lezione 13\n\n---\n\n## Margini\n- Il margine geometrico è la distanza dal piano\n"
            "- Le reti convolutive usano il pooling\n\n[[Lezione 13]]\n\n---\n\n## Percettrone\n"
            "- Il percettrone aggiorna i pesi sugli errori\n\n[[Lezione 13]]\n")
    assert artifacts.ungrounded_bullets(deck) == (1, 3)
    assert artifacts.grounding_notes('/slides "Lezione 13"', deck) == [
        {"kind": "grounding", "text": "slide points not found in their note: 1 of 3"}]



def test_a_card_written_with_labels_is_read_too(tmp_vault):
    tmp_vault.note("ML/Slack.md", "# Slack\n\nLe variabili slack misurano la violazione del margine.\n")
    answer = ("Question: Cosa misurano? Answer: La violazione del margine [[Slack]]\n"
              "Domanda: Chi le ha inventate? Risposta: Vapnik nel 1963 [[Slack]]\n")
    assert artifacts.ungrounded_cards(answer) == [2]



def test_a_formula_answer_is_judged_by_its_formula():
    note = "# Slack\n\nUn punto e' esterno al margine quando $y_i f(x_i) > \\gamma$.\n"
    assert artifacts._grounded("Quando $y_i f(x_i) > \\gamma$", note)
    assert not artifacts._grounded("Quando $y_i f(x_i) < 0$", note)



def test_the_same_formula_written_two_ways_is_held():
    note = "$$\\xi_{i} = \\max \\left(0, \\gamma - y_{i} f \\left(\\boldsymbol{x}^{(i)}\\right)\\right)$$"
    assert artifacts._grounded("$\\xi_i = \\max(0, \\gamma - y_i f(x^{(i)}))$", note)
    assert not artifacts._grounded("$\\xi_i = \\max(0, 1 - y_i f(x^{(i)}))$", note)
