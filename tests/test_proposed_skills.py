# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0127 points 5 and 6: a request made in three chats and never undone
becomes a proposed skill, which only the person installs; an installed skill
that answers a request is offered for the next turn."""
from __future__ import annotations

import itertools
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from silica.kernel import proposed_skills as ps

SAME = ["Integra i miei appunti con il manuale, capitolo per capitolo",
        "integra questi appunti con il capitolo del manuale allegato",
        "Puoi integrare gli appunti della lezione con il manuale?"]
OTHER = ["Spiegami la differenza tra contratto e negozio giuridico",
         "Fammi un quiz sul capitolo 3 del manuale",
         "Sistematizza il testo che ti incollo"]


def test_three_phrasings_of_one_request_are_alike_and_three_others_are_not():
    kinds = {t: 0 for t in SAME} | {t: i + 1 for i, t in enumerate(OTHER)}
    for a, b in itertools.combinations(SAME + OTHER, 2):
        assert ps.alike(ps.words(a), ps.words(b)) == (kinds[a] == kinds[b] == 0), (a, b)


def test_two_requests_that_share_only_a_notes_name_are_not_one_kind():
    """Measured 2026-10-22 on the owner's chats: the two met on Machine and
    learning alone. A note called Appunti does not stop the real repetition."""
    titles = {}
    for name in ("Machine learning", "Appunti lezione 3", "Manuale"):
        for s in (stems := ps.words(name)):
            titles.setdefault(s, []).append(stems)
    a, b = ps.words("Affronta in breve Machine learning"), ps.words("Cos'è il Machine Learning?")
    assert ps.alike(a, b) and not ps.same_kind(a, b, titles)
    assert ps.same_kind(ps.words(SAME[0]), ps.words(SAME[1]), titles)


def test_the_instruction_is_the_first_paragraph_and_never_the_pasted_material():
    text = "Integra i miei appunti con il manuale\n\nArt. 1321 c.c. Il contratto è l'accordo..."
    assert ps.ask(text) == "Integra i miei appunti con il manuale"
    assert len(ps.ask("x" * 900)) == ps.ASK_CHARS


def _vault() -> str:
    from silica.config import CONFIG

    return CONFIG.vault_path


def _history(monkeypatch, chats):
    monkeypatch.setattr(ps, "_history", lambda vault, current: chats)


def test_a_third_chat_with_the_same_request_is_the_evidence(monkeypatch):
    _history(monkeypatch, [("s1", [SAME[0]], False), ("s2", ["ciao", SAME[1]], False), ("s3", [OTHER[0]], False)])
    got = ps.detect(SAME[2], [], _vault(), "now")
    assert got == {"asks": [SAME[2], SAME[0], SAME[1]], "sessions": ["now", "s1", "s2"]}


def test_two_chats_are_not_enough_and_an_undone_one_does_not_count(monkeypatch):
    _history(monkeypatch, [("s1", [SAME[0]], False), ("s2", [SAME[1]], True)])
    assert ps.detect(SAME[2], [], _vault(), "now") is None
    _history(monkeypatch, [("s1", [SAME[0]], False), ("s2", [SAME[1]], False)])
    undid = [{"role": "user", "content": "/undo", "origin": "cli"}]
    assert ps.detect(SAME[2], undid, _vault(), "now") is None
    assert ps.detect("/integra appunti manuale", [], _vault(), "now") is None


def _provider(text: str):
    return SimpleNamespace(call_llm=lambda **kw: SimpleNamespace(text=text))


DRAFTED = {"name": "Integra Appunti", "description": "Integra gli appunti: con il manuale, un capitolo alla volta",
           "body": "Integra $ARGUMENTS con il manuale, un capitolo alla volta."}


def test_the_draft_sees_only_the_persons_words(monkeypatch):
    seen = {}

    def provider(config, role):
        def call_llm(**kw):
            seen.update(kw)
            return SimpleNamespace(text=json.dumps(DRAFTED))
        return SimpleNamespace(call_llm=call_llm)

    monkeypatch.setattr("silica.agent.providers.get_provider", provider)
    assert ps.draft(SAME, None) == DRAFTED
    user = seen["messages"][-1]["content"]
    assert all(a in user for a in SAME) and len(seen["messages"]) == 2 and seen["tools"] is None
    monkeypatch.setattr("silica.agent.providers.get_provider", lambda c, role: _provider("not json"))
    assert ps.draft(SAME, None) is None


def test_a_staged_proposal_runs_nowhere_until_installed_and_a_dismissed_one_is_not_proposed_again(monkeypatch):
    from silica_core.kernel.skills import discover

    vault = _vault()
    ev = {"asks": SAME, "sessions": ["now", "s1", "s2"]}
    name = ps.stage(vault, DRAFTED, ev, "m")
    assert name == "integra-appunti" and "integra-appunti" not in discover(vault)
    row = ps.pending(vault)[0]
    assert row["name"] == name and row["asks"] == SAME and ": " in row["description"]
    assert ps.stage(vault, DRAFTED, ev) == "integra-appunti-2"
    assert ps.install(vault, name) is None and "integra-appunti" in discover(vault)
    third = ps.stage(vault, DRAFTED, ev)
    assert third == "integra-appunti-3"   # the installed name and the staged -2 are taken
    (Path(vault) / ".silica" / "skills" / third).mkdir(parents=True)   # made by hand meanwhile
    assert "exists" in ps.install(vault, third)
    assert ps.dismiss(vault, "integra-appunti-2") is None
    assert [p["name"] for p in ps.pending(vault)] == ["integra-appunti-3"]
    _history(monkeypatch, [("s1", [SAME[0]], False), ("s2", [SAME[1]], False)])
    assert ps.detect(SAME[2], [], vault, "now") is None   # answered by the installed one, or asked once


def _skill(name: str, desc: str, persona: bool = False) -> None:
    d = Path(_vault()) / ".silica" / "skills" / name
    d.mkdir(parents=True)
    meta = "metadata:\n  persona: true\n" if persona else ""
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n{meta}---\nx\n", encoding="utf-8")


def test_an_installed_skill_is_offered_for_the_request_it_answers():
    _skill("integra-appunti", "Integra gli appunti con il manuale")
    _skill("parafrasi-giuridica", "Parafrasi chiara di un testo giuridico", persona=True)
    assert ps.offer(SAME[0], _vault()) == {"command": "/integra-appunti",
                                           "description": "Integra gli appunti con il manuale", "persona": False}
    assert ps.offer("Fammi la parafrasi di questo testo giuridico", _vault())["persona"] is True
    assert ps.offer(OTHER[0], _vault()) is None
    assert ps.offer("/integra-appunti", _vault()) is None


# --- the GUI ------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.config import CONFIG
    from silica.ui.web import server

    monkeypatch.setattr(CONFIG, "capture_sessions", False)
    server._reset_session()

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        messages.append({"role": "assistant", "content": "Fatto"})
        return "Fatto"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)
    return TestClient(server.app), server


def _done(tc, text: str) -> dict:
    lines = tc.post("/chat", json={"text": text}).text.splitlines()
    return [json.loads(x[6:]) for x in lines if x.startswith("data: ")][-1]


def test_the_third_request_ends_with_a_proposal_line_and_settings_installs_it(client, monkeypatch):
    tc, _server = client
    _history(monkeypatch, [("s1", [SAME[0]], False), ("s2", [SAME[1]], False)])
    monkeypatch.setattr(ps, "draft", lambda asks, config: DRAFTED)
    done = _done(tc, SAME[2])
    assert done["notes"][-1]["kind"] == "skill" and "/integra-appunti" in done["notes"][-1]["text"]
    listed = tc.get("/skills").json()
    assert [p["name"] for p in listed["proposed"]] == ["integra-appunti"]
    assert tc.post("/skills/proposed/integra-appunti/install").json() == {"ok": True, "name": "integra-appunti"}
    assert tc.post("/skills/proposed/integra-appunti/install").status_code == 404
    assert "/integra-appunti" in [c["name"] for c in tc.get("/commands").json()]


def test_an_installed_skill_rides_the_done_frame_as_an_offer_and_incognito_proposes_nothing(client, monkeypatch):
    tc, _server = client
    _skill("integra-appunti", "Integra gli appunti con il manuale")
    _history(monkeypatch, [])
    assert _done(tc, SAME[0])["offer"]["command"] == "/integra-appunti"
    monkeypatch.setattr("silica.kernel.user_memory.incognito", lambda: True)
    assert "offer" not in _done(tc, SAME[0])
