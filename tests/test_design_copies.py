"""Design values that must live in several files, held to one another.

None of these copies can link the others. site/ publishes with no build step, the
graph frame stays self-contained for file:// (ADR-0026), and the app and Pages each
paint their own tokens, so one shared token sheet is not on offer and a compare is
the cheapest honest thing. KNOWN_DRIFT below is the only disagreement allowed, and
it is the owner's to settle: DESIGN.md at the repo root (gitignored) names one value,
a published page prints another, and a refactor does not restyle a live page.
"""
from __future__ import annotations

import importlib.util
import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "silica" / "ui" / "web" / "static"
SITE = ROOT / "site" / "index.html"

_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _site_css() -> str:
    html = SITE.read_text(encoding="utf-8")
    return html[html.index("<style>") + len("<style>"):html.index("</style>")]


def _top_blocks(css: str) -> dict[str, dict[str, str]]:
    """Custom properties per top-level selector, so the ones an @media holds stay
    out: only the block that sets a theme outright says what the theme is."""
    css = _COMMENT.sub("", css)
    blocks: dict[str, dict[str, str]] = {}
    depth, start, selector = 0, 0, ""
    for i, ch in enumerate(css):
        if ch == "{":
            if depth == 0:
                selector = " ".join(css[start:i].split())
                start = i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                pairs = re.findall(r"(--[\w-]+)\s*:\s*([^;}]+)", css[start:i])
                blocks.setdefault(selector, {}).update({k: _norm(v) for k, v in pairs})
                start = i + 1
    return blocks


def _norm(value: str) -> str:
    return re.sub(r"\s*,\s*", ",", " ".join(value.split())).lower()


def _themes(css: str, default: str) -> dict[str, dict[str, str]]:
    """{graphite, paper}: `default` is what :root is, the other its override."""
    b = _top_blocks(css)
    root = b[":root"]
    other = "paper" if default == "graphite" else "graphite"
    flip = b[':root[data-theme="light"]'] if default == "graphite" else b[':root[data-theme="dark"]']
    themes = {default: root, other: {**root, **flip}}
    for theme in themes.values():  # app-base spells --lift through a channel token
        for k, v in theme.items():
            theme[k] = re.sub(r"var\((--[\w-]+)\)", lambda m: theme.get(m.group(1), m.group(0)), v)
    return themes


def _sheets() -> dict[str, dict[str, dict[str, str]]]:
    return {
        "app-base.css": _themes((STATIC / "app-base.css").read_text(encoding="utf-8"), "graphite"),
        "graph-frame.css": _themes((STATIC / "graph-frame.css").read_text(encoding="utf-8"), "graphite"),
        "pages.css": _themes((STATIC / "pages" / "pages.css").read_text(encoding="utf-8"), "paper"),
        "site/index.html": _themes(_site_css(), "paper"),
    }


# The paper theme's reading plane and well. DESIGN.md says paper-sheet #FFFFFF and
# paper-well #E0D8C6; the landing and Pages print #FFFDF9 and #F5F1E9 and have since
# before it was written. Found 2026-11-06. Delete an entry when the owner picks.
KNOWN_DRIFT = {("paper", "--sheet"), ("paper", "--well")}


def test_a_token_two_sheets_define_has_one_value():
    sheets = _sheets()
    assert len(sheets["app-base.css"]["paper"]) > 30, "the parser lost the token block"
    drift = []
    for theme in ("graphite", "paper"):
        names = {k for s in sheets.values() for k in s[theme]}
        for name in sorted(names):
            seen = {s: t[theme][name] for s, t in sheets.items() if name in t[theme]}
            if len(set(seen.values())) > 1:
                drift.append((theme, name))
    assert set(drift) == KNOWN_DRIFT


def test_the_system_and_the_switch_print_one_graphite_on_pages():
    """pages.css writes graphite twice, once for a reader's system and once for
    the switch, and a CSS rule cannot be both selectors at those two depths."""
    css = _COMMENT.sub("", (STATIC / "pages" / "pages.css").read_text(encoding="utf-8"))
    system = re.search(r':root:not\(\[data-theme="light"\]\)\s*\{([^}]*)\}', css).group(1)
    switch = re.search(r':root\[data-theme="dark"\]\s*\{([^}]*)\}', css).group(1)
    assert " ".join(system.split()) == " ".join(switch.split())


def test_python_writes_the_sheets_colours():
    """The graph's edge colours are painted by the exporter, so they are hex in
    Python, and only a comment tied them to a token."""
    from silica.kernel.graph import graph_export as ge, overlays

    app = _sheets()["app-base.css"]
    assert overlays._EDGE_COLOR_GAP.lower() == app["graphite"]["--warn"]
    assert overlays._EDGE_COLOR_GAP_PAPER.lower() == app["paper"]["--warn"]
    assert ge._EDGE_COLOR_SIMILAR.lower() == app["graphite"]["--accent"]
    assert ge._EDGE_COLOR_SIMILAR_PAPER.lower() == app["paper"]["--accent"]
    assert ge._EDGE_COLOR_REQUIRES.lower() == app["graphite"]["--frost"]
    assert ge._EDGE_COLOR_REQUIRES_PAPER.lower() == app["paper"]["--frost"]


