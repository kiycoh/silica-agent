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


def _block_calls(calls):
    return [c for c in calls if "rewrite one block" in c["messages"][0]["content"]]


def _requests(monkeypatch, message: str):
    from silica.agent import read_scope
    monkeypatch.setattr(read_scope, "REQUESTS", read_scope.ContextVar("t", default=(message,)))


def test_under_the_floor_it_sends_the_text_back_without_a_call(tmp_vault, llm):
    tmp_vault.note("Lit/Short.md", _para(300))
    out = rework.silica_rework(note="Short")
    assert out["ok"] is False and "rewrite it in your reply" in out["error"] and not llm


def test_a_note_is_reworked_block_by_block_into_a_note_beside_it(tmp_vault, llm, monkeypatch):
    from silica.agent import read_scope
    from silica.kernel.write import frontmatter
    monkeypatch.setattr(read_scope, "REQUESTS", read_scope.ContextVar("t", default=()))
    body = "\n\n".join(_para(400) for _ in range(6))
    tmp_vault.note("Lit/Chapter.md", "---\ntags: [x]\n---\n" + body)
    out = rework.silica_rework(note="Chapter", instruction="modernise it")  # no message: MCP
    assert out["ok"] and out["link"] == "[[Chapter (reworked)]]" and out["blocks"] == 2
    work = _block_calls(llm)
    assert len(work) == 2 and all(c["reasoning"] is False for c in work)
    assert "The user's request: modernise it" in work[0]["messages"][0]["content"]
    assert "tags" not in work[0]["messages"][1]["content"]
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
    assert out["ok"] and out["link"].startswith("[[testo0 testo1")
    assert list(Path(tmp_vault.note("Inbox/z.md")).parent.glob("testo0*(reworked).md"))
    sent = " ".join(c["messages"][1]["content"] for c in llm)
    assert "named-notes" not in sent and "image attached" not in sent
    assert _block_calls(llm)[0]["messages"][0]["content"].endswith("Rielabora questo testo:")


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
    assert rework.silica_rework(note="Chapter")["link"] == "[[Chapter (reworked) 2]]"


def test_too_many_blocks_is_refused(tmp_vault, llm, monkeypatch):
    monkeypatch.setattr(rework, "MAX_BLOCKS", 2)
    tmp_vault.note("Lit/Long.md", "\n\n".join(_para(400) for _ in range(9)))
    out = rework.silica_rework(note="Long")
    assert out["ok"] is False and "Over 2 blocks" in out["error"] and not llm


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
    assert _block_calls(llm)[0]["messages"][0]["content"].endswith(
        "The user's request: Riscrivi [[Chapter]] in italiano moderno")


def test_every_result_string_survives_the_eager_projection(tmp_vault, llm, monkeypatch):
    from silica.agent.compaction import generic_projection
    tmp_vault.note("Lit/Short.md", _para(300))
    tmp_vault.note("Lit/A long chapter title of several words.md", "\n\n".join(_para(400) for _ in range(3)))
    for out in (rework.silica_rework(note="Short"),
                rework.silica_rework(note="A long chapter title of several words")):
        assert "re-call to expand" not in generic_projection(out), out


# --- the readable variant: outline headings, block heads, language -----------

def _outline_llm(monkeypatch, reply):
    calls = []

    def fake(model, messages, **kw):
        calls.append({"messages": messages, **kw})
        if "rewrite one block" in messages[0]["content"]:
            return SimpleNamespace(text="RW " + messages[-1]["content"])
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(text=reply)

    monkeypatch.setattr("silica.agent.llm.call_llm", fake)
    monkeypatch.setattr("silica.config.CONFIG.decision_model", "")
    return calls


def test_the_outline_puts_its_headings_where_it_names_and_no_closer_than_the_gap(monkeypatch):
    paras = [_para(200) for _ in range(8)]
    _outline_llm(monkeypatch, "1 | The start\n2 | Too close\n3 | The middle\n99 | Out of range\nnoise")
    out = rework.outline("\n\n".join(paras), "Italian")
    assert [ln for ln in out.split("\n\n") if ln.startswith("## ")] == ["## The start", "## The middle"]
    assert out.split("\n\n")[0] == "## The start" and out.split("\n\n")[3] == "## The middle"


def test_a_text_without_headings_gets_the_outline_and_its_blocks_keep_them(tmp_vault, monkeypatch):
    calls = _outline_llm(monkeypatch, "1 | Il lago\n4 | La minaccia")
    tmp_vault.note("Lit/Chapter.md", "\n\n".join(_para(400) for _ in range(6)))
    assert rework.silica_rework(note="Chapter")["ok"]
    outline_call = [c for c in calls if "numbered [1]" in c["messages"][0]["content"]]
    assert len(outline_call) == 1 and outline_call[0].get("reasoning") is None
    written = (Path(tmp_vault.note("Lit/x.md")).parent / "Chapter (reworked).md").read_text()
    assert written.index("## Il lago") < written.index("## La minaccia")


def test_a_headed_text_gets_no_outline(tmp_vault, monkeypatch):
    calls = _outline_llm(monkeypatch, "1 | Never asked")
    body = "\n\n".join(f"## Part {i}\n\n" + _para(400) for i in range(4))
    tmp_vault.note("Lit/Headed.md", body)
    assert rework.silica_rework(note="Headed")["ok"]
    assert not [c for c in calls if "numbered [1]" in c["messages"][0]["content"]]


