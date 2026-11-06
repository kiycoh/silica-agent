# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The turn a drop of files becomes: what is asked of the agent, and the manifest of what
was staged. The requests a read file offers live here with the words that compose them."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from silica.config import CONFIG

logger = logging.getLogger(__name__)


NUCLEATE_ASK = ("Nucleate the attached file(s) into an appropriate folder; "
                "ask me if the target is unclear.")

# A drop with no words used to default to NUCLEATE_ASK. On t03 (2026-10-14) a
# dropped 317-page PDF started a 22-file run nobody asked for, the most
# expensive thing the agent can do. Without a request the turn now describes
# what came in and asks; "Nucleate it" on a finished job still says
# NUCLEATE_ASK, because there the student chose it.
DROP_ASK = ("I attached the file(s) below without saying what I want. Do not nucleate, convert, "
            "move or write anything yet. Say in one or two sentences what each file is, from its "
            "outline below; read the file only when the outline is not enough. Then ask what I "
            "want to do with it, offering three or four concrete options that fit this material "
            "(for example: nucleate it into notes under a folder you name, summarize it, quiz me "
            "on it, a guided /memorize path).")
_DROP_OUTLINE_HEADINGS = 8

# The two turns a read file offers besides its question (ADR-0121 point 6),
# composed like "Nucleate it" and drawn as the label the bubble shows.
SUMMARIZE_ASK = ("Summarize the attached file(s): the main points and findings, "
                 "in a few short sections, from the file's own text.")
QUIZ_ASK = ("Quiz me on the attached file(s): one question at a time, wait for my answer, "
            "then say whether it is right and why, from the file's text.")

# A turn that brings attached files in is not offered the injector (ADR-0121
# point 3): the 9B called it for a question about an 80-word file, against the
# DROP_ASK sentence a drop with no words already carries. "Nucleate it" keeps it.
WITHHELD_WITH_FILES = ("silica_run_injector",)

# How much of the attached files' text rides the turn (ADR-0121 point 2):
# about 6,000 tokens, a fifth of the hosted 32k window, which a verbale or a
# short paper fits and a 317-page textbook (t03) does not.
_INLINE_CHARS = 24_000

# Where the words the student typed end in a composed turn: what follows is
# the manifest the model reads and the bubble never shows (ADR-0121 point 4).
MANIFEST_MARK = "\n\n---\nAttached files"


