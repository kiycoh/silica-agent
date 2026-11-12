# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0169: the chat answers from general knowledge after the vault came up
empty, in a labelled block the turn checks, and a command never does."""
from __future__ import annotations

import json

import pytest

from silica.agent import general_knowledge as gk
from silica.agent import turn_notes
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
        label = {"kind": "general", "text": gk.LABEL}
        assert gk.general_notes("no block here", command=False) == []
        assert gk.general_notes(ANSWER, command=False) == [label]
        assert gk.general_notes(ANSWER, command=True) == [
            label, {"kind": "general", "text": gk.IN_COMMAND}]

    def test_a_block_is_held_back_in_place_and_the_rest_stays(self):
        held = gk.held_back(ANSWER)
        assert not gk.has_general(held) and "1648" not in held
        assert held.startswith("Your notes say nothing") and held.rstrip().endswith("for sources.")
        assert gk.HELD_BACK in held
        assert gk.held_back("no block here") == "no block here"
        assert gk.held_back(f"{GENERAL_HEAD}\n> only the block") == gk.HELD_BACK

    def test_a_search_or_a_read_counts_as_looking_and_a_write_does_not(self):
        import silica.cli  # noqa: F401  registers every tool

        assert gk.looks_in_vault("silica_recall") and gk.looks_in_vault("silica_read_note")
        assert gk.looks_in_vault("silica_search_context")
        assert not gk.looks_in_vault("silica_write_note") and not gk.looks_in_vault("nope")


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
    assert general == [{"kind": "general", "text": gk.LABEL}]
    assert not any(n["kind"] == "numbers" for n in done["notes"])
    assert {"kind": "general", "text": gk.LABEL} in SESSION.chat.messages[-1][turn_notes.KEY]


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
    assert not any(n["kind"] == "general" for n in done.get("notes", []))


def test_the_terminal_holds_the_block_back_too():
    from silica import cli
    from silica.session import turn as session_turn

    assert cli._AFTER[0] is session_turn.hold_unsearched_general


def test_the_block_draws_as_its_own_callout(tmp_vault):
    from silica.ui.web import render

    html = render.answer_html(ANSWER, [])
    assert 'class="callout callout-general"' in html
    assert "From general knowledge, not your notes" in html
