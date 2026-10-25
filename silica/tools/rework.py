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
# A condensed rework's target, of the text's length: hosted t06's own condensed
# note, written by the chat from "sintetizzando dove possibile", came to 0.63
# of its 4.7k-word chapter (2026-10-22). Never measured as a block target.
CONDENSED_SHARE = 0.6

# The readable variant (docs/evaluation/rework-quality-2026-10-16.md): the light
# paraphrase it replaces kept 0.99 of the facts at Gulpease 43 and no headings;
# these rules keep 0.97 at Gulpease 66, with the text's headings and the outline's.
# "Old documents become present-day language": told to keep quotations exact, the
# blocks copied Manzoni's decrees verbatim and the judge marked the rework down for
# it. The language is named, not "the language of the text": under English
# instructions Qwen wrote 17 of 65 paragraphs of one Italian rework in English.
_WORKER_PROMPT = (
    "You rewrite one block of a longer text that the user asked to have reworked. {length}\n\n"
    "How to write it:\n{rules}\n\n"
    "Reply with the rewritten text only: no preamble, no title, no closing remark, no offer of "
    "other versions. Write in {language}.\n\n"
    "The user's request: {instruction}")

_LENGTH = {
    "full": ("Rewrite the whole block, in order: every event, fact, name, number and date stays; "
             "the wording changes. Do not add facts, comments or explanations that are not in the text."),
    "condensed": ("Rewrite the whole block, in order, shorter where it can be: about {share} of its "
                  "length. Every concept, technical term, name, number and date stays; what goes is "
                  "repetition and wording. Do not add facts, comments or explanations that are not in "
                  "the text."),
}

_RULES = {
    "readable": (
        "- Clear present-day prose, as a good textbook writes it: most sentences between 12 and 25 "
        "words. Split a long sentence in two or three; never write a string of very short sentences.\n"
        "- Old or literary language becomes present-day language, quoted old documents included; "
        "keep in quotation marks only a phrase the text is famous for. Keep dialogue as dialogue, "
        "and keep who says each line.\n"
        "- Short paragraphs, one idea each.\n"
        "- Keep the text's headings at their level. Inside a section, where the subject changes, add "
        "a ### sub-heading of two to five plain words; in a story a whole scene is one subject, so "
        "never put a heading inside a dialogue.\n"
        "- Where the text lists three or more parallel things, make a bulleted list. Where it treats "
        "several kinds or parts one after another, open each with a bold label, like \"**Mechanical "
        "barriers.**\". Everything else stays prose.\n"
        "- Keep formulas, tables and links exact. Add no formula, table or diagram unless the user's "
        "request asks for one."),
    # Asked for by name only (_QUESTION's "register"): hosted t06 read the
    # readable form of a psychopathology chapter and asked for discursive,
    # linear reasoning with its beginning and end connected (2026-10-22).
    # Measured on the three 6k-word texts of 2026-10-16 (2 runs each, V4.1
    # Flash judging both orders, 2026-10-23): flow 5-1 against readable, study
    # 3-3, facts and key points equal, Gulpease 45 against 49. Handing each
    # block the end of the one before moved nothing, as ADR-0116 found.
    "discursive": (
        "- Connected prose, as a good textbook explains a subject: each paragraph follows from the "
        "one before and says how, by a cause, a consequence, a contrast or an example, so the block "
        "reads as one line of reasoning from its first sentence to its last.\n"
        "- Most sentences between 12 and 25 words. Split a long sentence in two or three; never "
        "write a string of very short sentences.\n"
        "- Old or literary language becomes present-day language, quoted old documents included; "
        "keep in quotation marks only a phrase the text is famous for. Keep dialogue as dialogue, "
        "and keep who says each line.\n"
        "- Keep the text's headings at their level and add none of your own.\n"
        "- No bulleted lists and no bold labels: parallel things become a sentence that names them, "
        "in the text's order. Keep a list only where the text itself is one, such as numbered "
        "steps. Emphasis at most once a paragraph.\n"
        "- Keep formulas, tables and links exact. Add no formula, table or diagram unless the user's "
        "request asks for one."),
}

