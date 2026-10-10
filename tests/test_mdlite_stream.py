# tests/test_mdlite_stream.py
"""mdLite() must terminate on every prefix of a streamed answer.

A table header arrives one delta before its delimiter row, and mdLite() re-parses
the whole segment on each delta. A half-arrived block used to consume no line and
spin the parser forever, until the tab threw RangeError and the SSE reader died
with it, truncating the answer.
"""
import json
import re
import shutil
import subprocess

import pytest

from tests.link_cases import URL_CASES

from tests.webassets import app_js

STREAMED = (
    "### Panoramica\n\n"
    "| Settimana | Date | Cosa studi |\n"
    "|:---------:|:----:|-----------|\n"
    "| **1** | 3/8 | Fondamenti |\n\n"
    "- una lista\n"
    "```\nfence\n```\n"
    # A URL arrives one character at a time too, and trimUrl() peels trailing
    # punctuation in a loop — every prefix of it has to terminate as well.
    "fonte https://en.wikipedia.org/wiki/A_(b), e [x](https://e.com).\n"
    "chiusura.\n"
)


def _md_lite_source() -> str:
    m = re.search(r"^function mdLite\(src\) \{.*?^\}", app_js(), re.S | re.M)
    assert m, "mdLite() not found in app.js"
    return m.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run app.js")
