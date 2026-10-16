# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0117 point 2: which line of a cited note its card quotes, from the pure
half of app-chat.js run under node. Placing the cards in the margin is layout
and is verified by opening the app."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static" / "app-chat.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _eval(tmp_path, expr: str):
    src = JS.read_text(encoding="utf-8")
    pure = src[src.index("// cite-pure:start"):src.index("// cite-pure:end")]
    script = tmp_path / "c.js"
    script.write_text(pure + f"\nconsole.log(JSON.stringify({expr}));\n", encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


NOTE = """---
tags: [statistica]
---
# Support Vector Machines

Si cerca l'iperpiano che massimizza il margine tra le due classi del training set.
| a | b |
```python
svm.fit(X, y)
```
- Solo i [[Vettori di supporto|vettori di supporto]] vicini al margine spostano il confine.
Machine Learning, 9 CFU.
"""


def test_a_note_reads_as_its_prose_sentences(tmp_path):
    lines = _eval(tmp_path, f"proseLines({json.dumps(NOTE)})")
    assert lines == [
        "Si cerca l'iperpiano che massimizza il margine tra le due classi del training set.",
        "Solo i vettori di supporto vicini al margine spostano il confine.",
    ]  # no frontmatter, heading, table, fence, and a course header is not a sentence


def test_the_card_quotes_the_line_sharing_most_with_the_citing_paragraph(tmp_path):
    words = "Secondo le note l'iperpiano massimizza il margine"
    got = _eval(tmp_path, f"bestLine(proseLines({json.dumps(NOTE)}), citeWords({json.dumps(words)}))")
    assert got.startswith("Si cerca l'iperpiano")


def test_an_answer_in_another_language_mostly_quotes_nothing(tmp_path):
    # the ceiling the ponytail note names: "margin" is not "margine", so one word in common
    words = "the SVM keeps the margin between the classes of the training set"
    assert _eval(tmp_path, f"bestLine(proseLines({json.dumps(NOTE)}), citeWords({json.dumps(words)}))") == ""


def test_one_shared_word_is_not_a_quote(tmp_path):
    # "margine" alone: the card shows title and folder, never a line made up to fill it
    got = _eval(tmp_path, f"bestLine(proseLines({json.dumps(NOTE)}), citeWords('a different margine'))")
    assert got == ""


def test_common_words_never_count(tmp_path):
    assert _eval(tmp_path, "[...citeWords('That with della nella SVM margin kernel')]") == ["margin", "kernel"]