_OUTLINE_PROMPT = (
    "The user gives you a text whose paragraphs are numbered [1], [2], ... Split it into its main "
    "sections, as a textbook would: between {low} and {high} sections, each starting where the "
    "subject changes. For each section write one line: the number of the paragraph it starts "
    "at, then \" | \", then a heading of two to five plain words in {language} naming what the "
    "section is about, as a textbook would title it (\"The landscape\", \"The threat\"). The "
    "first section starts at [1]. Nothing else.")

_QUESTION = {
    "kind": {
        "type": "choice",
        "instructions": ("The user asked the assistant to do something with a long text. Does the "
                         "request ask for the whole text rewritten at about its length, the whole "
                         "text rewritten more concisely, or something much shorter than the text?"),
        "criteria": {
            "full": ("rework, rewrite, rephrase, paraphrase or modernise the text, simplify its "
                     "wording or change its register, with nothing said about making it shorter"),
            "condensed": ("rework the whole text more concisely, summarising where possible while "
                          "keeping all its concepts and terms"),
            "shorter": ("a summary, an outline, study notes, key points, or a short version of "
                        "the text"),
        },
    },
    "register": {
        "type": "choice",
        "instructions": "How does the request want the result written?",
        "criteria": {
            "readable": ("nothing said about the form, or clear, simple, schematic, with lists, "
                         "headings or sections"),
            "discursive": ("discursive, flowing, linear prose: connected reasoning with a clear "
                           "beginning and end, without lists"),
        },
    },
}

# The marker server.run_turn appends for an attached image (ui/web/server.py,
# _ATTACHED_RE). Repeated here: importing the web server from a tool would pull
# FastAPI into every surface that registers tools.
_IMAGE_RE = re.compile(r"\n*\[image attached: [^\]\n]+\]")
_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|#^\[\]]+')
_HEADING_RE = re.compile(r"^\s*#{1,6} ")
_LEAD_HEADING_RE = re.compile(r"^\s*#{1,6} [^\n]*\n+")
_OUTLINE_LINE_RE = re.compile(r"\s*\[?(\d+)\]?\s*\|\s*(.+)")
# A text with fewer headings than one in this many words gets the outline's. The
# 6.1k-word chapter and article measured had none, the headed article one in 250.
OUTLINE_WORDS = 800
# An outline heading this close to the one before is dropped: without it, thinking
# off put a heading on every line of Manzoni's dialogue (65 in one chapter).
OUTLINE_GAP = 250
# The outline reads the whole text in one call, so past this it would cross the
# endpoint's 32k-token tier, billed at 3.3x (about 18k words of Italian). Only
# 6.1k-word texts were measured; a longer one is reworked without headings added.
OUTLINE_MAX_WORDS = 12_000


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


def _language(text: str) -> str:
    from silica.kernel.text import language

    return language.display_name(language.detect(text[:4000]))


