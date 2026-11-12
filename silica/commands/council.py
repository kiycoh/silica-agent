# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/council (ADR-0170): one search of the vault, the same question and the same
passages to several models at once, then the chat model's synthesis of where
they agree and where they part, each member's own answer folded under it.

Code fans out, the model never does (ADR-0158): the members get no tools, so
none of them searches again, and the one recall call is the chat's own."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event

from silica.command_table import Call, Result
from silica.commands import answered
from silica.config import CONFIG, demo, hosted
from silica.prompts import GENERAL_HEAD, GENERAL_MARK

logger = logging.getLogger(__name__)

MIN_MEMBERS, MAX_MEMBERS = 2, 5
# One attempt's CONFIG.llm_timeout (130 s) and the start of a retry: a member
# past it is named and skipped, so a dead endpoint costs the turn this long and
# not one per retry. shortcut: fixed, not a setting; make it one at the first
# member a person's own setup needs more for.
MEMBER_TIMEOUT_S = 180.0

_LETTERS = "ABCDE"


def members() -> list[str]:
    """The council's models, as SILICA_COUNCIL_MODELS names them. A model named
    twice sits twice: two samples of one model are a council too."""
    return [m.strip() for m in (CONFIG.council_models or "").split(",") if m.strip()]


@dataclass
class _Heard:
    label: str           # "A", by place in the setting, so one model named twice is two members
    model: str
    text: str = ""
    tokens: int | None = None
    failed: str = ""

    @property
    def name(self) -> str:
        return f"{self.label} · {self.model.rsplit('/', 1)[-1]}"


def _member_messages(question: str, context: str) -> list[dict]:
    if CONFIG.general_knowledge:
        empty = ("When the passages do not answer, say so in one line, then answer from what you know "
                 f'inside one block that opens with this line, every line of it starting with "> ":\n{GENERAL_HEAD}')
    else:
        empty = "When the passages do not answer, say so in one line and stop."
    system = (
        "You are one member of a council: several models answer the same question from the same "
        "passages of the person's notes, and another model compares the answers.\n"
        "Answer from the passages below, and cite each claim with the note it comes from as a "
        f"wikilink, [[path]]. {empty}\n"
        "Reply in the language of the question.")
    passages = context.strip() or "(the search of the notes found nothing)"
    return [{"role": "system", "content": system},
            {"role": "user", "content": f"Question: {question}\n\nPassages from the notes:\n{passages}"}]


def _passages(question: str) -> str:
    """The body search's passages, for a recall that found nothing: a vault whose
    recall indexes were never built (no /cooccur, no /embed) answers nothing
    there while the passage index, which builds itself, still finds the notes.
    The chat's model turns to it then, and so does the council (seen live
    2026-11-12 on a fresh copy of the test vault)."""
    from silica.tools.atomic import silica_search_context

    hits = answered(silica_search_context, question).get("hits") or []
    return "\n\n".join(f"[[{h['path']}]], {h.get('section') or 'line ' + str(h.get('line'))}:\n{h.get('snippet', '')}"
                       for h in hits if isinstance(h, dict) and h.get("path"))


def _synthesis_messages(question: str, heard: list[_Heard]) -> list[dict]:
    system = (
        "You compare a council's answers to one question, each written from the same passages of "
        "the person's notes. Write three parts, each under a short heading in the question's "
        "language: what the members agree on, with the citations they gave; where they disagree, "
        "each position attributed to its member by name and cited; what none of them could support "
        "from the notes. Keep every [[citation]] as a member wrote it and add none of your own. "
        f"What a member marked {GENERAL_MARK} stays in a block that opens with \"{GENERAL_HEAD}\", "
        "attributed to that member, never in cited prose. Write nothing else: no preamble and no "
        "answer of your own.")
    said = "\n\n".join(f"## Member {h.name}\n{h.text}" for h in heard)
    return [{"role": "system", "content": system},
            {"role": "user", "content": f"Question: {question}\n\n{said}"}]