def _attached_text(rel: str) -> str | None:

    try:
        return (Path(CONFIG.vault_path) / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        # The manifest still names the file; without its text or outline the
        # model reads it, which is the cost this saves and not a wrong answer.
        return None


def _drop_outline(rel: str, md: str | None = None) -> str:
    """"~N words; sections: a; b; ..." for a staged note, so DROP_ASK is
    answered from the manifest and not by reading a whole textbook into the
    context (the t03 PDF was 317 pages)."""
    from silica.kernel.outline import source_headings

    md = _attached_text(rel) if md is None else md
    if md is None:
        return ""
    heads = source_headings(md)
    more = f" (+{len(heads) - _DROP_OUTLINE_HEADINGS} more)" if len(heads) > _DROP_OUTLINE_HEADINGS else ""
    shown = "; ".join(heads[:_DROP_OUTLINE_HEADINGS])
    return f"~{len(md.split())} words" + (f"; sections: {shown}{more}" if shown else "")


def compose_nucleate_turn(text: str, ready: list[str], stubs: list[str],
                           mail: Sequence[str] = (),
                           skipped: Sequence[tuple[str, str]] = (),
                           converting: Sequence[str] = (),
                           earlier: str = "", inline: bool = True) -> str:
    """The agent turn for a batch of attached files: the user's instruction plus
    a factual manifest of what got staged. Empty instruction: DROP_ASK.

    Each ready file's text goes in whole while the total fits _INLINE_CHARS
    (ADR-0121 point 2), so a question about a short file needs no tool call;
    one that does not fit is named with its outline and read first. `earlier`
    is the question a drop asked when its file joins a later message (point
    4); `inline` is off for "Nucleate it", whose injector reads the files."""
    asked = text.strip()
    lines: list[str] = []
    if asked and asked not in (NUCLEATE_ASK, SUMMARIZE_ASK, QUIZ_ASK):
        # The prompt's rule is the language of the person's latest message,
        # and this message is mostly attached text: an Italian question on an
        # English paper was answered in English (ADR-0121, the app run).
        lines.append("Everything below this line is attached material, not the user's words: "
                     "reply in the language of their words above it, whatever the files' language.")
    if earlier.strip():
        lines.append(f'With these files the user asked earlier: "{earlier.strip()}"')
    if ready:
        # Not "ready to nucleate": with those words a 9B nucleated a file it
        # was asked a question about (ADR-0121 Context).
        lines.append("Attached to this message by the user. The message is about these files: answer "
                     "from them, and search the rest of the vault only when the message asks for that:"
                     if inline else "Copied into the Inbox, to nucleate:")
        budget = _INLINE_CHARS if inline else 0
        for p in ready:
            md = _attached_text(p)
            if md is not None and len(md) <= budget:
                budget -= len(md)
                lines.append(f"- {p} (~{len(md.split())} words), its text:\n<file path=\"{p}\">\n"
                             f"{md.strip()}\n</file>")
                continue
            outline = _drop_outline(p, md) if md is not None else ""
            # The whole path in the call: a chat's file is in no index, so a
            # read by its name alone finds nothing (the app run, ADR-0121).
            lines.append(f"- {p}" + (f" ({outline})" if outline else "")
                         + (f': too long to include here, read it with silica_read(path="{p}") before answering'
                            if inline else ""))
    if converting:
        # Named so the answer does not read as if the files were lost: each is
        # still being read and joins the conversation when it is ready
        # (ADR-0121 points 1 and 4). Each by its path, with what can be read of
        # it now (ADR-0099 amendment 2026-10-20). The button is not named: told
        # "offered with Nucleate it", the model told the student that button converts.
        lines.append("Still being read. Tell the user each one joins this chat once it is ready, "
                     "and can then be summarized or asked about:")
        lines += [_converting_line(p) for p in converting]
    if ready and inline:
        # Said again last, where a small model weighs it: with the sentence
        # only at the head, the 9B still ran ten vault searches for a question
        # its attached text answered (ADR-0121, the app run).
        lines.append("Answer from the attached text above. Do not search the vault unless the "
                     "user's words ask about their own notes.")
    if stubs:
        lines.append("Code skeleton stubs already staged in the vault:")
        lines += [f"- {p}" for p in stubs]
    if mail:
        lines.append("Not staged, mail files: mail is imported through Settings › Connectors › Mail "
                     "or /mail import <file>, which show a dry run first and keep the threads the "
                     "user took part in. Tell the user so:")
        lines += [f"- {n}" for n in mail]
    if skipped:
        # Named in the turn and not only in the log: a refused or failed file
        # otherwise vanished, and the user saw an answer about the others.
        lines.append("Not staged, or staged in part, each with the reason. Tell the user which files and why:")
        lines += [f"- {n}: {why}" for n, why in skipped]
    manifest = "\n".join(lines) if lines else "(no files could be staged)"
    return f"{asked or DROP_ASK}{MANIFEST_MARK}:\n{manifest}"


def _converting_line(rel: str) -> str:
    """One file of a drop still converting, by its vault path, which a read
    can take; the name alone left the model nothing to open.

    A PDF's text layer is readable now (ADR-0099 amendment 2026-10-20):
    silica_read opens it through pdf_extract, in the limited child on the
    server and without its hidden text (ADR-0098 amendment 2026-10-20), in
    0.24-0.42 s for 36 pages, while the conversion waits on the chat model
    reading page images (ADR-0087: 23.6 s for one scanned page). "Not
    readable in this turn" was true of the note and false of the file.

    This read is the one silica_read makes (`vision=0`, one cache key), so it
    moves that cost before the turn rather than adding one; it names the
    pages the layer lacks, so an answer that needs them says so."""
    from silica.sources import pdf_extract

    later = f"- {rel}: not readable until it is converted; tell the user so"
    data = _text_layer(rel)
    if data is None:
        return later
    n, no_text, math = data["n_pages"], data["no_text"], data["math"]
    look = f'silica_look(image="{rel}", question=...), which finds the pages by its words, or with page=N'
    if len(no_text) == n:
        return (f"- {rel} ({n} pages): it has no selectable text, so it is not readable as text "
                f"until it is converted; an answer from one page can look at it, {look}")
    # No `section="p. N"` here: once the note exists the engine serves it for
    # this path, and a page section is then not_found; the path alone reads
    # the layer now and the note later.
    line = f'- {rel} ({n} pages): its text layer is readable now, in this message: silica_read(path="{rel}")'
    gaps = ([f"may garble the formulas on {pdf_extract.named_pages([i + 1 for i in math])}"] if math else []) \
        + ([f"has nothing on {pdf_extract.named_pages([i + 1 for i in no_text])}"] if no_text else [])
    if gaps:
        # The page and not "say so" (ADR-0099 amendment 2026-10-21): on 40
        # questions about the owner's lectures the text layer answered 0.56
        # and the layer with the formula pages seen 0.91 to 0.93.
        line += f". That layer {' and '.join(gaps)}: when the answer depends on one of them, look at it, {look}"
    return line


def _text_layer(rel: str) -> dict | None:
    """A dropped PDF's text layer, read as silica_read reads it; None for any
    other file and for a PDF the reader refuses."""
    from silica.sources import pdf_extract

    if not rel.lower().endswith(".pdf"):
        return None
    try:
        return pdf_extract.read(Path(CONFIG.vault_path or ".") / rel, vision=0)
    except Exception:
        # Tolerated: a PDF the reader refuses (a password, a malformed file, a
        # child that died) is the conversion's to report, and how it ended
        # reaches the next turn (jobs.settled). Until then it is a file still
        # converting, which is all the drop's line said before.
        logger.warning("drop: the text layer of %s was not read", rel, exc_info=True)
        return None


def readable_now(rel: str) -> bool:
    """A PDF with text on some page: its question need not wait for it."""
    data = _text_layer(rel)
    return data is not None and len(data["no_text"]) < data["n_pages"]


def file_entry(source: str, notes: Sequence[str] | None = None, job: str = "") -> dict:
    """One file as a user message carries it (`silica_files`, ADR-0121 point
    4): its name, the file the student gave, the notes the model reads, and
    the job converting it when the notes are not written yet."""
    out: dict[str, Any] = {"name": Path(source).name, "source": source,
                           "notes": list(notes) if notes is not None else ([] if job else [source])}
    if job:
        out["job"] = job
    return out