def test_a_failed_outline_still_writes_the_rework(tmp_vault, monkeypatch):
    _outline_llm(monkeypatch, RuntimeError("provider down"))
    tmp_vault.note("Lit/Chapter.md", "\n\n".join(_para(400) for _ in range(3)))
    assert rework.silica_rework(note="Chapter")["ok"]


def test_a_heading_opening_a_mid_text_block_is_dropped_unless_the_source_opens_with_one(monkeypatch):
    monkeypatch.setattr("silica.agent.llm.call_llm",
                        lambda model, messages, **kw: SimpleNamespace(text="## Invented\n\nBody."))
    assert rework._rework("words mid-section", "x", "Italian", None) == "Body."
    assert rework._rework("## Real\n\nwords", "x", "Italian", None).startswith("## Invented")


def test_the_blocks_are_told_the_texts_language_by_name(tmp_vault, llm):
    italian = " ".join(["Il curato tornava a casa e la sera era tranquilla, ma non sapeva che cosa"] * 100)
    tmp_vault.note("Lit/Capitolo.md", "\n\n".join([italian] * 4))
    rework.silica_rework(note="Capitolo")
    assert "Write in Italian." in _block_calls(llm)[0]["messages"][0]["content"]


# --- 2026-10-22, hosted t06: routed by weight, and a discursive register -----

def _jev(monkeypatch, kind, register="readable"):
    monkeypatch.setattr("silica.config.CONFIG.decision_model", "typesafe/jev")
    monkeypatch.setattr("silica.agent.decide.decide", lambda state, q: {
        "kind": {"choice": kind, "probabilities": {kind: 0.9}},
        "register": {"choice": register, "probabilities": {register: 0.9}}})


def test_a_condensed_rework_that_fits_the_reply_goes_back_to_the_chat(tmp_vault, llm, monkeypatch):
    _jev(monkeypatch, "condensed")
    _requests(monkeypatch, "Rielabora [[Chapter]] sintetizzando dove possibile")
    tmp_vault.note("Lit/Chapter.md", "\n\n".join(_para(400) for _ in range(3)))
    out = rework.silica_rework(note="Chapter")
    assert out["ok"] is False and "condense it yourself" in out["error"] and not llm


def test_a_condensed_rework_too_big_for_the_window_runs_in_blocks(tmp_vault, llm, monkeypatch):
    """Hosted t06 at 32k: the chapter and the note written from it did not fit,
    and the chat re-read the chapter 14 times. Here the blocks do it."""
    _jev(monkeypatch, "condensed")
    monkeypatch.setattr("silica.config.CONFIG.max_context_tokens", 4000)
    _requests(monkeypatch, "Rielabora [[Chapter]] sintetizzando dove possibile")
    tmp_vault.note("Lit/Chapter.md", "\n\n".join(_para(400) for _ in range(3)))
    assert rework.silica_rework(note="Chapter")["ok"]
    prompt = _block_calls(llm)[0]["messages"][0]["content"]
    assert "shorter where it can be: about 60% of its length" in prompt


def test_discursive_blocks_get_the_prose_rules_and_the_text_before_them(tmp_vault, llm, monkeypatch):
    _jev(monkeypatch, "full", "discursive")
    _requests(monkeypatch, "Riscrivi [[Chapter]] in modo discorsivo e lineare")
    tmp_vault.note("Lit/Chapter.md", "\n\n".join(_para(400, f"w{k}x") for k in range(6)))
    assert rework.silica_rework(note="Chapter")["ok"]
    prompts = [c["messages"][0]["content"] for c in _block_calls(llm)]
    assert len(prompts) >= 2 and all("No bulleted lists and no bold labels" in p for p in prompts)
    assert "The text just before this block" not in prompts[0]
    assert "The text just before this block" in prompts[1] and "w2x399" in prompts[1]
    assert all(p.endswith("The user's request: Riscrivi [[Chapter]] in modo discorsivo e lineare")
               for p in prompts)


def test_a_shorter_version_asked_discursive_says_so_to_the_chat(tmp_vault, llm, monkeypatch):
    _jev(monkeypatch, "shorter", "discursive")
    _requests(monkeypatch, "Fammi un riassunto discorsivo di [[Chapter]]")
    tmp_vault.note("Lit/Chapter.md", "\n\n".join(_para(400) for _ in range(3)))
    out = rework.silica_rework(note="Chapter")
    assert out["ok"] is False and "connected prose, without lists" in out["error"] and not llm


def test_an_unsure_register_keeps_the_measured_default(tmp_vault, llm, monkeypatch):
    monkeypatch.setattr("silica.config.CONFIG.decision_model", "typesafe/jev")
    monkeypatch.setattr("silica.agent.decide.decide", lambda state, q: {
        "kind": {"choice": "full", "probabilities": {"full": 0.9}},
        "register": {"choice": "discursive", "probabilities": {"discursive": 0.55}}})
    tmp_vault.note("Lit/Chapter.md", "\n\n".join(_para(400) for _ in range(3)))
    assert rework.silica_rework(note="Chapter")["ok"]
    assert "Short paragraphs, one idea each." in _block_calls(llm)[0]["messages"][0]["content"]
