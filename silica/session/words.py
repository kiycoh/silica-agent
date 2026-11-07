# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""How a typed command's words are read. `shlex` and argparse each get one thing wrong
for prose and for a REPL, so the rules live here and every surface reads a command the same way."""
from __future__ import annotations

import shlex


def direct_args(raw: str) -> list[str]:
    """A direct command's words, a double-quoted run being one word, so
    `/check "Drafts/Opinion on limitation"` names the note and not a note
    called `"Drafts/Opinion`. Not shlex's default: an apostrophe is a letter in
    prose (`/find l'entropia`), `#` starts a tag and not a comment, and a
    backslash is a Windows path's. An unbalanced quote leaves the words as
    typed, as every direct command read them before quotes were honoured."""
    lex = shlex.shlex(raw, posix=True)
    lex.whitespace_split, lex.quotes, lex.escape, lex.commenters = True, '"', "", ""
    try:
        return list(lex)
    except ValueError:
        return raw.split()


def unquoted(text: str) -> str:
    """The path a command takes whole (`/undo`, `/delete`, `/trash restore`)
    without one pair of quotes around it: those read the raw line, since a
    note's name has spaces, and a quoted name was looked up with its quotes."""
    text = text.strip()
    return text[1:-1].strip() if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'" else text


def positional(args: list[str]) -> list[str]:
    """The tokens that are not flags — anything not starting with `-`.

    Slash commands take their subject positionally and their options as
    `--key=value`, so this and `str_flag` are the whole parser. argparse would
    own sys.argv and exit the REPL on a typo; these two never do.
    """
    return [a for a in args if not a.startswith("-")]


def str_flag(args: list[str], flag: str, default: str = "") -> str:
    """`--flag=value` out of args, or `default` when it is absent.

    `flag` is given without the `=` ("--target"); an empty `--target=` yields ""
    and is therefore indistinguishable from absent, which every caller wants.
    """
    prefix = flag + "="
    return next((a[len(prefix):] for a in args if a.startswith(prefix)), default)


def int_flag(args: list[str], flag: str, default: int) -> int:
    """`--flag=N` out of args; keeps the default when absent or not a number."""
    raw = next((a[len(flag):] for a in args if a.startswith(flag)), None)
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def remember_text(raw_input: str) -> str:
    """What follows `/remember`, exactly as typed: the entry is the user's own
    words, so neither case nor inner spacing is touched."""
    parts = raw_input.strip().split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""
