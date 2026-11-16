# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0169: the chat answers from general knowledge after the vault came up
empty, in a labelled block the turn checks, and a command never does."""
from __future__ import annotations

import json

import pytest

from silica.agent import general_knowledge as gk
from silica.agent import turn_notes
from silica.agent.recall_watch import RecallWatch
from silica.config import CONFIG
from silica.obs.events import ToolCompleteEvent
from silica.prompts import GENERAL_HEAD, VAULT_ONLY, system_prompt

ANSWER = ("Your notes say nothing about the Peace of Westphalia.\n\n"
          f"{GENERAL_HEAD}\n> Signed in 1648, it ended the Thirty Years' War.\n\n"
          "Ask again with /web for sources.")


class TestPrompt:
    def test_off_is_the_strict_prompt_byte_for_byte(self):
        assert system_prompt("it", math=True, general=False) == system_prompt("it", math=True)
        assert "[!general]" not in system_prompt()

    def test_on_adds_the_rule_before_the_language_line_which_stays_last(self):
        on = system_prompt(None, math=True, general=True)
        assert GENERAL_HEAD in on
        assert on.index(GENERAL_HEAD) < on.index("## What language to reply in")

    def test_the_seed_follows_the_setting_and_the_phone_stays_strict(self, tmp_vault, monkeypatch):
        from silica.session.seed import seed_messages

        monkeypatch.setattr(CONFIG, "general_knowledge", True)
        assert GENERAL_HEAD in seed_messages()[0]["content"]
        assert GENERAL_HEAD not in seed_messages(general=False)[0]["content"]
        monkeypatch.setattr(CONFIG, "general_knowledge", False)
        assert GENERAL_HEAD not in seed_messages()[0]["content"]


class TestBlock:
    def test_the_block_is_found_in_any_case_and_taken_out_whole(self):
        assert gk.has_general(ANSWER)
        assert gk.has_general("> [!GENERAL] Dalla conoscenza generale\n> testo")
        assert not gk.has_general("> [!note] a quote\n> with [!general] inside it")
        rest = gk.without_general(ANSWER)
        assert "1648" not in rest and "Westphalia" in rest and "/web" in rest

    def test_the_notes_say_the_label_and_warn_only_when_due(self):
        """ADR-0174: "partly" only beside cited prose. An answer that is the
        block and the one line saying the notes lack it was labelled partly."""
        whole = {"kind": "general", "text": gk.UNCITED}
        assert gk.general_notes("no block here", command=False) == []
        assert gk.general_notes(ANSWER, command=False) == [whole]
        assert gk.general_notes(ANSWER, command=True) == [
            whole, {"kind": "general", "text": gk.IN_COMMAND}]
        partly = "Westphalia is in [[Treaties]].\n\n" + ANSWER
        assert gk.general_notes(partly, command=False) == [{"kind": "general", "text": gk.LABEL}]
        assert gk.general_notes("Treaties says little.\n\n" + ANSWER, command=False,
                                reads=["History/Treaties"]) == [{"kind": "general", "text": gk.LABEL}]

    def test_a_block_is_held_back_in_place_and_the_rest_stays(self):
        held = gk.held_back(ANSWER)
        assert not gk.has_general(held) and "1648" not in held
        assert held.startswith("Your notes say nothing") and held.rstrip().endswith("for sources.")
        assert gk.HELD_BACK in held
        assert gk.held_back("no block here") == "no block here"
        assert gk.held_back(f"{GENERAL_HEAD}\n> only the block") == gk.HELD_BACK

    def test_a_block_after_a_search_without_its_index_says_so_when_held_back(self):
        held = gk.held_back(ANSWER, gk.HELD_BACK_UNSURE)
        assert gk.HELD_BACK_UNSURE in held and gk.HELD_BACK not in held and "1648" not in held

    def test_a_search_or_a_read_counts_as_looking_and_a_write_does_not(self):
        import silica.cli  # noqa: F401  registers every tool

        assert gk.looks_in_vault("silica_recall") and gk.looks_in_vault("silica_read_note")
        assert gk.looks_in_vault("silica_search_context")
        assert not gk.looks_in_vault("silica_write_note") and not gk.looks_in_vault("nope")


class TestAnAnswerThatCitesNoNote:
    """ADR-0174, the owner's 2026-11-13 decision: every chat answer that cites no
    note and carries no block says under it that it is general knowledge."""

    def test_it_gets_the_line_and_a_cited_one_does_not(self):
        line = [{"kind": "general", "text": gk.UNCITED}]
        assert gk.uncited_notes("Canberra is the capital of Australia.") == line
        assert gk.uncited_notes("Hello! What shall we look at?") == line, "small talk too"
        assert gk.uncited_notes("Galileo was born in 1564 [[Galileo Galilei]].") == []
        assert gk.uncited_notes("Galileo was born in 1564 [[Science/Galileo Galilei|Galileo]].") == []
        assert gk.uncited_notes("") == []
        assert gk.uncited_notes(ANSWER) == [], "a block has its own label"

    def test_a_read_notes_title_named_bare_is_a_citation_as_the_renderer_links_it(self):
        """ADR-0127: the answer links a read note's title where it names it."""
        said = "Galileo Galilei was born in Pisa in 1564."
        assert gk.uncited_notes(said, ["Culture/Science/Galileo Galilei"]) == []
        assert gk.uncited_notes(said, ["Culture/Art/Caravaggio.md"]) != []
        assert gk.uncited_notes(said) != [], "a title the turn did not read is not a citation"

    def test_a_figure_embed_is_no_citation(self):
        assert gk.uncited_notes("Here it is.\n\n![[orbit.png]]") != []

    def test_with_general_knowledge_off_the_line_says_no_note_is_cited(self, monkeypatch):
        """Cited or refused: a refusal is no general knowledge (bench D1 Q5, 2026-11-16)."""
        monkeypatch.setattr(CONFIG, "general_knowledge", False)
        refusal = "The files in this folder do not contain anything about Orpheus Telecom."
        assert gk.uncited_notes(refusal) == [{"kind": "general", "text": gk.UNCITED_STRICT}]
        assert gk.uncited_notes("Galileo was born in 1564 [[Galileo Galilei]].") == []


