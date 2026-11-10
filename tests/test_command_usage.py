"""A command's usage is its row's: what /help and the GUI picker show is the
text its handler says when a line is wrong, and every flag the handler reads
is in it (entry-sources re-audit, 2026-11-10, Worth fixing 2: three rows had
drifted from their handlers, /nucleate by three flags)."""
from __future__ import annotations

import ast
import importlib
import inspect
import re

from silica.command_table import COMMANDS

_FLAG = re.compile(r"--[a-z][a-z-]*")

# Read by a handler and shown by no row, each on purpose.
_UNSHOWN = {
    # The older spelling of the folder these commands take as a word.
    ("/embed", "--folder"), ("/cooccur", "--folder"), ("/lexical", "--folder"), ("/report", "--folder"),
    # The older spellings of --embeddings and --cooccurrence.
    ("/report", "--with-embeddings"), ("/report", "--with-cooccurrence"),
    # /convert's words are read as /nucleate's are (commands.ingress.read_words,
    # one read for both and for the browser's jobs), and it uses the files and
    # --target alone.
    ("/convert", "--hub"), ("/convert", "--profile"), ("/convert", "--seen"), ("/convert", "--no-keep-sources"),
}


def _flags_read(fn) -> set[str]:
    """Every `--flag` literal in `fn` and in the functions of its module it calls
    by name: `str_flag(args, "--x")`, `"--x" in args`, `p == "--x"` alike."""
    module = inspect.getmodule(fn)
    seen, todo, found = set(), [fn], set()
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        tree = ast.parse(inspect.cleandoc("\n" + inspect.getsource(f)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if m := _FLAG.fullmatch(node.value.rstrip("=")):
                    found.add(m.group(0))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called = getattr(module, node.func.id, None)
                if inspect.isfunction(called) and inspect.getmodule(called) is module:
                    todo.append(called)
    return found


def _handled():
    for row in COMMANDS:
        if row.handler:
            module, name = row.handler.split(":")
            yield row, getattr(importlib.import_module(module), name)


def test_every_flag_a_handler_reads_is_in_its_rows_usage():
    missing = []
    for row, fn in _handled():
        shown = " ".join((row.usage, *row.examples))
        missing += [(row.name, f) for f in sorted(_flags_read(fn))
                    if f not in _FLAG.findall(shown) and (row.name, f) not in _UNSHOWN]
    assert missing == []


def test_a_form_of_a_command_is_read_off_its_usage():
    rows = {c.name: c for c in COMMANDS}
    assert rows["/memorize"].form("--teach") == "--teach <note>"
    assert rows["/memorize"].form("--plan") == "--plan <name> --scope=<folder|note,note|topic:term> [--steps=<note>]"
    assert rows["/settings"].form("") == "<key> <value|none>"
    assert rows["/map"].synopsis == "/map <note> [--force]" and rows["/memory"].synopsis == "/memory"