def _ask(heard: _Heard, messages: list[dict]) -> _Heard:
    """One member, on its model alone and inside its time: a failure is the
    member's to report, never the council's to raise."""
    from silica.model.llm import call_llm, run_with_deadline

    stop = Event()
    try:
        resp = run_with_deadline(lambda: call_llm(heard.model, messages, cancel=stop, fallback=False),
                                 MEMBER_TIMEOUT_S, abandon=stop)
    except TimeoutError:
        heard.failed = f"no answer in {MEMBER_TIMEOUT_S:.0f} s"
        return heard
    except Exception as e:
        # Any failure of one member (a refused key, a model its endpoint does
        # not have, a limit) is named under the answer and the others go on.
        logger.warning("council member %s failed: %s", heard.model, e)
        heard.failed = type(e).__name__
        return heard
    heard.text = (resp.text or "").strip()
    heard.failed = "" if heard.text else "an empty answer"
    heard.tokens = _tokens(resp.usage)
    return heard


def _tokens(usage: dict | None) -> int | None:
    u = usage or {}
    total = (u.get("prompt_tokens") or 0) + (u.get("completion_tokens") or 0)
    return total or None


def _meter(name: str, heard: _Heard) -> dict:
    """One member's line under the answer: its calls and, when the endpoint
    reported them, its tokens; or why it was skipped."""
    if heard.failed:
        return {"kind": "council", "text": f"{name}: skipped, {heard.failed}"}
    tokens = f", {heard.tokens:,} tokens" if heard.tokens else ""
    return {"kind": "council", "text": f"{name}: 1 call{tokens}"}


def _drawn(synthesis: str, heard: list[_Heard]) -> str:
    """The synthesis, then each member's whole answer folded under it."""
    folds = "\n\n".join(f"<details>\n<summary>Member {h.name}</summary>\n\n{h.text}\n\n</details>"
                        for h in heard if h.text)
    return f"{synthesis.strip()}\n\n{folds}"


def council(c: Call) -> Result:
    """/council "<question>": see the module."""
    from silica.agent import general_knowledge, turn_notes
    from silica.model.llm import call_llm
    from silica.tools.graph import silica_recall

    out = c.out
    if hosted() or demo():
        return out.refused("/council is not on the server in this version: it is a model call per "
                           "member and one more, which the account's count does not take yet")
    question = " ".join(c.args).strip()
    if not question:
        return out.refused(f"Usage: {c.row.synopsis}")
    names = members()
    if not names:
        return out.refused(
            "/council has no members yet. Name 2 to 5 models in Settings › Brains › council models, "
            "or as SILICA_COUNCIL_MODELS=<model>,<model> in ~/.silica/.env, as litellm ids (the "
            "syntax of SILICA_FALLBACK_MODELS). Silica picks none for you: each one is a call on your key.")
    if not MIN_MEMBERS <= len(names) <= MAX_MEMBERS:
        return out.refused(f"/council takes {MIN_MEMBERS} to {MAX_MEMBERS} members, and "
                           f"SILICA_COUNCIL_MODELS names {len(names)}")
    found = answered(silica_recall, question)
    if "error" in found:
        return out.refused(f"the search of your notes failed: {found['error']}")
    asked = _member_messages(question, str(found.get("context") or "") or _passages(question))
    heard = [_Heard(_LETTERS[i], m) for i, m in enumerate(names)]
    with ThreadPoolExecutor(max_workers=len(heard)) as pool:
        heard = list(pool.map(lambda h: _ask(h, asked), heard))
    answering = [h for h in heard if h.text]
    if not answering:
        return out.refused("no member answered: " + "; ".join(f"{h.name}, {h.failed}" for h in heard))
    resp = call_llm(CONFIG.model, _synthesis_messages(question, answering))
    synthesis = (resp.text or "").strip() or "(the synthesis came back empty: the members' answers are below)"
    out.answer = _drawn(synthesis, answering)
    notes = general_knowledge.general_notes(out.answer, looked=True, command=False)
    notes += [_meter(f"member {h.name}", h) for h in heard]
    tokens = _tokens(resp.usage)
    notes.append({"kind": "council", "text": f"synthesis: 1 call{f', {tokens:,} tokens' if tokens else ''}"})
    out.notes = turn_notes.ordered(notes)
    return out