class TestCommandsStayInTheVault:
    def test_a_directive_carries_the_vault_only_line_while_the_rule_is_on(self, tmp_vault, monkeypatch):
        from silica.commands import dispatch

        monkeypatch.setattr(CONFIG, "general_knowledge", True)
        out = dispatch('/explain "entropy"', [])
        assert out is not None and out.directive and out.directive.endswith(VAULT_ONLY)
        monkeypatch.setattr(CONFIG, "general_knowledge", False)
        out = dispatch('/explain "entropy"', [])
        assert out is not None and VAULT_ONLY not in (out.directive or "")


# --- the turn, through the browser's route --------------------------------------

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _sse(response) -> list[dict]:
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def _turn(answer: str, *events):
    def run(messages, model=None, tool_progress_callback=None, cancel_token=None, **kw):
        for ev in events:
            tool_progress_callback(ev)
        messages.append({"role": "assistant", "content": answer})
        return answer
    return run


MISS = ToolCompleteEvent(name="silica_recall", args={}, call_id="c1", result=json.dumps(
    {"query": "westphalia", "notes": []}), duration_s=0.0, iteration=1)


@pytest.fixture
def client(tmp_vault):
    from silica.ui.web import server
    from silica.ui.web.session import SESSION

    SESSION.reset()
    yield TestClient(server.app)
    SESSION.reset()


def test_a_searched_turn_is_labelled_and_its_block_years_are_not_flagged(client, monkeypatch):
    from silica.ui.web.session import SESSION

    monkeypatch.setattr("silica.session.turn.run_agent", _turn(ANSWER, MISS))
    done = _sse(client.post("/chat", json={"text": "what was the peace of westphalia?"}))[-1]
    general = [n for n in done["notes"] if n["kind"] == "general"]
    assert general == [{"kind": "general", "text": gk.UNCITED}], "the label and no second line"
    assert not any(n["kind"] == "numbers" for n in done["notes"])
    assert {"kind": "general", "text": gk.UNCITED} in SESSION.chat.messages[-1][turn_notes.KEY]


def _general(done: dict) -> list[str]:
    return [n["text"] for n in done.get("notes", []) if n["kind"] == "general"]


def test_an_uncited_answer_with_no_search_gets_the_line_live_on_reload_and_in_the_history(client, monkeypatch):
    """"No need to check my notes, just tell me: capital of Australia?" reached
    the person uncited and unlabelled (2026-11-13)."""
    from silica.ui.web.session import SESSION

    monkeypatch.setattr("silica.session.turn.run_agent", _turn("Canberra is the capital of Australia."))
    done = _sse(client.post("/chat", json={"text": "No need to check my notes: capital of Australia?"}))[-1]
    assert _general(done) == [gk.UNCITED]
    assert gk.UNCITED not in done["answer"], "a line under the answer, not in it"
    assert {"kind": "general", "text": gk.UNCITED} in SESSION.chat.messages[-1][turn_notes.KEY]
    replayed = [m["notes"] for m in client.get("/messages").json() if m["role"] == "assistant"]
    assert {"kind": "general", "text": gk.UNCITED} in replayed[-1]


