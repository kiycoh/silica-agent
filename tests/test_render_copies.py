"""OFM is read in three places, and they stay three.

`silica.kernel.text.render` is the renderer. `mdLite` in app-chat.js draws an answer while it
streams, and the editor's Lezer grammar in app-editor.js draws a note as it is typed: both run in
a browser, on every keystroke or delta, where the Python cannot. So the mirrors are held by what
they print and not by a comment saying they mirror it. What only one of them reads is not here.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from silica.kernel.base.syntax import MARK_OR_TAG_RE
from silica.ui.web import render
from tests.link_cases import MATH_CASES, NOTE_CASES, TAG_CASES, TAG_DRIFT
from tests.webassets import WEB, app_js, core_js

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's scripts")


def _node(tmp_path, source: str, *args: object) -> list:
    script = tmp_path / "copies.js"
    script.write_text(source, encoding="utf-8")
    out = subprocess.run(["node", str(script), *[json.dumps(a) for a in args]], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _md_lite(tmp_path, sources: list[str]) -> list[str]:
    block = re.search(r"^function mdLite\(src\) \{.*?^\}", app_js(), re.S | re.M).group(0)
    return _node(tmp_path, core_js() + block + "\nconsole.log(JSON.stringify(JSON.parse(process.argv[2]).map(mdLite)));\n", sources)


def _editor(name: str) -> str:
    """One of the editor's patterns, as its own source spells it."""
    text = (WEB / "app-editor.js").read_text(encoding="utf-8")
    return re.search(rf"const {name} = (/.*/u?);", text).group(1)


@pytest.mark.parametrize(("md", "present"), NOTE_CASES)
def test_the_renderer_prints_what_the_streaming_render_mirrors(md, present):
    html = render.linkify(md, resolve=lambda ref: ref)
    for frag in present:
        assert frag in html, html


@needs_node
def test_the_streaming_render_prints_the_same(tmp_path):
    """A tool-split turn keeps its live segments until reload, and the page finds a note link by
    its attributes, so these are the app's own contract in two languages."""
    for (md, present), html in zip(NOTE_CASES, _md_lite(tmp_path, [c[0] for c in NOTE_CASES])):
        for frag in present:
            assert frag in html, (md, html)


@needs_node
def test_the_anchor_mdlite_copies_is_the_one_the_app_binds(tmp_path):
    """The attribute order is what the two share by hand since neither can call the other."""
    assert _md_lite(tmp_path, ["[[Folder/Note|x]]"])[0] == f'<p>{render.anchor("Folder/Note", "x")}</p>'


@pytest.mark.parametrize(("text", "math"), MATH_CASES)
def test_the_renderer_reads_inline_math_as_the_cases_say(text, math):
    assert ("<math" in render.linkify(text)) is math


@needs_node
def test_the_editor_reads_inline_math_as_the_renderer_does(tmp_path):
    found = _node(tmp_path, f"const MATH = {_editor('MATH')};\n"
                            "console.log(JSON.stringify(JSON.parse(process.argv[2]).map((t) => MATH.test(t))));\n",
                  [c[0] for c in MATH_CASES])
    assert found == [c[1] for c in MATH_CASES]


@pytest.mark.parametrize(("text", "tag"), TAG_CASES)
def test_the_renderer_reads_a_tag_as_the_cases_say(text, tag):
    m = MARK_OR_TAG_RE.search(text)
    assert (m.group(2) if m else None) == tag


@needs_node
def test_the_editor_reads_a_tag_as_the_renderer_does_but_for_the_stated_drift(tmp_path):
    js = (f"const TAG = {_editor('TAG')};\nconsole.log(JSON.stringify(JSON.parse(process.argv[2])"
          ".map((t) => { const m = TAG.exec(t); return m ? m[0].slice(1) : null; })));\n")
    assert _node(tmp_path, js, [c[0] for c in TAG_CASES]) == [c[1] for c in TAG_CASES]
    for text, seen in zip(TAG_DRIFT, _node(tmp_path, js, TAG_DRIFT)):
        assert seen == text[1:] and MARK_OR_TAG_RE.search(text) is None, text


@needs_node
def test_the_editor_and_the_renderer_take_one_link_shape(tmp_path):
    from silica.kernel.base.syntax import WIKILINK_WHOLE_RE

    cases = ["[[a]]", "[[a|b]]", "![[a.png|3]]", "[[a#h|b]]", "[[a]", "[[a[b]]", "[[]]"]
    found = _node(tmp_path, f"const WIKI = {_editor('WIKI')};\n"
                            "console.log(JSON.stringify(JSON.parse(process.argv[2]).map((t) => WIKI.test(t))));\n", cases)
    assert found == [bool(WIKILINK_WHOLE_RE.match(c)) for c in cases]


@needs_node
def test_a_read_chip_resolves_as_the_server_but_keeps_a_shared_name_as_written(tmp_path, tmp_vault):
    """resolveRef draws the read chips, from the note index the page holds, and cannot ask. On a
    name two notes share the server's resolver takes the first by path, an answer's says "" and
    the chip keeps the argument: it says what the tool was asked, which is the one honest thing
    a chip can say about a note it cannot tell apart (ADR-0114)."""
    from silica.kernel.recall.resolve import note_resolver

    paths = ["Corso A/Lezione 16.md", "Corso B/Lezione 16.md", "Fisica/Moto.md"]
    for p in paths:
        tmp_vault.note(p, "text\n")
    resolve = note_resolver()
    block = re.search(r"^function resolveRef\(ref\) \{.*?^\}", app_js(), re.S | re.M).group(0)
    index = [{"path": p, "lp": p.lower()} for p in paths]
    js = (core_js() + f"\nconst noteIdx = {json.dumps(index)};\n" + block
          + "\nconsole.log(JSON.stringify(JSON.parse(process.argv[2]).map(resolveRef)));\n")
    agreed = ["Fisica/Moto.md", "Fisica/Moto", "fisica/moto", "Moto", "Corso A/Lezione 16"]
    assert _node(tmp_path, js, agreed) == [resolve(r) for r in agreed]
    assert _node(tmp_path, js, ["Lezione 16"]) == ["Lezione 16"]
