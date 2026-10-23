# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0127 point 3: a skill whose metadata says `persona: true` is worn by a
conversation instead of run once."""
from __future__ import annotations

from pathlib import Path

from silica.kernel import personas


def _skill(name: str, body: str, persona: bool = True) -> None:
    from silica.config import CONFIG

    d = Path(CONFIG.vault_path) / ".silica" / "skills" / name
    d.mkdir(parents=True)
    meta = "metadata:\n  persona: true\n" if persona else ""
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: clear legal paraphrase\n{meta}---\n{body}\n", encoding="utf-8")


def _seed() -> list[dict]:
    return [{"role": "system", "content": "base"}, {"role": "system", "content": "memory"},
            {"role": "system", "content": "LANGUAGE"}]


def test_the_flag_makes_a_persona_and_a_plain_skill_stays_a_command():
    from silica.config import CONFIG
    from silica.ui.commands import commands

    _skill("parafrasi", "Bold the key terms.<!-- why: memorisation -->")
    _skill("weekly", "Review the inbox.", persona=False)
    assert list(personas.available(CONFIG.vault_path)) == ["parafrasi"]
    rows = {c.name: c.summary for c in commands()}
    assert rows["/parafrasi"] == "clear legal paraphrase (vault persona)"
    assert rows["/weekly"] == "clear legal paraphrase (vault skill)"


def test_wearing_a_persona_writes_one_line_before_the_language_rule_and_edits_it_in_place():
    from silica.config import CONFIG

    _skill("parafrasi", "Bold the key terms.<!-- why: memorisation -->")
    _skill("tutor", "Ask before you answer.")
    msgs = _seed() + [{"role": "user", "content": "ciao"}]
    reply, rest = personas.command("/parafrasi", msgs, CONFIG.vault_path)
    assert rest == "" and "parafrasi on" in reply
    assert msgs[-2]["content"] == "LANGUAGE" and msgs[-3]["origin"] == personas.ORIGIN
    assert "Bold the key terms." in msgs[-3]["content"] and "memorisation" not in msgs[-3]["content"]
    assert personas.active(msgs) == "parafrasi"
    personas.command("/persona tutor", msgs, CONFIG.vault_path)
    assert len(msgs) == 5 and personas.active(msgs) == "tutor"
    personas.command("/persona off", msgs, CONFIG.vault_path)
    assert personas.active(msgs) == "" and msgs[-3]["content"] == personas.OFF_LINE


def test_words_after_the_name_are_the_turn_and_a_compacted_chat_is_told_why_not():
    from silica.config import CONFIG

    _skill("parafrasi", "Bold the key terms.")
    msgs = _seed()
    assert personas.command("/parafrasi L'art. 1321 c.c. recita", msgs, CONFIG.vault_path)[1] == \
        "L'art. 1321 c.c. recita"
    fresh = _seed()
    reply, rest = personas.command("/parafrasi testo", fresh, CONFIG.vault_path, can_insert=False)
    assert rest == "" and "compacted" in reply and personas.active(fresh) == ""


def test_persona_lists_and_an_unknown_name_says_so_and_other_commands_pass_through():
    from silica.config import CONFIG

    _skill("parafrasi", "x")
    msgs = _seed()
    assert "/parafrasi" in personas.command("/persona", msgs, CONFIG.vault_path)[0]
    assert "No persona named" in personas.command("/persona nessuna", msgs, CONFIG.vault_path)[0]
    assert personas.command("/explain entropia", msgs, CONFIG.vault_path) is None
    assert personas.command("ciao", msgs, CONFIG.vault_path) is None


def test_the_line_is_capped():
    from silica.config import CONFIG

    _skill("lunga", "\n".join(["riga di istruzioni lunga abbastanza"] * 200))
    skill = personas.available(CONFIG.vault_path)["lunga"]
    body = personas.line(skill).split("in its author's words:\n\n", 1)[1]
    assert len(body) <= personas.CAP


def test_the_persona_never_reaches_the_provider():
    from silica.agent.providers import INTERNAL_KEYS

    assert personas.TURN_KEY in INTERNAL_KEYS


# --- the GUI turn ---------------------------------------------------------------

def _sse(response) -> list[dict]:
    import json

    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_a_gui_chat_wears_a_persona_with_no_model_and_a_reopened_chat_wears_it_again(tmp_vault, monkeypatch):
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.config import CONFIG
    from silica.ui.web import server

    monkeypatch.setattr(CONFIG, "capture_sessions", False)
    _skill("parafrasi", "Bold the key terms.")
    server._reset_session()
    tc = TestClient(server.app)
    seen: list[list[dict]] = []

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        seen.append([dict(m) for m in messages])
        messages.append({"role": "assistant", "content": "Risposta"})
        return "Risposta"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)
    done = _sse(tc.post("/chat", json={"text": "/parafrasi"}))[-1]
    assert done["type"] == "done" and "parafrasi on" in done["answer"] and not seen
    tc.post("/chat", json={"text": "Parafrasa l'art. 1321"})
    sent = seen[-1]
    assert any(m.get("origin") == personas.ORIGIN for m in sent)
    assert sent[-1][personas.TURN_KEY] == "parafrasi"
    sid = tc.get("/sessions").json()[0]["id"]
    tc.post("/reset")
    assert personas.active(server.messages) == ""
    assert tc.post("/session/load", json={"id": sid}).status_code == 200
    assert personas.active(server.messages) == "parafrasi"
