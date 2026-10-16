# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""silica_rework (ADR-0116): blocks, guards, sources, all-or-nothing write."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from silica.tools import rework


def _para(n: int, word: str = "parola") -> str:
    return " ".join(f"{word}{i}." if i % 12 == 11 else f"{word}{i}" for i in range(n))


def _norm(s: str) -> str:
    return " ".join(s.split())


# --- blocks -----------------------------------------------------------------

def test_blocks_hold_a_thousand_words_and_keep_every_word():
    text = "\n\n".join(_para(300) for _ in range(10))
    parts = rework.blocks(text)
    assert all(rework._words(p) >= rework.BLOCK_WORDS for p in parts[:-1])
    assert rework._words(parts[-1]) >= rework.TAIL_WORDS
    assert _norm(" ".join(parts)) == _norm(text)


def test_a_heading_ends_a_block_past_half_its_size():
    text = "\n\n".join([_para(300), _para(300), "## Second part", _para(300), _para(300), _para(300)])
    parts = rework.blocks(text)
    assert parts[1].startswith("## Second part")


def test_a_short_tail_joins_the_block_before():
    text = "\n\n".join([_para(500), _para(500), _para(100)])
    assert len(rework.blocks(text)) == 1


def test_a_paragraph_without_blank_lines_is_cut_on_lines_then_sentences():
    lines = "\n".join(_para(100) for _ in range(30))
    assert len(rework.blocks(lines)) == 3
    one_line = " ".join(_para(120) for _ in range(25))
    parts = rework.blocks(one_line)
    assert len(parts) >= 2 and _norm(" ".join(parts)) == _norm(one_line)


# --- the tool ---------------------------------------------------------------

@pytest.fixture
def llm(monkeypatch):
    calls = []

    def fake(model, messages, **kw):
        calls.append({"messages": messages, **kw})
        return SimpleNamespace(text="RW " + messages[-1]["content"])

    monkeypatch.setattr("silica.agent.llm.call_llm", fake)
    monkeypatch.setattr("silica.config.CONFIG.decision_model", "")
    return calls


def _requests(monkeypatch, message: str):
    from silica.agent import read_scope
    monkeypatch.setattr(read_scope, "REQUESTS", read_scope.ContextVar("t", default=(message,)))


def test_under_the_floor_it_sends_the_text_back_without_a_call(tmp_vault, llm):
    tmp_vault.note("Lit/Short.md", _para(300))
    out = rework.silica_rework(note="Short")
    assert out["ok"] is False and "rewrite it yourself" in out["error"] and not llm


def test_a_note_is_reworked_block_by_block_into_a_note_beside_it(tmp_vault, llm):
    from silica.kernel.write import frontmatter
    body = "\n\n".join(_para(400) for _ in range(6))
    tmp_vault.note("Lit/Chapter.md", "---\ntags: [x]\n---\n" + body)
    out = rework.silica_rework(note="Chapter", instruction="modernise it")  # no message: MCP
    assert out["ok"] and out["path"] == "Lit/Chapter (reworked).md" and out["blocks"] == 2
    assert all(c["reasoning"] is False for c in llm)
    assert "The user's request: modernise it" in llm[0]["messages"][0]["content"]
    assert "tags" not in llm[0]["messages"][1]["content"]
    data, _, written = frontmatter.split((Path(tmp_vault.note("Lit/x.md")).parent / "Chapter (reworked).md").read_text())
    assert data["source"] == "[[Chapter]]"
    assert written.count("RW ") == 2 and _norm(written.replace("RW ", "")) == _norm(body)


def test_a_pasted_text_comes_from_the_message_without_what_the_server_appends(tmp_vault, llm, monkeypatch):
    text = "\n\n".join(_para(400, "testo") for _ in range(3))
    message = ("Rielabora questo testo:\n\n" + text
               + "\n\n<named-notes>\nNotes the user named (read one before answering about its content): A.md\n"
               "</named-notes>\n\n[image attached: /tmp/x.png]")
    _requests(monkeypatch, message)
    out = rework.silica_rework()
    assert out["ok"] and out["path"].startswith("Inbox/testo0 testo1")
    sent = " ".join(c["messages"][1]["content"] for c in llm)
    assert "named-notes" not in sent and "image attached" not in sent
    assert llm[0]["messages"][0]["content"].endswith("Rielabora questo testo:")


def test_one_failed_block_writes_nothing(tmp_vault, monkeypatch):
    monkeypatch.setattr("silica.config.CONFIG.decision_model", "")
    n = {"i": 0}

    def flaky(model, messages, **kw):
        n["i"] += 1
        return SimpleNamespace(text="" if n["i"] == 2 else "ok")

    monkeypatch.setattr("silica.agent.llm.call_llm", flaky)
    tmp_vault.note("Lit/Chapter.md", "\n\n".join(_para(400) for _ in range(6)))
    out = rework.silica_rework(note="Chapter")
    assert out["ok"] is False and "nothing was written" in out["error"]
    assert not list(Path(tmp_vault.note("Lit/y.md")).parent.glob("*reworked*"))


def test_jev_saying_shorter_sends_the_request_back(tmp_vault, llm, monkeypatch):
    monkeypatch.setattr("silica.config.CONFIG.decision_model", "typesafe/jev")
    verdict = {"choice": "shorter", "probabilities": {"shorter": 0.9, "full": 0.1}}
    monkeypatch.setattr("silica.agent.decide.decide", lambda state, q: {"kind": verdict})
    _requests(monkeypatch, "Rielabora [[Chapter]] in una versione più breve")
    tmp_vault.note("Lit/Chapter.md", "\n\n".join(_para(400) for _ in range(3)))
    out = rework.silica_rework(note="Chapter")
    assert out["ok"] is False and "shorter" in out["error"] and not llm
    verdict["probabilities"] = {"shorter": 0.55, "full": 0.45}
    assert rework.silica_rework(note="Chapter")["ok"]


def test_an_existing_rework_is_not_overwritten(tmp_vault, llm):
    tmp_vault.note("Lit/Chapter.md", "\n\n".join(_para(400) for _ in range(3)))
    tmp_vault.note("Lit/Chapter (reworked).md", "earlier")
    assert rework.silica_rework(note="Chapter")["path"] == "Lit/Chapter (reworked) 2.md"


def test_too_many_blocks_is_refused(tmp_vault, llm, monkeypatch):
    monkeypatch.setattr(rework, "MAX_BLOCKS", 2)
    tmp_vault.note("Lit/Long.md", "\n\n".join(_para(400) for _ in range(9)))
    out = rework.silica_rework(note="Long")
    assert out["ok"] is False and "limit of 2 blocks" in out["error"] and not llm


def test_the_tool_sits_in_the_chat_core_before_the_loader():
    from silica.agent.constraints import CHAT_CORE
    from silica.tools import TOOLS
    import silica.tools.composed  # noqa: F401
    assert CHAT_CORE[-2:] == ("silica_rework", "silica_load_tools")
    props = TOOLS["silica_rework"].json_schema()["function"]["parameters"]["properties"]
    assert set(props) == {"note", "instruction"}


def test_the_blocks_get_the_persons_words_not_the_chats_paraphrase(tmp_vault, llm, monkeypatch):
    _requests(monkeypatch, "Riscrivi [[Chapter]] in italiano moderno")
    tmp_vault.note("Lit/Chapter.md", "\n\n".join(_para(400) for _ in range(3)))
    rework.silica_rework(note="Chapter", instruction="usa paragrafi tematici e tabelle")
    assert llm[0]["messages"][0]["content"].endswith("The user's request: Riscrivi [[Chapter]] in italiano moderno")
