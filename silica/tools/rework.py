# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Rework a long text in full, block by block, into a new note (ADR-0116).

Why a tool and not the chat's own answer: asked to rework a long text in one
call, the hosted chat model condenses it whatever the prompt or the wording
(0.10 to 0.17 of a 6,101-word chapter), while the same text cut into blocks of
about a thousand words comes back at 0.92 to 1.01
(docs/evaluation/rework-chunked-2026-10-16.md). The result goes to a note so a
rework of 15k tokens never enters the chat history, where the pre-call fit can
stub only tool results and the 32k cap would not hold (ADR-0113).
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

from pydantic import Field

from silica.tools import tool

logger = logging.getLogger(__name__)

# At 682 words the chat keeps 0.84 to 1.01 of a text it reworks itself, and
# 0.73 to 1.00 after this tool sends it back; at 1,191 words it falls to 0.37.
FLOOR_WORDS = 700
# Blocks of 863 to 1,643 words were reworked at 0.92 to 1.03; nothing smaller
# or larger was measured.
BLOCK_WORDS = 1000
# A heading ends a block that already holds this many words, so a section is
# not cut a few lines after its heading. Half a block, never measured: reopen
# with a headed text in the rework probe.
HEADING_CUT = 500
# A last block this short joins the one before it rather than going alone.
TAIL_WORDS = 300
# A paragraph this long (a PDF's text, a paste with no blank lines) is cut on
# its lines, then on its sentences, or the whole text would be one block and
# condense like the chat does.
LONG_UNIT = 1500
# 40 blocks cost about 0.013 USD at the measured 0.0016 for 5, an eighth of a
# Free month (0.10 USD, ADR-0102).
MAX_BLOCKS = 40
# The probes ran 5 or 6 calls at once; 8 bounds a 40-block text's threads on
# the hosted unit (TasksMax 256, shared with litellm's own pool).
WORKERS = 8
# A wrong "shorter" sends the request back to the chat, which condenses it (the
# failure this tool exists for); a wrong "full" writes a note the person can
# delete. So "shorter" needs a margin, the floor forms.SNIFF_ACCEPT and
# read_scope.PART_ACCEPT use. Reopen with 100 labelled requests.
SHORTER_ACCEPT = 0.6

_WORKER_PROMPT = (
    "You rewrite one block of a longer text that the user asked to have reworked. "
    "Rewrite this block only: keep every passage, in order, at roughly its original "
    "length; change the wording, not the content. Do not add facts, comments or "
    "explanations that are not in the text. Reply with the rewritten text only: no "
    "preamble, no heading, no closing remark, no offer of other versions. Write in "
    "the language of the text.\n\nThe user's request: {instruction}")

_QUESTION = {"kind": {
    "type": "choice",
    "instructions": ("The user asked the assistant to do something with a long text. Does the "
                     "request ask for the whole text rewritten at about its length, or for "
                     "something shorter than the text?"),
    "criteria": {
        "full": ("rework, rewrite, rephrase, paraphrase or modernise the text, simplify its "
                 "wording or change its register, with nothing said about making it shorter"),
        "shorter": ("a summary, an outline, study notes, key points, or a shorter or "
                    "condensed version of the text"),
    },
}}

# The marker server.run_turn appends for an attached image (ui/web/server.py,
# _ATTACHED_RE). Repeated here: importing the web server from a tool would pull
# FastAPI into every surface that registers tools.
_IMAGE_RE = re.compile(r"\n*\[image attached: [^\]\n]+\]")
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|#^\[\]]+')


def _words(text: str) -> int:
    return len(text.split())


def _units(text: str) -> list[tuple[str, str]]:
    """(separator before it, text) pieces: paragraphs, and the lines or
    sentences of a paragraph over LONG_UNIT words, so the blocks can be joined
    back with the text's own separators."""
    out: list[tuple[str, str]] = []
    for para in re.split(r"\n\s*\n", text.strip()):
        if not para.strip():
            continue
        if _words(para) <= LONG_UNIT:
            out.append(("\n\n", para))
            continue
        lines = para.split("\n")
        pieces = [("\n", ln) for ln in lines] if len(lines) > 1 else [(" ", s) for s in _SENTENCE_RE.split(para)]
        out.append(("\n\n", pieces[0][1]))
        out.extend(pieces[1:])
    return out


def blocks(text: str) -> list[str]:
    """The text cut into blocks of at least BLOCK_WORDS words, on headings
    first and paragraphs second (ADR-0116 point 3)."""
    groups: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    count = 0
    for sep, piece in _units(text):
        is_heading = piece.lstrip().startswith("#")
        if cur and (count >= BLOCK_WORDS or (is_heading and count >= HEADING_CUT)):
            groups.append(cur)
            cur, count = [], 0
        cur.append((sep, piece))
        count += _words(piece)
    if cur:
        if groups and count < TAIL_WORDS:
            groups[-1].extend(cur)
        else:
            groups.append(cur)
    return [g[0][1] + "".join(sep + piece for sep, piece in g[1:]) for g in groups]


def _request() -> str:
    """The user's current message as they wrote it: without the named-notes
    block and the image markers the server appends to it."""
    from silica.agent import mentions
    from silica.agent.read_scope import REQUESTS

    requests = REQUESTS.get()
    return _IMAGE_RE.sub("", mentions.strip(requests[-1])).strip() if requests else ""


