# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The public page's files (ADR-0143): a profile, one page and one markdown
file per public note, their images, a sheet and two fonts, in a folder any
static host serves.

Rendered by the note drawer's own renderer (`server._linkify`), so a page
reads as the note reads in the app, with one difference that is the point:
the resolver only knows the public notes, so a link to anything else comes
out as its words (ADR-0143 point 4). Built in a temporary folder and swapped
in whole (`publish.swap_in`).
"""
from __future__ import annotations

import datetime
import html
import importlib.resources
import json
import logging
import os
import re
import shutil
from pathlib import Path
from urllib.parse import quote

from silica.kernel import publish

logger = logging.getLogger(__name__)

# What a page shows of a note's properties (ADR-0143 point 4); everything
# else in a frontmatter (sources, paths, a paper's ids) stays in the vault.
_SHOWN_PROPS = ("title", "tags", "date")
_ANCHOR = re.compile(r'<a class="note-link" data-path="([^"]*)">')
_ASSET_SRC = re.compile(r'/asset\?path=([^"\'\s>]+)')
_FONTS = ("archivo-latin.woff2", "martian-mono-latin.woff2")
_LANGS = {"italian": "it", "english": "en", "spanish": "es", "french": "fr",
          "german": "de", "portuguese": "pt"}


def _esc(text) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def _title(path: str, fm: dict) -> str:
    t = fm.get("title")
    return t.strip() if isinstance(t, str) and t.strip() else path.rsplit("/", 1)[-1].removesuffix(".md")


def _tags(fm: dict) -> list[str]:
    raw = fm.get("tags")
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, str) else []
    return [str(t).lstrip("#") for t in items if str(t).strip()]


def _slugs(paths: list[str], titles: dict[str, str]) -> dict[str, str]:
    """One address per note, from its title; two notes of one title in two
    folders get -2, -3 in path order, so a republish gives each the same one."""
    out, used = {}, set()
    for p in sorted(paths):
        base = publish.slugify(titles[p]) or "note"
        slug, n = base, 2
        while slug in used or slug == "index":
            slug, n = f"{base}-{n}", n + 1
        used.add(slug)
        out[p] = slug
    return out


def _author(fm: dict, h: str) -> str:
    """The handle a saved note credits, "" for the page's own notes."""
    a = fm.get("author")
    a = a.strip().lstrip("@") if isinstance(a, str) else ""
    return "" if not a or a == h else a


class _Site:
    """Where things are and how a page names another, for one build."""

    def __init__(self, h: str, base: str):
        self.h, self.base = h, base
        # Served under /@handle when the host says where; plain files beside
        # each other otherwise, so the folder also opens from disk.
        self.root = f"/@{h}" if base else ""

    def href(self, slug: str) -> str:
        return f"{self.root}/{slug}" if self.base else f"{slug}.html"

    def index(self) -> str:
        return self.root if self.base else "index.html"

    def asset(self, name: str) -> str:
        return f"{self.root}/assets/{name}" if self.base else f"assets/{name}"

    def absolute(self, slug: str = "") -> str:
        origin = self.base.split("/@", 1)[0]
        return f"{origin}{self.href(slug)}" if slug else self.base


def _report_link(url: str) -> str:
    """The channel for reports on a public page (ADR-0143 Open 5), from the
    host; a page without one shows none rather than a dead link."""
    to = os.environ.get("SILICA_PUBLIC_REPORT", "").strip()
    if not to:
        return ""
    href = (f"mailto:{to}?subject={quote('Report: ' + url)}"
            if "@" in to and "://" not in to else to)
    return f' · <a href="{_esc(href)}">Report this page</a>'


def _app() -> str:
    """The app a reader is sent to: to save a page, or to join. The public
    host answers nothing at its root (silica-hosted's caddy), so "Join" and
    "Published with Silica" go here too."""
    return os.environ.get("SILICA_PUBLIC_SAVE_URL", "").strip().rstrip("/") or "https://app.silica-agent.dev"


def _save_link(url: str) -> str:
    return f"{_app()}/?save={quote(url, safe='')}"