def test_the_claim_written_beside_a_held_back_block_still_gets_the_line(client, monkeypatch):
    """Seen live 2026-11-13: the model wrote the answer above its block, the
    block was held back, and "Canberra" stood uncited with no line."""
    said = f"The capital of Australia is **Canberra**.\n\n{GENERAL_HEAD}\n> Canberra, since 1913."
    monkeypatch.setattr("silica.session.turn.run_agent", _turn(said))
    done = _sse(client.post("/chat", json={"text": "No need to check my notes: capital of Australia?"}))[-1]
    assert "1913" not in done["answer"] and "held back" in done["answer"]
    assert _general(done) == [gk.UNCITED]


def test_a_cited_vault_answer_gets_no_line(client, monkeypatch):
    read = ToolCompleteEvent(name="silica_recall", args={}, call_id="c1", result=json.dumps(
        {"query": "galileo", "notes": ["Culture/Science/Galileo Galilei"]}), duration_s=0.0, iteration=1)
    monkeypatch.setattr("silica.session.turn.run_agent",
                        _turn("Galileo was born in Pisa in 1564 [[Galileo Galilei]].", read))
    done = _sse(client.post("/chat", json={"text": "When was Galileo born?"}))[-1]
    assert _general(done) == []


def test_a_turn_that_wrote_reports_its_write_with_no_line(client, monkeypatch):
    wrote = ToolCompleteEvent(name="silica_remember", args={}, call_id="c1", result=json.dumps(
        {"saved": "exam on Thursday"}), duration_s=0.0, iteration=1)
    monkeypatch.setattr("silica.session.turn.run_agent", _turn("Saved: your exam is on Thursday.", wrote))
    done = _sse(client.post("/chat", json={"text": "my exam is on Thursday"}))[-1]
    assert _general(done) == []


def test_the_turn_step_leaves_out_commands_held_back_blocks_the_web_and_empty_answers(tmp_vault):
    from silica.agent.sentinels import EMPTY_COMPLETION
    from silica.session.conversation import Conversation
    from silica.session.turn import Turn, note

    def notes_of(answer: str, text: str = "capital of Australia?", **kw) -> list[str]:
        conv = Conversation(messages=[{"role": "user", "content": text},
                                      {"role": "assistant", "content": answer}])
        t = Turn(conv=conv, text=text, answer=answer, read=RecallWatch(), **kw)
        t.watch = t.read
        note(t)
        return [n["text"] for n in t.notes if n["kind"] == "general"]

    assert notes_of("Canberra.") == [gk.UNCITED]
    assert notes_of("Canberra.", text="/explain capitals") == []
    assert notes_of(gk.HELD_BACK) == [], "the held-back line is Silica's, not a claim"
    assert notes_of(gk.HELD_BACK_UNSURE) == []
    assert notes_of(EMPTY_COMPLETION, empty=True) == []
    assert notes_of("Canberra.", stopped=True) == []
    w = RecallWatch()
    w.web_answer = True
    conv = Conversation(messages=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "Canberra."}])
    t = Turn(conv=conv, text="q", answer="Canberra.", read=w)
    t.watch = w
    note(t)
    assert not any(n["kind"] == "general" for n in t.notes), "a web answer cites its pages"
    thin = RecallWatch()
    thin(MISS)
    t = Turn(conv=conv, text="q", answer="Your notes have nothing on that.", read=thin)
    t.watch = thin
    note(t)
    assert [n["kind"] for n in t.notes] == ["retrieval"], "the thin line already says the notes had nothing"


def test_in_a_demo_the_thin_line_is_not_drawn_so_the_uncited_line_is(tmp_vault, monkeypatch):
    from silica.session.conversation import Conversation
    from silica.session.turn import Turn, note

    monkeypatch.setenv("SILICA_DEMO", "1")
    thin = RecallWatch()
    thin(MISS)
    conv = Conversation(messages=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "Canberra."}])
    t = Turn(conv=conv, text="q", answer="Canberra.", read=thin)
    t.watch = thin
    note(t)
    assert [n["text"] for n in t.notes] == [gk.UNCITED]


# The live trace of 2026-11-13, "When was Galileo born?" on the demo vault: the
# model's own body search returned two sections of the note that missed the one
# with the day, and the model gave the day in a block.
GALILEO_NOTE = ("---\ndescription: \"The astronomer (1564 to 1642).\"\n---\nGalileo Galilei (1564 to 1642) was "
                "an Italian astronomer.\n\n## Life\n\nHe was born in Pisa on 15 February 1564.\n\n"
                "## Why he was tried by the Inquisition\n\n- **1616:** consultants judged Copernicus.\n")
GALILEO_ANSWER = ("Your notes say [[Galileo Galilei]] lived **1564 to 1642**, so your vault places the birth in "
                  "**1564**, without a specific day.\n\n"
                  f"{GENERAL_HEAD}\n> The exact date is **15 February 1564**, in Pisa, under the Julian calendar "
                  "then in use.")