def outline(text: str, language: str, cancel_token: Any = None) -> str:
    """The text with a heading at each of its main sections, from one reading of
    the whole of it: a block sees a thousand words and cannot tell where a
    chapter's parts begin. Paragraph numbers and not quoted first words, because
    asked to quote eight words Qwen copied whole paragraphs (2026-10-16)."""
    from silica.agent.llm import call_llm
    from silica.config import CONFIG

    paras = re.split(r"\n\s*\n", text.strip())
    n = _words(text)
    prompt = _OUTLINE_PROMPT.format(low=max(2, n // 900), high=max(3, n // 350), language=language)
    numbered = "\n\n".join(f"[{i + 1}] {p}" for i, p in enumerate(paras))
    # Thinking on, unlike the blocks: with it off the reply numbered the sections
    # 1 to 10 instead of naming paragraphs, so every heading landed in the first
    # 700 words of a 6k-word article. On it costs about 0.001 USD and 40 to 60 s
    # on a 6k-word text (2026-10-16). Reopen with a 1024-token reasoning budget
    # if a rework of 6k words passes 120 s.
    reply = call_llm(CONFIG.model, [{"role": "system", "content": prompt},
                                    {"role": "user", "content": numbered}], cancel=cancel_token).text or ""
    named: dict[int, str] = {}
    for line in reply.splitlines():
        m = _OUTLINE_LINE_RE.match(line)
        if m and 0 < int(m.group(1)) <= len(paras):
            named.setdefault(int(m.group(1)) - 1, m.group(2).strip(" *#"))
    out: list[str] = []
    words, last = 0, -OUTLINE_GAP
    for i, para in enumerate(paras):
        if named.get(i) and words - last >= OUTLINE_GAP and not _HEADING_RE.match(para):
            out.append("## " + named[i])
            last = words
        out.append(para)
        words += _words(para)
    return "\n\n".join(out)


def _request() -> str:
    """The user's current message as they wrote it: without the named-notes
    block and the image markers the server appends to it."""
    from silica.agent import mentions
    from silica.agent.read_scope import REQUESTS

    requests = REQUESTS.get()
    return _IMAGE_RE.sub("", mentions.strip(requests[-1])).strip() if requests else ""


def _ask(request: str) -> tuple[str, str]:
    """(kind, register) the request asks for, by one decision call: "full"
    and "readable", the measured defaults, unless Jev is on and sure
    (ADR-0116, amendment 2026-10-23)."""
    from silica.agent.decide import decide
    from silica.config import CONFIG

    if not CONFIG.decision_model or not request:
        return "full", "readable"
    answers = decide({"current_request": request[:400]}, _QUESTION) or {}

    def pick(key: str, default: str) -> str:
        verdict = answers.get(key) or {}
        choice = str(verdict.get("choice") or "")
        sure = (verdict.get("probabilities") or {}).get(choice, 0.0) > SHORTER_ACCEPT
        return choice if choice in _QUESTION[key]["criteria"] and sure else default

    return pick("kind", "full"), pick("register", "readable")


def _fits_chat(text: str, share: float) -> bool:
    """The text and a result of `share` its length fit beside the rest of the
    turn under the line where compaction starts, counted as the pre-call fit
    counts (2 chars a token). Past it the fit stubs the source once the
    result is in the call's arguments: hosted t06, 2026-10-22."""
    from silica.agent.compaction import COMPACT_FRACTION, TEXT_CHARS_PER_TOKEN
    from silica.config import CONFIG

    return len(text) * (1 + share) / TEXT_CHARS_PER_TOKEN <= COMPACT_FRACTION * CONFIG.max_context_tokens


def _prompt(instruction: str, language: str, kind: str = "full", register: str = "readable") -> str:
    return _WORKER_PROMPT.format(
        length=_LENGTH[kind].format(share=f"{round(CONDENSED_SHARE * 100)}%"),
        rules=_RULES[register], language=language, instruction=instruction)


def _rework(block: str, instruction: str, language: str, cancel_token: Any,
            kind: str = "full", register: str = "readable") -> str:
    from silica.agent.llm import call_llm
    from silica.config import CONFIG

    messages = [{"role": "system", "content": _prompt(instruction, language, kind, register)},
                {"role": "user", "content": block}]
    # Thinking off: on two texts it was the most faithful arm (FActScore 0.966
    # and 0.996, against 0.879 and 0.965 with thinking on), with the fewest
    # language errors, at a fifth of the cost and a quarter of the wait.
    out = (call_llm(CONFIG.model, messages, reasoning=False, cancel=cancel_token).text or "").strip()
    if not out:
        raise RuntimeError("the model returned no text")
    # A block starts wherever a thousand words ran out, mostly mid-section, so a
    # heading the worker opens it with would split a section in two; one the
    # source block opens with is the text's own (or the outline's) and stays.
    return out if _HEADING_RE.match(block) else _LEAD_HEADING_RE.sub("", out, count=1)


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
    """Rework a long text in full: rewrite, rephrase, modernise or simplify its wording, or change its register, keeping every passage in order at about its length. The text is a note, or a text of more than about 700 words the user pasted in their message. The tool reads it itself and writes the result to a new note, so call it directly: do not read the note first, and do not copy a pasted text into the call. Use it whenever the user asks to rework, rewrite, rephrase or modernise such a text, also more concisely ("summarising where possible"): when that fits your reply the tool says so and you write it. Not for a summary, an outline or study notes: those are not a rework, do them yourself."""
    from silica.config import CONFIG
    from silica.driver import DRIVER
    from silica.kernel.write import frontmatter
    from silica.tools.notes import silica_write_note

    request = _request()
    if note:
        try:
            src = DRIVER.read_note(note)
        except Exception:
            # The driver raises on a missing note; the model needs the name back,
            # and the backend's message (a vault path) would push it past the
            # projection's 80 chars.
            return {"ok": False, "error": f"No note named '{note}'."}
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
                "error": f"Under {FLOOR_WORDS} words: rewrite it in your reply, all of it, same length."}
    kind, register = _ask(request)
    how = " as connected prose, without lists" if register == "discursive" else ""
    if kind == "shorter":
        return {"ok": False, "error": f"A shorter version was asked for: write it yourself{how}."}
    if kind == "condensed" and _fits_chat(text, CONDENSED_SHARE):
        # "It fits your reply" read as "answer in the chat": a probe case pasted
        # the chapter into its reply and wrote no note (2026-10-23).
        return {"ok": False, "error": f"Short enough for you: condense it into a new note yourself{how}."}
    parts = blocks(text)
    if len(parts) > MAX_BLOCKS:
        return {"ok": False, "blocks": len(parts),
                "error": f"Over {MAX_BLOCKS} blocks: ask the person for one part at a time."}
    language = _language(text)
    if len(re.findall(r"^#{1,6} ", text, re.M)) * OUTLINE_WORDS < words <= OUTLINE_MAX_WORDS:
        try:
            parts = blocks(outline(text, language, cancel_token))
        except Exception as e:
            # Headings are for reading, the rework is the content: a text that
            # comes back complete without them is still what was asked for.
            logger.warning("rework outline failed, no headings added: %s", e)

    # The person's own words, not the chat's `instruction`: the chat paraphrases
    # and adds what nobody asked for ("paragrafi tematici e tabelle quando
    # utile" came back at 0.70 of the length, 2026-10-16), and every measured
    # block ran on the request as written. `instruction` serves a call with no
    # message behind it.
    ask = re.split(r"\n\s*\n", request, maxsplit=1)[0][:300] if request else (instruction or "Rework this text")
    with ThreadPoolExecutor(min(WORKERS, len(parts))) as pool:
        futures = [pool.submit(_rework, p, ask, language, cancel_token, kind, register) for p in parts]
        outs: list[str] = []
        for k, fut in enumerate(futures):
            try:
                outs.append(fut.result())
            except Exception as e:
                # All or nothing: a note missing one block would read as a
                # complete rework with a passage gone, the failure this exists for.
                logger.warning("rework block %d of %d failed: %s", k + 1, len(parts), e)
                return {"ok": False, "error": f"Block {k + 1} of {len(parts)} failed; nothing was written."}

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
    # Every string here stays under the 80 chars the loop's eager projection
    # keeps (compaction.generic_projection): a longer `reply` reached the chat as
    # "reply=<128 chars> ⟨↻ re-call to expand⟩", and the chat read the new note
    # whole and then misspelled its link (2026-10-16). `link` is there to be
    # copied. "Link the new note" once read as "link it from the source": a run
    # patched the person's note with a pointer to the rework.
    return {"ok": True, "link": f"[[{PurePosixPath(written.get('path', path)).stem}]]",
            "words_in": words, "words_out": _words(body), "blocks": len(parts),
            "reply": "Give the person `link` in one line. Do not edit the source note."}