def _head(title: str, desc: str, site: _Site, *, canonical: str = "", md: str = "",
          lang: str = "", kind: str = "website") -> str:
    meta = [f"<title>{_esc(title)}</title>",
            f'<meta name="description" content="{_esc(desc)}">',
            f'<link rel="stylesheet" href="{_esc(site.asset("site.css"))}">']
    if canonical:
        meta += [f'<link rel="canonical" href="{_esc(canonical)}">',
                 f'<meta property="og:url" content="{_esc(canonical)}">']
    meta += [f'<meta property="og:title" content="{_esc(title)}">',
             f'<meta property="og:description" content="{_esc(desc)}">',
             f'<meta property="og:type" content="{kind}">',
             '<meta property="og:site_name" content="Silica">']
    if md:
        meta.append(f'<link rel="alternate" type="text/markdown" href="{_esc(md)}">')
    lang_attr = f' lang="{lang}"' if lang else ""
    return ("<!DOCTYPE html>\n<html" + lang_attr + '>\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            + "\n".join(meta) + "\n</head>\n")


def _top(site: _Site, crumb: str = "") -> str:
    join = f'<a class="btn small" href="{_esc(_app() + "/")}">Join Silica</a>' if site.base else ""
    return (f'<div class="page">\n<header class="bar"><a class="handle" href="{_esc(site.index())}">@{_esc(site.h)}</a>'
            f'{crumb}<span class="spacer"></span>{join}</header>\n')


# Silica's mark (site/index.html's brand): the page says whose tool made it once, at its foot.
_MARK = ('<svg class="mk" viewBox="0 0 256 256" aria-hidden="true"><path class="i" d="M16 112 L144 112 '
         'L144 240 L112 240 L112 144 L16 144 Z M16 64 L192 64 L192 240 L160 240 L160 96 L16 96 Z M16 16 '
         'L240 16 L240 240 L208 240 L208 48 L16 48 Z"/><path class="s" d="M16 160 L96 160 L96 240 L16 240 Z"/></svg>')


def _foot(site: _Site, url: str) -> str:
    made = f'<a href="{_esc(_app() + "/")}">Published with Silica</a>' if site.base else "Published with Silica"
    return f'<footer class="foot">{_MARK}{made}{_report_link(url)}</footer>\n</div>\n</body>\n</html>\n'


def _desc(body: str) -> str:
    """The first words of a note for a link preview, markup and links stripped."""
    text = re.sub(r"!?\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", body)
    text = re.sub(r"[#>*_`~\[\]()|-]+", " ", text)
    return " ".join(text.split())[:160]


def _md_for_reader(body: str, resolve, href_of) -> str:
    """The note as a reader saves it (ADR-0143 point 8): a link to a public
    note becomes a web link to its page, any other becomes its words, an
    embedded image points at the page's copy.
    shortcut: wikilinks inside a code fence are rewritten too; split on fences
    if a published note ever shows one mangled."""
    from silica.ui.web.server import _ASSET_EXTS, _WIKILINK

    def sub(m: re.Match) -> str:
        bang, inner = m.group(1), m.group(2)
        target, _, alias = inner.partition("|")
        target = target.split("#", 1)[0].strip()
        display = alias.strip() or target.rsplit("/", 1)[-1].removesuffix(".md")
        if bang and "." + target.rsplit(".", 1)[-1].lower() in _ASSET_EXTS:
            name = href_of(("asset", target))
            return f"![{display}]({name})" if name else ""
        hit = resolve(target)
        return f"[{display}]({href_of(('note', hit))})" if hit else display

    return _WIKILINK.sub(sub, body)


# shortcut: past this many notes the cover keeps the best linked ones, so the layout stays near a
# second; reopen if a page this large asks for every mark (count them in the published meta line)
COVER_MAX = 1500


def cover_layout(nodes: list[str], pairs: set[tuple[str, str]]) -> tuple[list[tuple[str, float, float, int]],
                                                                       list[tuple[int, int]]]:
    """The page's graph as its cover lays it out: each note at (x, y) in 0..1 with its degree, and
    the links as index pairs. One layout rule for the app's cover (`GET /public/cover`, links from
    the vault graph) and the published one (links from the rendered pages), so the two agree
    wherever their links do (ADR-0146 point 4 and its amendment of 2026-11-01). Not the Explore
    frame: it carries layers that would leak (ADR-0143 point 4). A fixed seed, so a republish of
    the same notes draws the same picture."""
    import networkx as nx

    if not nodes:
        return [], []
    g = nx.Graph()
    g.add_nodes_from(nodes)
    g.add_edges_from((a, b) for a, b in pairs if a != b)
    if len(nodes) > COVER_MAX:
        g = g.subgraph(sorted(nodes, key=lambda p: (-g.degree(p), p))[:COVER_MAX]).copy()
    order = sorted(g.nodes)
    if len(order) == 1:
        return [(order[0], 0.5, 0.5, 0)], []  # one note sits in the middle, not in a corner
    pos = nx.spring_layout(g, seed=7, k=1.6 / len(order) ** 0.5)
    xs = [pos[n][0] for n in order]
    ys = [pos[n][1] for n in order]
    sx, sy = (max(xs) - min(xs)) or 1.0, (max(ys) - min(ys)) or 1.0
    at = {n: i for i, n in enumerate(order)}
    marks = [(n, round((pos[n][0] - min(xs)) / sx, 4), round((pos[n][1] - min(ys)) / sy, 4), g.degree(n))
             for n in order]
    return marks, [(at[a], at[b]) for a, b in g.edges]


def mark_px(count: int, degree: int) -> int:
    """A cover mark's width in screen pixels: a sparse page prints bigger marks, so three notes read
    as three and not as specks, and a hub (twelve links) never prints under 5 px. The app's
    canvas cover and the published SVG both take it from here."""
    base = 7 if count < 40 else 5 if count < 200 else 3
    return max(base, 5 if degree >= 12 else 3)


_COVER_W, _COVER_H, _COVER_PAD = 1000, 420, 14


def _cover_svg(marks: list[tuple[str, float, float, int]], links: list[tuple[int, int]],
               titles: dict, hrefs: dict) -> str:
    """The cover printed at publish: notes as marks, links as lines, in the sheet's two inks.
    SVG and not the app's canvas because the public host allows no script (silica-hosted's
    Caddyfile serves /@handle under default-src 'none'); crisp edges keep every pixel one of the
    two inks, and the strokes are screen pixels, so a mark keeps its size on a phone. Each mark is
    a link to its note, out of the tab order: the list below is the way through by keyboard."""
    if not marks:
        return ""
    w, h, pad = _COVER_W, _COVER_H, _COVER_PAD
    xy = [(pad + x * (w - 2 * pad), pad + y * (h - 2 * pad)) for _, x, y, _ in marks]
    dots: dict[int, list[str]] = {}
    for (_, _, _, deg), (x, y) in zip(marks, xy):
        dots.setdefault(mark_px(len(marks), deg), []).append(f"M{x:.1f} {y:.1f}h0")
    out = [f'<svg class="graph" viewBox="0 0 {w} {h}" aria-hidden="true" focusable="false">']
    if links:
        out.append('<path class="ln" d="' + "".join(
            f"M{xy[a][0]:.1f} {xy[a][1]:.1f}L{xy[b][0]:.1f} {xy[b][1]:.1f}" for a, b in links) + '"/>')
    # a clear ring of the sheet under each mark first, so a note reads as a mark and not a crossing;
    # sizes are classes, not style attributes, which the host's style-src 'self' would drop
    for px, d in sorted(dots.items()):
        out.append(f'<path class="halo s{px}" d="{"".join(d)}"/>')
    for px, d in sorted(dots.items()):
        out.append(f'<path class="ink s{px}" d="{"".join(d)}"/>')
    for (p, _, _, _), (x, y) in zip(marks, xy):
        out.append(f'<a href="{_esc(hrefs[p])}" tabindex="-1"><title>{_esc(titles[p])}</title>'
                   f'<path class="hit" d="M{x:.1f} {y:.1f}h0"/></a>')
    out.append("</svg>")
    return "".join(out)


def build(vault: Path | None = None) -> dict:
    """Write the page from the current choice and make it live. Returns what
    went out, for the view's line after Publish."""
    from silica.driver import DRIVER
    from silica.kernel.recall.mindmap import note_resolver
    from silica.kernel.vault_manifest import get_active_manifest
    from silica.kernel.write import frontmatter as fm
    from silica.ui.web.server import _find_asset, _linkify

    vault = vault or publish._vault()
    h = publish.handle(vault)
    if not publish.available(vault):
        raise ValueError("this account has no public address yet")
    sel = publish.selection(vault)
    fms = sel["frontmatter"]
    public = sorted(p for p, s in sel["notes"].items() if s == "on")
    on = set(public)
    titles = {p: _title(p, fms.get(p) or {}) for p in public}
    slugs = _slugs(public, titles)
    site = _Site(h, publish.page_url(vault))
    lang = _LANGS.get((get_active_manifest().conventions.language or "").strip().lower(), "")

    base_resolve = note_resolver()

    def resolve(ref: str) -> str:
        # "" and not None: the renderer then writes the words with no
        # "broken" mark, which would say that a note stands behind them.
        hit = base_resolve(ref)
        return hit if hit in on else ""

    root = publish.site_root(h)
    root.mkdir(parents=True, exist_ok=True)
    build_dir = root / f".build-{os.getpid()}"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    (build_dir / "assets").mkdir(parents=True)
    _copy_static(build_dir / "assets")

    copied: dict[str, str] = {}

    def copy_asset(target: str) -> str:
        """The page's name for an image a public note cites, copied once."""
        if target in copied:
            return copied[target]
        found = _find_asset(target)
        if found is None:
            copied[target] = ""
            return ""
        name = f"{len(copied) + 1}-{publish.slugify(found.stem) or 'image'}{found.suffix.lower()}"
        shutil.copyfile(found, build_dir / "assets" / name)
        copied[target] = name
        return name

    def href_of(item) -> str:
        kind, target = item
        if kind == "asset":
            name = copy_asset(target)
            return site.asset(name) if name else ""
        return site.absolute(slugs[target]) if site.base else site.href(slugs[target])

    rendered: dict[str, dict] = {}
    edges: set[tuple[str, str]] = set()
    for p in public:
        try:
            text = DRIVER.read_note(p).content
        except Exception as exc:
            # One unreadable note must not take the whole page down with it;
            # it stays out and the log names it.
            logger.warning("public page: %s skipped (%s)", p, exc)
            continue
        props, _raw, body = fm.split(text)
        props = props or {}
        body = _drop_title_heading(body, titles[p])
        page_html = _linkify(body, resolve)
        targets = set(_ANCHOR.findall(page_html))
        edges.update((p, html.unescape(t)) for t in targets if html.unescape(t) in on)
        page_html = _ANCHOR.sub(lambda m: f'<a class="note-link" href="{_esc(site.href(slugs[html.unescape(m.group(1))]))}">'
                                if html.unescape(m.group(1)) in slugs else "<a>", page_html)
        page_html = _ASSET_SRC.sub(
            lambda m: site.asset(copy_asset(_unquote(m.group(1)))) if copy_asset(_unquote(m.group(1))) else "",
            page_html)
        rendered[p] = {"html": page_html, "props": props, "body": body}

    shown = [p for p in public if p in rendered]
    backlinks: dict[str, list[str]] = {p: [] for p in shown}
    for a, b in edges:
        if b in backlinks and a in rendered:
            backlinks[b].append(a)
    stamp = datetime.date.today().isoformat()

    for p in shown:
        r = rendered[p]
        props, slug = r["props"], slugs[p]
        url = site.absolute(slug) if site.base else ""
        credit = _author(props, h)
        source = props.get("source") if isinstance(props.get("source"), str) else ""
        source = source if source.startswith(("http://", "https://")) else ""
        # A republished copy points engines and savers at its original
        # (ADR-0143 point 10), so the copy cannot outrank the author.
        canonical = source if credit and source else url
        head = _head(f"{titles[p]} · @{h}", _desc(r["body"]), site, canonical=canonical,
                     md=f"{slug}.md", lang=lang, kind="article")
        crumb = f'<span class="crumb">/ {_esc(slug)}</span>'
        banner = ""
        if credit:
            saved = props.get("saved") or ""
            orig = f' <a href="{_esc(source)}">Read the original</a>' if source else ""
            banner = (f'<p class="banner">Saved from <b>@{_esc(credit)}</b>'
                      f'{" on " + _esc(saved) if saved else ""}. This is a copy, and @{_esc(credit)} '
                      f'may have changed the original since.{orig}</p>')
        date = props.get("date") or stamp
        byline = f"@{_esc(credit)} · shared by @{_esc(h)}" if credit else f"@{_esc(h)} · {_esc(date)}"
        tags = _tags(props)
        save = ""
        if site.base:
            target = source if credit and source else url
            note = f"Saves @{_esc(credit)}'s original" if credit and source else "A copy goes into your own vault"
            save = (f'<p class="actions"><a class="btn" href="{_esc(_save_link(target))}">Save to Silica</a>'
                    f'<span class="cap">{note}</span></p>')
        linked = "".join(f'<li><a href="{_esc(site.href(slugs[b]))}">{_esc(titles[b])}</a></li>'
                         for b in sorted(backlinks[p], key=lambda x: titles[x].lower()))
        more = [q for q in shown if q != p][:3]
        more_html = "".join(f'<li><a href="{_esc(site.href(slugs[q]))}">{_esc(titles[q])}</a></li>' for q in more)
        side = ""
        if linked:
            side += f'<div><h2 class="lab">Linked from</h2><ul>{linked}</ul></div>'
        if more_html:
            side += (f'<div><h2 class="lab">More from @{_esc(h)}</h2><ul>{more_html}'
                     f'<li><a href="{_esc(site.index())}">All {len(shown):,} notes</a></li></ul></div>')
        page = (head + "<body>\n" + _top(site, crumb)
                + f'<div class="art-grid{"" if side else " solo"}"><main class="sheet art">' + banner
                + f'<p class="byline">{byline}</p><h1>{_esc(titles[p])}</h1>'
                + (f'<p class="tags">{" · ".join(_esc(t) for t in tags)}</p>' if tags else "")
                + f'<article class="body">{r["html"]}</article>{save}</main>'
                + (f'<aside class="sheet side">{side}</aside>' if side else "") + "</div>\n"
                + _foot(site, url or slug))
        (build_dir / f"{slug}.html").write_text(page, encoding="utf-8")
        # the title always, so a saver names the copy as the page does
        kept = {"title": titles[p], **{k: props[k] for k in _SHOWN_PROPS if k in props and k != "title"}}
        if credit:
            kept.update({"author": f"@{credit}", **({"source": source} if source else {})})
        md = _md_for_reader(r["body"], resolve, href_of)
        (build_dir / f"{slug}.md").write_text(fm.dump(kept, md) if kept else md, encoding="utf-8")

    own = [p for p in shown if not _author(rendered[p]["props"], h)]
    others = [p for p in shown if _author(rendered[p]["props"], h)]
    pairs = {(min(a, b), max(a, b)) for a, b in edges if a in rendered and b in rendered and a != b}

    def row(p: str) -> str:
        deg = sum(1 for e in pairs if p in e)
        credit = _author(rendered[p]["props"], h)
        tail = f"from @{_esc(credit)}" if credit else " · ".join(_esc(t) for t in _tags(rendered[p]["props"]))
        return (f'<li><a href="{_esc(site.href(slugs[p]))}">{_esc(titles[p])}</a>'
                f'<span class="t">{tail}</span><span class="m">{deg} {"link" if deg == 1 else "links"}</span></li>')

    by_title = lambda ps: sorted(ps, key=lambda x: titles[x].lower())  # noqa: E731
    lists = (f'<h2 class="lab">Notes<span>{len(own):,}</span></h2>'
             f'<ol class="plist">{"".join(row(p) for p in by_title(own))}</ol>')
    if others:
        lists += (f'<h2 class="lab sub">Saved from others<span>{len(others):,}</span></h2>'
                  f'<ol class="plist">{"".join(row(p) for p in by_title(others))}</ol>')
    marks, links = cover_layout(shown, pairs)
    cover = _cover_svg(marks, links, titles, {p: site.href(slugs[p]) for p in shown})
    meta = (f'{len(shown):,} {"note" if len(shown) == 1 else "notes"} · {len(pairs):,} '
            f'{"link" if len(pairs) == 1 else "links"} · updated {stamp}')
    index = (_head(f"@{h} · Silica", f"{len(shown)} notes by @{h}", site, canonical=site.base, lang=lang)
             + "<body>\n" + _top(site)
             + '<section class="sheet profile">'
             # a page of a few notes prints big marks (mark_px) on a lower sheet, as the app's cover
             + (f'<figure class="cover{" sparse" if len(marks) < 40 else ""}">{cover}</figure>' if cover else "")
             + f'<div class="addr"><h1>@{_esc(h)}</h1><p class="meta">{_esc(meta)}</p></div></section>\n'
             + f'<section class="sheet notes">{lists}</section>\n'
             + _foot(site, site.base or "index.html"))
    (build_dir / "index.html").write_text(index, encoding="utf-8")
    state = {"published_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
             "notes": shown}
    (root / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    _readable(build_dir, root)
    publish.swap_in(build_dir, h)
    return {"notes": len(shown), "links": len(pairs), "images": sum(1 for v in copied.values() if v),
            "url": site.base, "published_at": state["published_at"]}


def _readable(build_dir: Path, root: Path) -> None:
    """Every published file readable by others, whatever the umask: on the
    server the account's unit writes with UMask=0077 and the web server reads
    as another user (silica-hosted, ADR-0143 point 6). What is published is
    public by definition; state.json beside it stays the account's."""
    root.chmod(0o755)
    build_dir.chmod(0o755)
    for p in build_dir.rglob("*"):
        p.chmod(0o755 if p.is_dir() else 0o644)


def last_published(h: str) -> dict:
    """{"published_at", "notes"} of the last build, {} before the first."""
    try:
        return json.loads((publish.site_root(h) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _drop_title_heading(body: str, title: str) -> str:
    """A body that opens on `# <its title>` loses that line: the page heads
    itself with the title already, and a note read twice in a row is noise."""
    m = re.match(r"\s*#[ \t]+(.+?)[ \t#]*\r?\n", body)
    return body[m.end():] if m and m.group(1).strip().lower() == title.strip().lower() else body


def _unquote(s: str) -> str:
    from urllib.parse import unquote

    return unquote(html.unescape(s))


def _copy_static(dest: Path) -> None:
    """The sheet and the fonts every page links, from the app's own files."""
    static = importlib.resources.files("silica.ui.web") / "static"
    for name in _FONTS:
        res = static / name
        if res.is_file():
            (dest / name).write_bytes(res.read_bytes())
    (dest / "site.css").write_text(_CSS, encoding="utf-8")


# The app's kit (root DESIGN.md "Public pages", ADR-0146 amendment 2026-11-01): the canvas, its
# sheets at 14 px, Archivo for words and Martian Mono for counts, the app's two ramps. Paper unless
# the reader's system is dark: a remembered switch, as the landing has, needs a script, and the
# public host allows none (silica-hosted Caddyfile, default-src 'none').
_CSS = """@font-face{font-family:"Archivo";src:url("archivo-latin.woff2") format("woff2");font-weight:100 900;font-stretch:62% 125%;font-display:swap}
@font-face{font-family:"Martian Mono";src:url("martian-mono-latin.woff2") format("woff2");font-weight:300 800;font-display:swap}
:root{--void:#EFEAE0;--slate:#F5F1E9;--sheet:#FFFDF9;--slate-2:#E9E2D4;--line:#D9D1C0;--line-2:#C6BCA8;
--frost:#1A1815;--text:#3A362F;--ash:#5B554B;--ash-dim:#655D4F;--accent:#0A6070;--on-vivid:#FFFFFF;
--r:14px;--r-sm:8px;--r-xs:4px;--gutter:12px;--text-w:43rem;
--sans:"Archivo",ui-sans-serif,system-ui,"Helvetica Neue",Arial,sans-serif;
--mono:"Martian Mono",ui-monospace,Menlo,Consolas,monospace;--ease:cubic-bezier(.2,.8,.2,1);--dur:.18s;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--void:#0D0E0F;--slate:#141518;--sheet:#23272B;--slate-2:#24272B;
--line:#2F343A;--line-2:#3F454D;--frost:#F4F5F7;--text:#C5CAD2;--ash:#AEB4BE;--ash-dim:#969CA6;--accent:#22B4CC;--on-vivid:#0D0E0F}}
*{box-sizing:border-box}html{background:var(--void)}
body{margin:0;padding:var(--gutter);background:var(--void);color:var(--text);font:400 15px/1.65 var(--sans);-webkit-font-smoothing:antialiased}
h1,h2,h3,p,figure,ol,ul{margin:0}h1,h2,h3{color:var(--frost);text-wrap:balance}
a{color:inherit;text-underline-offset:3px}b,strong{color:var(--frost)}
:focus-visible{outline:1px solid var(--accent);outline-offset:3px;border-radius:var(--r-xs)}
.page{max-width:76rem;margin:0 auto;display:grid;gap:var(--gutter)}
.sheet{background:var(--sheet);border-radius:var(--r);overflow:clip}
.bar{display:flex;align-items:center;gap:12px;min-height:56px;padding:0 10px 0 20px;background:var(--slate);border-radius:var(--r)}
.handle{font:600 15px/1 var(--sans);color:var(--frost);text-decoration:none}.crumb{font-size:13.5px;color:var(--ash)}.spacer{flex:1}
.btn{display:inline-flex;align-items:center;min-height:44px;padding:0 1.25rem;background:var(--accent);color:var(--on-vivid);border:1px solid var(--accent);border-radius:var(--r-sm);font:600 15px/1 var(--sans);text-decoration:none;transition:background-color var(--dur) var(--ease),border-color var(--dur) var(--ease)}
.btn:hover{background:var(--frost);border-color:var(--frost)}.btn:active{transform:translateY(1px)}
.btn.small{min-height:36px;padding:0 1rem;font-size:13px}
.profile{padding:clamp(16px,2.4vw,32px) clamp(16px,2.4vw,32px) 0}
.cover{height:clamp(240px,36vw,460px)}.cover.sparse{height:clamp(160px,18vw,220px)}
.graph{display:block;width:100%;height:100%;shape-rendering:crispEdges}
.graph path{fill:none;vector-effect:non-scaling-stroke;stroke-linecap:round}
.graph .ln{stroke:var(--frost);stroke-width:1px}.graph .halo{stroke:var(--sheet)}.graph .ink{stroke:var(--frost)}
.graph .halo.s3{stroke-width:5px}.graph .halo.s5{stroke-width:7px}.graph .halo.s7{stroke-width:9px}
.graph .ink.s3{stroke-width:3px}.graph .ink.s5{stroke-width:5px}.graph .ink.s7{stroke-width:7px}
.graph .hit{stroke:transparent;stroke-width:14px}.graph a:hover .hit{stroke:var(--frost);stroke-width:9px}
.addr{display:flex;align-items:baseline;justify-content:space-between;gap:16px;flex-wrap:wrap;padding:16px 0 24px}
.cover + .addr{border-top:1px solid var(--line);margin-top:16px}
.addr h1{font-size:clamp(2rem,1.3rem + 2.4vw,3.4rem);font-weight:700;line-height:1;letter-spacing:-.035em}
.meta{font-size:13px;color:var(--ash);font-variant-numeric:tabular-nums}
.notes{padding:20px clamp(16px,2.4vw,32px) 24px}
.lab{display:flex;align-items:baseline;gap:8px;font:500 13px/1.3 var(--sans);color:var(--ash)}
.lab span{font:400 12px/1 var(--mono);color:var(--ash-dim);font-variant-numeric:tabular-nums}
.lab.sub{margin-top:32px}
.plist{list-style:none;padding:0;margin-top:12px;columns:2 24rem;column-gap:clamp(1.25rem,3.6vw,4rem)}
.plist li{display:flex;align-items:baseline;gap:12px;min-height:40px;padding:9px 0;border-bottom:1px solid var(--line);break-inside:avoid}
.plist a{flex:1;min-width:0;font-size:15px;font-weight:500;color:var(--frost);text-decoration:none}
.plist a:hover{text-decoration:underline;text-decoration-color:var(--accent)}
.t{font-size:12px;color:var(--ash);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:40%}
.m{font:400 12px/1 var(--mono);color:var(--ash-dim);font-variant-numeric:tabular-nums;white-space:nowrap}
.art-grid{display:grid;grid-template-columns:minmax(0,1fr) 18rem;gap:var(--gutter);align-items:start}
.art-grid.solo{grid-template-columns:minmax(0,1fr)}
.art{padding:clamp(24px,4vw,56px)}.art > *{max-width:var(--text-w)}
.byline{font-size:12px;color:var(--ash);margin-bottom:12px}
.art > h1{font-size:clamp(2rem,1.3rem + 2.4vw,3.4rem);font-weight:700;line-height:1;letter-spacing:-.035em}
.tags{margin-top:12px;font-size:13px;color:var(--ash)}
.body{margin-top:32px;font-size:16px;line-height:1.72}.body > * + *{margin-top:1em}
.body h1,.body h2{font-size:1.35rem;line-height:1.25;font-weight:600;margin-top:2em}
.body h3,.body h4{font-size:1.05rem;font-weight:600;margin-top:1.6em}
.body a,.body .note-link{color:var(--frost);text-decoration:underline;text-decoration-color:var(--accent);text-decoration-thickness:1px}
.body ul,.body ol{padding-left:1.25em}.body li + li{margin-top:.35em}
.body blockquote{margin-left:0;padding:2px 16px;border-left:2px solid var(--line-2)}
.body code{font-family:var(--mono);font-size:.82em}
.body pre{background:var(--slate);border-radius:var(--r-sm);padding:12px 16px;overflow-x:auto;font-size:13px}
.body table{border-collapse:collapse;display:block;overflow-x:auto;font-size:14px}
.body th,.body td{border:1px solid var(--line);padding:6px 10px;text-align:left}
.body img{max-width:100%;height:auto;border-radius:var(--r-sm)}
.body .math{overflow-x:auto;padding:4px 0}.body math{font-size:1.1em;color:var(--frost)}
.body hr{border:0;border-top:1px solid var(--line);margin:2em 0}
.body .tag{font-family:var(--mono);font-size:.8em;color:var(--ash)}
.banner{border:1px solid var(--line-2);border-radius:var(--r-sm);padding:12px 16px;margin-bottom:24px;font-size:14px}
.banner a{color:var(--frost);text-decoration-color:var(--accent)}
.actions{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-top:48px;padding-top:24px;border-top:1px solid var(--line)}
.cap{font-size:13px;color:var(--ash)}
.side{padding:24px;display:grid;gap:24px}.side ul{list-style:none;padding:0;margin-top:8px;display:grid;gap:2px}
.side a{display:block;padding:6px 0;font-size:14px;color:var(--frost);text-decoration:none}
.side a:hover{text-decoration:underline;text-decoration-color:var(--accent)}
.foot{display:flex;justify-content:center;align-items:center;gap:8px;flex-wrap:wrap;padding:16px 0 4px;font-size:12px;color:var(--ash-dim)}
.foot a{color:inherit;text-decoration:none}.foot a:hover{color:var(--frost)}
.mk{width:14px;height:14px}.mk .i{fill:currentColor}.mk .s{fill:var(--accent)}
@media (max-width:860px){body{padding:8px}.art-grid{grid-template-columns:1fr}.bar .crumb{display:none}.t{display:none}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""
