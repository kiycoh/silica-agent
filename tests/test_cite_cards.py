# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0117 point 2: which line of a cited note a citation opens at, from the
pure half of app-chat.js run under node. Marking it in the drawer's editor is
verified by opening the app."""
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
    assert [x["text"] for x in lines] == [
        "Si cerca l'iperpiano che massimizza il margine tra le due classi del training set.",
        "Solo i vettori di supporto vicini al margine spostano il confine.",
    ]  # no frontmatter, heading, table, fence, and a course header is not a sentence


def test_each_sentence_carries_the_line_of_the_raw_file(tmp_path):
    # the editor marks this line, so it counts the frontmatter and the fence too
    raw = NOTE.split("\n")
    for x in _eval(tmp_path, f"proseLines({json.dumps(NOTE)})"):
        assert x["text"][:12] in raw[x["line"] - 1].replace("[[Vettori di supporto|", "")


def test_a_comment_over_several_lines_is_not_prose(tmp_path):
    note = "<!--\nQuesta frase sta dentro un commento e non va citata mai.\n-->\nQuesta frase invece e' prosa della nota e si puo' citare.\n"
    got = _eval(tmp_path, f"proseLines({json.dumps(note)})")
    assert got == [{"text": "Questa frase invece e' prosa della nota e si puo' citare.", "line": 4}]


def test_a_callout_body_is_prose_and_its_head_is_not(tmp_path):
    note = "> [!note] Una definizione utile da ricordare per l'esame finale.\n> La probabilita' deve stare tra zero e uno, mentre un modello lineare no.\n"
    got = _eval(tmp_path, f"proseLines({json.dumps(note)})")
    assert got == [{"text": "La probabilita' deve stare tra zero e uno, mentre un modello lineare no.", "line": 2}]


def test_the_citation_opens_at_the_line_sharing_most_with_the_citing_paragraph(tmp_path):
    words = "Secondo le note l'iperpiano massimizza il margine"
    got = _eval(tmp_path, f"bestLine(proseLines({json.dumps(NOTE)}), citeWords({json.dumps(words)}))")
    assert got["text"].startswith("Si cerca l'iperpiano") and got["line"] == 6


def test_an_answer_in_another_language_mostly_marks_nothing(tmp_path):
    # the ceiling the ponytail note names: "margin" is not "margine", so one word in common
    words = "the SVM keeps the margin between the classes of the training set"
    assert _eval(tmp_path, f"bestLine(proseLines({json.dumps(NOTE)}), citeWords({json.dumps(words)}))") is None


def test_one_shared_word_is_not_a_match(tmp_path):
    # "margine" alone: the note opens at its top, no line is marked on a guess
    got = _eval(tmp_path, f"bestLine(proseLines({json.dumps(NOTE)}), citeWords('a different margine'))")
    assert got is None


def test_common_words_never_count(tmp_path):
    assert _eval(tmp_path, "[...citeWords('That with della nella SVM margin kernel')]") == ["margin", "kernel"]