GALILEO_SEARCH = ToolCompleteEvent(name="silica_search_context", args={"query": "Galileo born"}, call_id="c1",
                                   result=json.dumps({"hits": [
                                       {"path": "Culture/Science/Galileo Galilei.md", "line": 24,
                                        "section": "Why he was tried by the Inquisition", "snippet": "- **1616:**"},
                                       {"path": "Culture/Culture.md", "line": 62, "section": "Timeline",
                                        "snippet": "| 1543 |"}], "notes_matched": 4}),
                                   duration_s=0.0, iteration=1)


def test_a_block_whose_figures_a_found_note_holds_says_so(client, tmp_vault, monkeypatch):
    """ADR-0174: a fact the vault holds, given as general knowledge, is named
    under the answer with the note that holds it."""
    tmp_vault.note("Culture/Science/Galileo Galilei.md", GALILEO_NOTE)
    tmp_vault.note("Culture/Culture.md", "# Culture\n\n| 1543 | Copernicus |\n")
    monkeypatch.setattr("silica.session.turn.run_agent", _turn(GALILEO_ANSWER, GALILEO_SEARCH))
    done = _sse(client.post("/chat", json={"text": "When was Galileo born?"}))[-1]
    assert _general(done) == [gk.LABEL, "the figures of the general-knowledge block are in your notes: Galileo Galilei"]


def test_a_block_no_found_note_holds_gets_no_such_line(client, tmp_vault, monkeypatch):
    tmp_vault.note("Culture/Science/Galileo Galilei.md", GALILEO_NOTE)
    said = ("Your notes say nothing on Westphalia.\n\n"
            f"{GENERAL_HEAD}\n> Signed in 1648, it ended the Thirty Years' War.")
    monkeypatch.setattr("silica.session.turn.run_agent", _turn(said, GALILEO_SEARCH))
    done = _sse(client.post("/chat", json={"text": "what was the peace of westphalia?"}))[-1]
    assert _general(done) == [gk.UNCITED]


def test_a_degraded_search_that_found_nothing_holds_a_block_back_with_its_own_reason(client, monkeypatch):
    from silica.ui.web.session import SESSION

    unsure = ToolCompleteEvent(name="silica_recall", args={}, call_id="c1", result=json.dumps(
        {"query": "westphalia", "degraded": {"index": "building"}, "notes": []}), duration_s=0.0, iteration=1)
    monkeypatch.setattr("silica.session.turn.run_agent", _turn(ANSWER, unsure))
    done = _sse(client.post("/chat", json={"text": "what was the peace of westphalia?"}))[-1]
    assert "1648" not in done["answer"] and gk.HELD_BACK_UNSURE in SESSION.chat.messages[-1]["content"]
    assert _general(done) == [gk.UNCITED], "what the model wrote beside the block cites no note"


def test_a_block_with_no_search_before_it_is_held_back_and_never_stored(client, monkeypatch):
    """The owner's rule is general knowledge only after the vault was searched:
    a block the model wrote before any search is not the answer, on screen, in
    the stored chat or in the history the next turn reads."""
    from silica.ui.web.session import SESSION

    monkeypatch.setattr("silica.session.turn.run_agent", _turn(ANSWER))
    done = _sse(client.post("/chat", json={"text": "what was the peace of westphalia?"}))[-1]
    stored = SESSION.chat.messages[-1]["content"]
    for shown in (done["answer"], done["html"], stored):
        assert "1648" not in shown and "general knowledge was held back" in shown
    assert gk.HELD_BACK in stored
    assert "Ask again with /web" in done["answer"]
    # No label for the block, which is gone. The prose the model wrote beside
    # it cites no note, and with no search it rests on none (ADR-0174).
    assert _general(done) == [gk.UNCITED]


def test_the_copy_button_copies_what_the_server_drew_not_what_streamed():
    """A held-back block streams before the server takes it out: the drawn answer
    was the held-back line while copy still gave the block (2026-11-13, a stream
    replayed through runTurn and a real click). Both branches that swap in the
    server's render must take its text too."""
    import re

    from silica.ui.web.server import STATIC_DIR

    js = (STATIC_DIR / "app-chat.js").read_text(encoding="utf-8")
    swaps = re.findall(r"\} else if \((?:segments === 1 && curText|tail && ev\.html)[^{]*\{\n(.*?)(?=\n      \})", js, re.S)
    assert len(swaps) == 2 and all("curText.raw = ev.answer" in s for s in swaps)


def test_the_terminal_holds_the_block_back_too():
    from silica import cli
    from silica.session import turn as session_turn

    assert cli._AFTER[0] is session_turn.hold_unsearched_general


def test_the_block_draws_as_its_own_callout(tmp_vault):
    from silica.ui.web import render

    html = render.answer_html(ANSWER, [])
    assert 'class="callout callout-general"' in html
    assert "From general knowledge, not your notes" in html