def test_every_streaming_prefix_terminates(tmp_path):
    script = tmp_path / "sweep.js"
    script.write_text(
        _md_lite_source()
        + "\nconst src = JSON.parse(process.argv[2]);\n"
        + "for (let n = 0; n <= src.length; n++) mdLite(src.slice(0, n));\n"
        + "console.log(mdLite(src).includes('<table>') ? 'TABLE' : 'NO-TABLE');\n"
    )
    out = subprocess.run(
        ["node", str(script), json.dumps(STREAMED)],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "TABLE"  # the finished table still renders as one


def _render(tmp_path, sources: list[str]) -> list[str]:
    """mdLite() over each source, under node."""
    script = tmp_path / "render.js"
    script.write_text(
        _md_lite_source()
        + "\nfor (const s of JSON.parse(process.argv[2])) console.log(JSON.stringify(mdLite(s)));\n"
    )
    out = subprocess.run(
        ["node", str(script), json.dumps(sources)],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return [json.loads(line) for line in out.stdout.splitlines()]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run app.js")
def test_url_contract_matches_the_server_render(tmp_path):
    """The same corpus test_gui_web.py runs against `_linkify`. A tool-split turn
    keeps its live segments until reload, so what mdLite does to a URL is what the
    user sees — the two renders answer to one contract."""
    htmls = _render(tmp_path, [md for md, _p, _a in URL_CASES])
    for (md, present, absent), html in zip(URL_CASES, htmls):
        for frag in present:
            assert frag in html, (md, html)
        for frag in absent:
            assert frag not in html, (md, html)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run app.js")
def test_unsafe_scheme_never_becomes_a_live_href(tmp_path):
    """mdLite builds anchors by hand and gets none of markdown-it's validateLink,
    so a model-authored `javascript:` used to land as a live anchor in the app's
    own origin. Whitelist: the text stays, the anchor does not."""
    cases = [
        "[x](javascript:alert%281%29)",
        "[x](JaVaScRiPt:alert%281%29)",
        "[x](data:text/html,<script>1</script>)",
        "[x](vbscript:msgbox)",
    ]
    for html in _render(tmp_path, cases):
        assert "<a href" not in html, html
        assert ">x</a>" not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run app.js")
def test_markdown_link_is_not_double_wrapped_by_the_url_pass(tmp_path):
    """Both link forms share one pass: a bare-URL match running before the
    markdown form eats the target inside `](…)`, running after it re-matches the
    URL already in href="…" and nests the anchors."""
    (html,) = _render(tmp_path, ["vedi [testo](https://e.com/a) qui"])
    assert html.count("<a ") == 1, html
    assert '<a href="https://e.com/a">testo</a>' in html


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run app.js")
def test_only_a_closed_mermaid_fence_becomes_a_diagram(tmp_path):
    """ADR-0082: a tool-split turn keeps mdLite's render, so a closed mermaid
    fence has to come out as the server's <pre class="mermaid"> for
    drawDiagrams() to run; a fence still streaming stays code, and so does any
    other language."""
    closed, open_, other = _render(tmp_path, [
        "```mermaid\nflowchart LR\n  A -->|causes| B\n```\n",
        "```mermaid\nflowchart LR\n  A -->",
        "```python\nx = 1\n```\n",
    ])
    assert closed.startswith('<pre class="mermaid">') and "A --&gt;|causes| B" in closed
    assert "<code>" in open_ and "mermaid" not in open_
    assert '<pre><code>x = 1</code></pre>' in other


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run app.js")
def test_a_figure_embed_is_the_picture_and_a_note_embed_is_not(tmp_path):
    """The same <img> the server's _embed_img emits, width alias included; a
    note transclusion keeps its old link render."""
    img, sized, note = _render(tmp_path, [
        "vedi la curva\n\n![[Pasted image 2025.png]]",
        "![[fig/a&b.jpg|500]]",
        "![[Una nota]]",
    ])
    assert '<img src="/asset?path=Pasted%20image%202025.png" alt="Pasted image 2025">' in img
    assert '<img src="/asset?path=fig%2Fa%26b.jpg" alt="a&b" width="500">' in sized
    assert "<img" not in note and 'class="note-link"' in note


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run app.js")
def test_a_link_never_reaches_inside_a_tag_wiki_built(tmp_path):
    """A `[` in an embed's alt and a later `](…)` on the line used to be one
    markdown link: its href closed the alt early and the model's target became
    attributes of the <img>, an onerror that ran on paint (review of ADR-0082)."""
    img, note, held = _render(tmp_path, [
        "See ![[fig.png|a[b]] and [x](/onerror=onerror=alert;throw/**/1) here",
        "See [[Note|a[b]] and [x](/onerror=alert) here",
        "`[[Una nota]]` e **[[Altra]]**",
    ])
    assert img == ('<p>See <img src="/asset?path=fig.png" alt="a[b"> and '
                   '<a href="/onerror=onerror=alert;throw/**/1">x</a> here</p>'), img
    assert note == ('<p>See <a class="note-link" data-path="Note">a[b</a> and '
                    '<a href="/onerror=alert">x</a> here</p>'), note
    # a parked tag inside a parked code span comes back whole, no placeholder left
    assert "\u0000" not in held and '<code><a class="note-link" data-path="Una nota">' in held, held


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run app.js")
def test_quote_labels_escapes_what_the_9b_wrote_bare(tmp_path):
    """ADR-0082: the /diagram blocks that failed mermaid's parser held ( [ { |
    in bare labels. Quoting keeps the shape bracket; a mindmap line of text
    becomes one quoted node; the root and a clean label are left alone."""
    m = re.search(r"^function quoteLabels\(src\) \{.*?^\}", app_js(), re.S | re.M)
    assert m, "quoteLabels() not found"
    script = tmp_path / "q.js"
    script.write_text(m.group(0) + "\nfor (const s of JSON.parse(process.argv[2])) console.log(JSON.stringify(quoteLabels(s)));\n")
    cases = [
        "flowchart TD\n  A[Funzione f continua su [a,b]] --> B{Valore f(a)?}\n  B -->|f(x)=0| C[Fine]",
        "mindmap\n  root((Java))\n    Object\n      toString()\n      ::icon(fa fa-book)",
    ]
    out = subprocess.run(["node", str(script), json.dumps(cases)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    flow, mind = (json.loads(l) for l in out.stdout.splitlines())
    assert 'A["Funzione f continua su [a,b]"] --> B{"Valore f(a)?"}' in flow
    assert '-->|"f(x)=0"| C[Fine]' in flow
    assert mind.splitlines()[1:3] == ["  root((Java))", "    Object"]
    assert '      n3["toString()"]' in mind and "::icon(fa fa-book)" in mind
