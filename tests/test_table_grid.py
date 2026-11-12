# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0160: the editor's table grid, run under node on the shipped bundle. A cell is
split where the server splits it, written back with its pipes escaped, and a column
change writes the table compactly; where the split disagrees the grid is not built."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from silica.kernel.text.render import render
from tests.webassets import WEB

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the editor bundle")

PROBE = """
const vm = require("vm"), fs = require("fs");
globalThis.window = globalThis;
vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
vm.runInThisContext(fs.readFileSync(process.argv[3], "utf8"));
const L = window.LiveEditor, out = [];
for (const [fn, arg] of JSON.parse(process.argv[4])) {
  if (fn === "cells") out.push(L.tableCells(arg).map(([a, b]) => arg.slice(a, b)));
  else if (fn === "model") out.push(L.tableModel(arg));
  else if (fn === "text") out.push(L.tableText(L.tableModel(arg)));
  else out.push(L[fn](arg));
}
console.log(JSON.stringify(out));
"""


def _run(tmp_path, calls):
    probe = tmp_path / "probe.js"
    probe.write_text(PROBE)
    run = subprocess.run(["node", str(probe), str(WEB / "codemirror.min.js"), str(WEB / "app-editor.js"), json.dumps(calls)],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-2000:]
    return json.loads(run.stdout)


# Rows whose pipes the server keeps in a cell, and one it splits (a code span, as GFM does).
ROWS = [
    "| a | b | c |",
    "a | b | c",
    "| [[Note|alias]] | $|x|$ | a \\| b |",
    "| **x** |  | [y](https://e.org/?a|b) |",
    "| `a|b` | c | d |",
    "| Coffee | $5 | x |",  # a price: a `$` that never closes is text
    "| [[x | y | z |",  # a link that never closes is text
    "| $5 | $7 | x |",  # two prices: `$5 | $` closes on a space, so dollarmath reads no math
    "| $5 | $|x|$ | y |",  # a price beside a formula
]


def _server_cells(row: str) -> int:
    """The cells markdown-it gives the row when the head has as many as the grid saw: a
    row it splits wider than its head is cut to the head, so the head is made wide."""
    head = "|" + " h |" * 8 + "\n|" + "---|" * 8 + "\n"
    html = render(head + row + "\n", link=lambda p, d: d, asset=lambda t: t, resolve=lambda r: r)
    cells = [c.split(">", 1)[1].split("</td>")[0].strip() for c in html[html.index("<tbody>"):].split("<td")[1:]]
    while cells and not cells[-1]:  # the padding up to the head's width (no row here ends empty)
        cells.pop()
    return len(cells)


def test_the_split_agrees_with_the_servers_render(tmp_path):
    got = _run(tmp_path, [["cells", r] for r in ROWS])
    assert [len(c) for c in got] == [_server_cells(r) for r in ROWS] == [3, 3, 3, 4, 4, 3, 3, 3, 3]
    assert got[2] == [" [[Note|alias]] ", " $|x|$ ", " a \\| b "]
    # the text, not only the count: a cell taken one pipe too far writes over its neighbour
    assert got[5] == [" Coffee ", " $5 ", " x "] and got[6] == [" [[x ", " y ", " z "]
    assert got[7] == [" $5 ", " $7 ", " x "] and got[8] == [" $5 ", " $|x|$ ", " y "]
    # a row the server splits wider than its head makes the table no grid: drawn, and edited as text
    head = "| h1 | h2 | h3 |\n|---|---|---|\n"
    assert _run(tmp_path, [["model", head + ROWS[4]], ["model", head + ROWS[2]]])[0] is None


def test_a_cell_writes_its_pipes_escaped_and_shows_them_bare(tmp_path):
    shown, raw, kept = _run(tmp_path, [["cellShown", "a \\| b $|x|$"], ["cellRaw", " a | b $|x|$ \\| "],
                                       ["cellRaw", "[[A|b]]"]])
    assert shown == "a | b $|x|$"
    assert raw == "a \\| b $|x|$ \\|"  # a pipe in a formula is the formula's
    assert kept == "[[A\\|b]]"  # Obsidian's spelling of an alias in a table
    # `$5 | $7` typed in one cell is two prices and a pipe, not a formula: a bare pipe
    # would split the cell on disk and leave the table ragged
    assert _run(tmp_path, [["cellRaw", "$5 | $7"]]) == ["$5 \\| $7"]


def test_a_row_of_prices_reads_as_cells_of_text():
    """`| $5 | $7 |` drew one cell holding `$5 \\vert{} $7` and an empty one, in the
    reader too (2026-11-12): the bars in a formula were escaped where dollarmath reads
    no formula, `$5 | $` closing on a space."""
    html = render("| Item | S | L |\n|---|---|---|\n| Coffee | $5 | $7 |\n", link=lambda p, d: d, asset=lambda t: t)
    assert "<td>$5</td>\n<td>$7</td>" in html and "vert" not in html


def test_a_column_change_writes_the_table_compactly_with_its_alignments(tmp_path):
    table = "|   Name    | Formula |  N |\n|-----------|:-------:|---:|\n| **water** | $x$     |  1 |"
    model, text = _run(tmp_path, [["model", table], ["text", table]])
    assert model["aligns"] == ["", "center", "right"] and model["body"] == [["**water**", "$x$", "1"]]
    assert text == "| Name | Formula | N |\n| --- | :---: | ---: |\n| **water** | $x$ | 1 |"


def test_a_ragged_table_is_never_a_grid(tmp_path):
    assert _run(tmp_path, [["model", "| a | b |\n|---|---|\n| 1 | 2 | 3 |"]]) == [None]
