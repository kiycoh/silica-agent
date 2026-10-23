# SPDX-License-Identifier: AGPL-3.0-or-later
"""/flashcards and /slides (silica/cli.py): the deck and the cards NotebookLM's
Studio makes, drawn from Learn's checked key points where the notes carry them."""
from silica import cli


def _points(monkeypatch, points):
    from silica.kernel.learn import items

    monkeypatch.setattr(items, "key_points", lambda path, text=None: [{"text": t} for t in points.get(path, [])])


def test_flashcards_ground_every_card_in_a_checked_point(tmp_vault, monkeypatch):
    tmp_vault.note("ML/Dropout.md", "# Dropout\n\ncorpo\n")
    _points(monkeypatch, {"ML/Dropout.md": ["Il dropout approssima il bagging di sottoreti."]})
    prompt = cli._sc_flashcards(["Dropout", "--n=5"])
    assert prompt.startswith("Write 5 flashcards from [[Dropout]].")
    assert "- [[Dropout]]: Il dropout approssima il bagging di sottoreti." in prompt
    assert "(` :: `)" in prompt and "no labels such as Question or Answer" in prompt and "READ-ONLY" in prompt
    saved = cli._sc_flashcards(["Dropout", "--save=ML/Dropout cards.md"])
    assert "`ML/Dropout cards.md` with silica_write_note" in saved and "#flashcards" in saved
    assert cli._sc_flashcards([]).startswith("Error: /flashcards needs a note or a folder")


def test_slides_follow_the_points_or_read_the_notes(tmp_vault, monkeypatch):
    tmp_vault.note("ML/Lezione 13.md", "# Lezione 13\n")
    tmp_vault.note("ML/SVM.md", "# SVM\n")
    _points(monkeypatch, {})
    prompt = cli._sc_slides(["ML/"])
    assert "[[Lezione 13]]" in prompt and "[[SVM]]" in prompt
    assert "Read the note(s) first" in prompt and "`---`" in prompt and "READ-ONLY" in prompt
    assert "Slides plugin presents it" in cli._sc_slides(["SVM", "--save=ML/SVM deck.md"])


def test_both_are_listed_commands():
    from silica.ui.commands import COMMANDS

    names = {c.name for c in COMMANDS}
    assert {"/flashcards", "/slides"} <= names and "/flashcards" in cli._SHORTCUTS and "/slides" in cli._SHORTCUTS