def test_every_surface_sets_the_same_three_faces():
    """site/ loads from Google Fonts what the app and Pages serve themselves, so
    a face that changes in one place has no other way to be noticed."""
    faces = {"Archivo", "Martian Mono", "Host Grotesk"}
    pages = (STATIC / "pages" / "pages.css").read_text(encoding="utf-8")
    assert set(re.findall(r'@font-face \{ font-family: "([^"]+)"', pages)) == faces
    link = re.search(r'href="https://fonts\.googleapis\.com/css2\?([^"]+)"', SITE.read_text(encoding="utf-8")).group(1)
    assert {f.split(":")[0].replace("+", " ") for f in re.findall(r"family=([^&]+)", link)} == faces
    for woff in ("archivo-latin.woff2", "martian-mono-latin.woff2", "host-grotesk-latin.woff2"):
        assert (STATIC / woff).is_file()


def _banners():
    spec = importlib.util.spec_from_file_location("build_banners", ROOT / "assets" / "build_banners.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_favicons_are_what_build_banners_writes():
    master = _banners().symbol()
    for copy in (ROOT / "assets" / "silica-mark.svg", STATIC / "favicon.svg", ROOT / "site" / "favicon.svg"):
        assert copy.read_text(encoding="utf-8") == master, copy


def test_every_inline_mark_is_the_computed_one():
    """The pages around these copies are hand-written, so build_banners cannot
    regenerate them; any path that starts on the mark's corner is held to it."""
    from silica.kernel import public_site

    shells, seed = _banners().mark_paths()
    pieces = {seed, shells, *re.findall(r"M[^Z]+Z", shells)}
    for name, text, count in (("app-chat.js", (STATIC / "app-chat.js").read_text(encoding="utf-8"), 4),
                              ("index.html", (STATIC / "index.html").read_text(encoding="utf-8"), 8),
                              ("site/index.html", SITE.read_text(encoding="utf-8"), 6),
                              ("public_site._BRAND", public_site._BRAND, 2)):
        paths = re.findall(r'"(M16 \d+ L[^"]*)"', text)
        assert len(paths) == count, name
        assert set(paths) <= pieces, name


def test_the_app_and_pages_print_a_cover_on_one_press():
    """One file, loaded by each when it first prints. What each keeps is what differs:
    the app's build-up and hover, Pages' plain print."""
    from silica.kernel import public_site

    assert "cover.js" in public_site.static_files()
    press = (STATIC / "pages" / "cover.js").read_text(encoding="utf-8")
    assert "N[i][3] >= 12" in press
    for script in (STATIC / "app-public.js", STATIC / "pages" / "pages.js"):
        text = script.read_text(encoding="utf-8")
        assert "coverPress(cv, N, w, h, pad)" in text and "N[i][3] >= 12" not in text
        assert '("--void"' in text and '("--frost"' in text
        assert "((255 << 24) | (c[2] << 16) | (c[1] << 8) | c[0]) >>> 0" in text


def test_a_key_note_is_the_same_note_on_every_cover():
    """The landing prints one bit on a canvas of its own and public_site an SVG
    for a reader without the script, so neither can share the press; what they
    do share is which note is drawn large."""
    from silica.kernel import public_site

    for name, text in (("cover.js", (STATIC / "pages" / "cover.js").read_text(encoding="utf-8")),
                       ("site/index.html", SITE.read_text(encoding="utf-8")),
                       ("public_site._svg", inspect.getsource(public_site._svg))):
        assert set(re.findall(r"\[3\] >= (\d+)", text)) == {"12"}, name


def _mermaid(path: Path, start: str, reads: str) -> tuple[str, set[str]]:
    text = path.read_text(encoding="utf-8")
    body = text[text.index(start):]
    body = body[:body.index("\n}\n" if path.name != "pages.js" else "\n  }\n")]
    return body, set(re.findall(r'"(--[\w-]+)"', body)) | set(re.findall(reads + r'\("(--[\w-]+)"', body))


def test_a_diagram_only_reads_tokens_its_surface_defines():
    """Two configs stay two: the app's maps a chart ramp Pages does not carry, and
    Pages has to lock what a note's own %%{init}%% may set. getPropertyValue on a
    name a sheet lacks is an empty string and mermaid then falls back on its own
    lavender, with no error anywhere."""
    app_css = "".join(p.read_text(encoding="utf-8") for p in sorted(STATIC.glob("app-*.css")))
    pages_css = (STATIC / "pages" / "pages.css").read_text(encoding="utf-8")
    app_body, app_reads = _mermaid(STATIC / "app-notes.js", "function mermaidConfig() {", "v")
    pages_body, pages_reads = _mermaid(STATIC / "pages" / "pages.js", "function diagramConfig() {", "css")
    assert len(app_reads) > 10 and len(pages_reads) > 5
    assert {t for t in app_reads if f"{t}:" not in app_css} == set()
    assert {t for t in pages_reads if f"{t}:" not in pages_css} == set()
    for body in (app_body, pages_body):
        for shared in ("startOnLoad: false", "suppressErrorRendering: true", 'curve: "basis", padding: 14'):
            assert shared in body
        assert re.search(r'theme: "base"', body) and re.search(r'fontSize: "14px"', body)
