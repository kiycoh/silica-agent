# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""silica_session_search (ADR-0063): earlier chats with this vault, found by
their words, each hit with the other half of its exchange."""
from __future__ import annotations

from silica.agent import narration
from silica.tools.sessions import silica_session_search


def _session(*msgs: dict, driver: str = "tui") -> str:
    """A closed narration session holding `msgs`, for the current vault."""
    nar = narration.Narrator()
    sid = nar.ensure_session(driver=driver)
    for m in msgs:
        nar.turn(m)
    nar.close()
    return sid


def _u(text: str, **kw) -> dict:
    return {"role": "user", "content": text, **kw}


def _a(text: str | None, **kw) -> dict:
    return {"role": "assistant", "content": text, **kw}


def test_an_earlier_exchange_is_found_with_its_answer():
    sid = _session(
        _u("which reranker did we settle on?"),
        _a(None, tool_calls=[{"id": "c1", "function": {"name": "silica_search", "arguments": "{}"}}]),
        {"role": "tool", "tool_call_id": "c1", "content": "mxbai notes"},
        _a("We kept mxbai-rerank-base-v2 over bge."),
    )
    out = silica_session_search("reranker")
    [hit] = out["hits"]
    assert (hit["session"], hit["role"]) == (sid, "user")
    assert hit["answer"] == "We kept mxbai-rerank-base-v2 over bge."
    assert out["sessions_searched"] == 1


def test_tool_results_directives_and_the_open_session_are_not_searched(monkeypatch):
    _session(
        _u("Summarize from the vault: zebra", origin="cli"),
        {"role": "tool", "tool_call_id": "c1", "content": "zebra zebra zebra"},
        _a("Nothing on that."),
    )
    open_sid = _session(_u("zebra again"))
    monkeypatch.setattr(narration.NARRATOR, "_sid", open_sid)
    assert silica_session_search("zebra")["hits"] == []


def test_another_vault_and_an_mcp_session_stay_out(tmp_path, monkeypatch):
    from silica.config import CONFIG

    this_vault = CONFIG.vault_path
    _session(_u("tell me about okapi"), driver="mcp")
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path / "other"))
    _session(_u("okapi in the other vault"))
    # Not monkeypatch.undo(): that would also undo conftest's sandbox of the home.
    monkeypatch.setattr(CONFIG, "vault_path", this_vault)
    assert silica_session_search("okapi")["hits"] == []


def test_one_exchange_is_one_hit_and_a_session_fills_two_slots_at_most():
    _session(_u("gecko facts?"), _a("Geckos lick their eyes, gecko trivia."),
             _u("more gecko?"), _a("A gecko has no eyelids."),
             _u("gecko once more"), _a("Gecko feet stick."))
    hits = silica_session_search("gecko", k=10)["hits"]
    assert len(hits) == 2
    assert len({(h["role"], h["text"]) for h in hits}) == 2