def _wants_shorter(request: str) -> bool:
    from silica.agent.decide import decide
    from silica.config import CONFIG

    if not CONFIG.decision_model or not request:
        return False
    answers = decide({"current_request": request[:400]}, _QUESTION)
    if not answers:
        return False
    verdict = answers["kind"]
    return verdict["choice"] == "shorter" and verdict["probabilities"].get("shorter", 0.0) > SHORTER_ACCEPT


def _rework(block: str, instruction: str, cancel_token: Any) -> str:
    from silica.agent.llm import call_llm
    from silica.config import CONFIG

    messages = [{"role": "system", "content": _WORKER_PROMPT.format(instruction=instruction)},
                {"role": "user", "content": block}]
    # Thinking off: on two texts it was the most faithful arm (FActScore 0.966
    # and 0.996, against 0.879 and 0.965 with thinking on), with the fewest
    # language errors, at a fifth of the cost and a quarter of the wait.
    out = (call_llm(CONFIG.model, messages, reasoning=False, cancel=cancel_token).text or "").strip()
    if not out:
        raise RuntimeError("the model returned no text")
    return out


def _target(folder: str, stem: str) -> str:
    from silica.config import CONFIG

    vault = Path(CONFIG.vault_path)
    base = str(PurePosixPath(folder) / f"{stem} (reworked)") if folder not in ("", ".") else f"{stem} (reworked)"
    path, n = f"{base}.md", 2
    while (vault / path).exists():
        path, n = f"{base} {n}.md", n + 1
    return path


@tool(cls="composed", collapse="eager")
def silica_rework(
    note: Annotated[str | None, Field(description="Name or path of the note to rework. Leave it out when the text is pasted in the user's message: the tool takes it from there.")] = None,
    instruction: Annotated[str | None, Field(description="What the user asked for, in their words: register, audience, purpose.")] = None,
    cancel_token=None,
) -> dict[str, Any]:
    """Rework a long text in full: rewrite, rephrase, modernise or simplify its wording, or change its register, keeping every passage in order at about its length. The text is a note, or a text of more than about 700 words the user pasted in their message. The tool reads it itself and writes the result to a new note, so call it directly: do not read the note first, and do not copy a pasted text into the call. Use it whenever the user asks to rework, rewrite, rephrase or modernise such a text. Not for a summary, an outline, study notes or any shorter version: those are not a rework, do them yourself."""
    from silica.config import CONFIG
    from silica.driver import DRIVER
    from silica.kernel.write import frontmatter
    from silica.tools.notes import silica_write_note

    request = _request()
    if note:
        try:
            src = DRIVER.read_note(note)
        except Exception as e:
            return {"error": f"Note '{note}' not found ({e})."}
        text = frontmatter.split(src.content)[2]
        title = PurePosixPath(src.ref.path or note).stem
        folder = str(PurePosixPath(src.ref.path or note).parent)
    else:
        text = request
        title = None
        folder = CONFIG.inbox_dir
    words = _words(text)
    if not words:
        return {"error": "No note was named and the message holds no text to rework."}
    if words < FLOOR_WORDS:
        return {"ok": False, "words": words,
                "error": (f"The text is {words} words, under the tool's floor of {FLOOR_WORDS}: "
                          "rewrite it yourself in your reply, every passage in order, at about its length.")}
    if _wants_shorter(request):
        return {"ok": False, "error": ("The request asks for something shorter than the text: this tool "
                                       "keeps all of it. Write the summary or the shorter version yourself.")}
    parts = blocks(text)
    if len(parts) > MAX_BLOCKS:
        return {"ok": False, "error": (f"The text is {words} words ({len(parts)} blocks), over the tool's "
                                       f"limit of {MAX_BLOCKS} blocks. Ask for one part of it at a time.")}

    # The person's own words, not the chat's `instruction`: the chat paraphrases
    # and adds what nobody asked for ("paragrafi tematici e tabelle quando
    # utile" came back at 0.70 of the length, 2026-10-16), and every measured
    # block ran on the request as written. `instruction` serves a call with no
    # message behind it (MCP).
    ask = re.split(r"\n\s*\n", request, maxsplit=1)[0][:300] if request else (instruction or "Rework this text")
    with ThreadPoolExecutor(min(WORKERS, len(parts))) as pool:
        futures = [pool.submit(_rework, p, ask, cancel_token) for p in parts]
        outs: list[str] = []
        for k, fut in enumerate(futures):
            try:
                outs.append(fut.result())
            except Exception as e:
                # All or nothing: a note missing one block would read as a
                # complete rework with a passage gone, the failure this exists for.
                return {"ok": False, "error": f"Block {k + 1} of {len(parts)} failed ({e}); nothing was written."}

    body = "\n\n".join(outs)
    # A pasted text's name comes from its own first words, not from the request
    # line above it ("Rielabora questo testo:").
    head, _, rest = text.partition("\n")
    naming = rest if rest.strip() and _words(head) < 20 else text
    stem = title or _UNSAFE_NAME.sub("", " ".join(naming.split()[:6])).strip(" ,.;:!?") or "Pasted text"
    path = _target(folder, stem)
    written = silica_write_note(path=path, body=body, template="none",
                                props={"source": f"[[{title}]]"} if title else None)
    if written.get("error"):
        return {"ok": False, "error": written["error"]}
    return {"ok": True, "path": written.get("path", path), "words_in": words,
            "words_out": _words(body), "blocks": len(parts),
            # "Link the new note" read as "link it from the source": one run patched
            # the person's note with a pointer to the rework (2026-10-16).
            "reply": ("Answer in one line that gives the person a [[wikilink]] to the new note. "
                      "Do not paste its text, and do not edit the source note.")}
