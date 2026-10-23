# SPDX-License-Identifier: AGPL-3.0-or-later
"""/flashcards and /slides (silica/cli.py): the deck and the cards NotebookLM's
Studio makes, drawn from Learn's checked key points where the notes carry them."""
from silica import cli


def _points(monkeypatch, points):
    from silica.kernel.learn import items

    monkeypatch.setattr(items, "key_points", lambda path, text=None: [{"text": t} for t in points.get(path, [])])


def test_flashcards_ground_every_card_in_a_checked_point(tmp_vault, monkeypatch):
    from silica.kernel.learn import artifacts

    tmp_vault.note("ML/Dropout.md", "# Dropout\n\nIl dropout approssima il bagging di sottoreti.\n")
    _points(monkeypatch, {"ML/Dropout.md": ["Il dropout approssima il bagging di sottoreti."]})
    prompts = []
    monkeypatch.setattr(artifacts, "_card_reply", lambda prompt, retry=False: (
        prompts.append(prompt), "Cosa approssima il dropout? :: il bagging di sottoreti [[Dropout]]")[1])
    assert cli._sc_flashcards(["Dropout", "--n=5"]) == ""   # made and printed here, nothing for the agent
    assert prompts[0].startswith("Write 5 flashcards from the notes [[Dropout]].")
    assert "- [[Dropout]]: Il dropout approssima il bagging di sottoreti." in prompts[0]
    assert cli._sc_flashcards([]).startswith("Error: /flashcards needs a note or a folder")


def test_the_chat_shows_the_cards_as_markdown(tmp_vault, tmp_path, monkeypatch):
    """The web turn answers /flashcards itself: the direct path would wrap the
    printed cards in a code block, where the formulas do not render."""
    import asyncio

    from silica.kernel.learn import artifacts
    from silica.ui.web import server

    server._reset_session()
    tmp_vault.note("ML/Dropout.md", "# Dropout\n\nIl dropout approssima il bagging di sottoreti.\n")
    _points(monkeypatch, {"ML/Dropout.md": ["Il dropout approssima il bagging di sottoreti."]})
    monkeypatch.setattr(artifacts, "_card_reply",
                        lambda prompt, retry=False: "Cosa approssima il dropout? :: il bagging di sottoreti [[Dropout]]")
    monkeypatch.setattr(server, "run_agent", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no agent turn")))

    async def collect():
        return [x async for x in server.run_turn('/flashcards "Dropout" --n=3')]

    done = asyncio.run(collect())[-1]
    assert done["type"] == "done" and ":: il bagging di sottoreti" in done["answer"]
    assert "<li>" in done["html"] and "<pre" not in done["html"]
    assert server.messages[-1] == {"role": "assistant", "content": done["answer"]}


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
