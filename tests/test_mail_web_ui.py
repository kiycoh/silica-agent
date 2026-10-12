# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0090 "GUI and TUI": what the Mail row's dry run SAYS, from the pure
half of app-mail.js run under node. The DOM half places it and is verified by
opening the app."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static" / "app-mail.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _call(tmp_path, fn: str, *args):
    src = JS.read_text(encoding="utf-8")
    pure = src[src.index("// mail-pure:start"):src.index("// mail-pure:end")]
    script = tmp_path / "m.js"
    script.write_text(pure + f"\nconsole.log(JSON.stringify({fn}(...JSON.parse(process.argv[2]))));\n")
    out = subprocess.run(["node", str(script), json.dumps(list(args))],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _plan(**kw):
    base = {"takeout": False, "in_file": 3180, "took_part": 214, "present": 3, "over_limit": 11,
            "added_by_all": 2966, "threads": [{}] * 200}
    return {**base, **kw}


def test_the_dry_run_line_is_the_one_the_adr_writes(tmp_path):
    assert _call(tmp_path, "mailPlanLine", _plan()) == (
        "214 threads you took part in, 3,180 in the file; the first 200 would be written, "
        "3 already in the vault")


def test_under_the_limit_it_says_how_many_and_counts_one_as_one(tmp_path):
    line = _call(tmp_path, "mailPlanLine", _plan(in_file=2, took_part=1, present=0, over_limit=0,
                                                 threads=[{}]))
    assert line == "1 thread you took part in, 2 in the file; 1 would be written, 0 already in the vault"


def test_a_takeout_file_names_the_sent_label(tmp_path):
    line = _call(tmp_path, "mailPlanLine", _plan(takeout=True))
    assert line.startswith("214 threads you sent in (Gmail's Sent label), 3,180 in the file")


def test_the_all_switch_says_what_it_adds(tmp_path):
    assert _call(tmp_path, "mailAllLabel", _plan()) == \
        "also the 2,966 threads you took no part in"
    assert _call(tmp_path, "mailAllLabel", _plan(added_by_all=0)) == ""


def test_import_waits_for_a_confirmed_address_outside_takeout(tmp_path):
    assert _call(tmp_path, "mailImportBlock", _plan(guessed=True)) == "confirm your address above first"
    assert _call(tmp_path, "mailImportBlock", _plan(guessed=False)) == ""
    assert _call(tmp_path, "mailImportBlock", _plan(guessed=True, takeout=True)) == ""
    assert _call(tmp_path, "mailImportBlock", _plan(threads=[])) == "nothing to write"


def test_the_undo_line_says_what_stayed(tmp_path):
    assert _call(tmp_path, "mailUndoLine", {"removed": 12, "kept": 0, "gone": 0, "errors": []}) \
        == "removed 12 notes"
    assert _call(tmp_path, "mailUndoLine", {"removed": 1, "kept": 1, "gone": 2, "errors": ["a: x"]}) \
        == "removed 1 note · kept 1 note you edited since · 2 notes already gone · 1 failed: a: x"
