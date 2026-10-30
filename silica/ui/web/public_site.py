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
_FONTS = ("host-grotesk-latin.woff2", "martian-mono-latin.woff2", "host-grotesk-OFL.txt")
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


def _save_link(url: str) -> str:
    app = os.environ.get("SILICA_PUBLIC_SAVE_URL", "").strip().rstrip("/") or "https://app.silica-agent.dev"
    return f"{app}/?save={quote(url, safe='')}"


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
    join = (f'<a class="btn ghost small" href="{_esc(site.absolute().split("/@", 1)[0] + "/")}">Join Silica</a>'
            if site.base else "")
    return (f'<header class="top"><a class="brand" href="{_esc(site.index())}">@{_esc(site.h)}</a>'
            f'{crumb}<span class="spacer"></span>{join}</header>\n')


def _foot(site: _Site, url: str) -> str:
    made = (f'<a href="{_esc(site.absolute().split("/@", 1)[0] + "/")}">Published with Silica</a>'
            if site.base else "Published with Silica")
    return f'<footer class="foot">{made}{_report_link(url)}</footer>\n</body>\n</html>\n'


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


def _graph_svg(public: list[str], edges: set[tuple[str, str]], titles: dict, slugs: dict,
               site: _Site) -> str:
    """The links between public notes, drawn once at publish. Not the Explore
    frame: it carries layers that would leak (ADR-0143 point 4) and two
    renderers per page. A fixed seed, so a republish of the same notes draws
    the same picture."""
    import networkx as nx

    if not public:
        return ""
    g = nx.Graph()
    g.add_nodes_from(public)
    g.add_edges_from((a, b) for a, b in edges if a != b)
    pos = nx.spring_layout(g, seed=7, k=1.6 / max(len(public), 1) ** 0.5) if len(public) > 1 \
        else {public[0]: (0.0, 0.0)}
    w, hgt, pad = 620, 400, 44
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    span_x = (max(xs) - min(xs)) or 1.0
    span_y = (max(ys) - min(ys)) or 1.0

    def at(n):
        x, y = pos[n]
        return (pad + (x - min(xs)) / span_x * (w - 2 * pad), pad + (y - min(ys)) / span_y * (hgt - 2 * pad))

    named = set(sorted(public, key=lambda n: (-g.degree(n), titles[n].lower()))[:14])
    out = [f'<svg viewBox="0 0 {w} {hgt}" role="img" aria-label="how these notes link">']
    for a, b in g.edges():
        (x1, y1), (x2, y2) = at(a), at(b)
        out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>')
    for n in sorted(public):
        x, y = at(n)
        r = 4.5 + 1.6 * min(g.degree(n), 9) ** 0.5
        label = (f'<text x="{x + r + 5:.1f}" y="{y + 4:.1f}">{_esc(titles[n])}</text>'
                 if n in named else "")
        out.append(f'<a href="{_esc(site.href(slugs[n]))}"><title>{_esc(titles[n])}</title>'
                   f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}"/>{label}</a>')
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
    if not h:
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
            note = f"saves @{_esc(credit)}'s original" if credit and source else "a copy goes into your own vault"
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
                     f'<li><a href="{_esc(site.index())}">All {len(shown)} notes</a></li></ul></div>')
        page = (head + "<body>\n" + _top(site, crumb)
                + '<div class="art-grid"><main class="art">' + banner
                + f'<p class="lab meta">{byline}</p><h1>{_esc(titles[p])}</h1>'
                + (f'<p class="tags">{" · ".join(_esc(t) for t in tags)}</p>' if tags else "")
                + f'<article class="body">{r["html"]}</article>{save}</main>'
                + (f'<aside class="side">{side}</aside>' if side else "") + "</div>\n"
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
                f'<span class="m">{tail}</span><span class="m">{deg} {"link" if deg == 1 else "links"}</span></li>')

    by_title = lambda ps: sorted(ps, key=lambda x: titles[x].lower())  # noqa: E731
    lists = f'<ol class="plist">{"".join(row(p) for p in by_title(own))}</ol>'
    if others:
        lists += f'<h2 class="lab sub">Saved from others</h2><ol class="plist">{"".join(row(p) for p in by_title(others))}</ol>'
    svg = _graph_svg(shown, pairs, titles, slugs, site)
    meta = f'{len(shown)} {"note" if len(shown) == 1 else "notes"} · {len(pairs)} links · updated {stamp}'
    index = (_head(f"@{h} · Silica", f"{len(shown)} notes by @{h}", site, canonical=site.base, lang=lang)
             + "<body>\n" + _top(site)
             + f'<section class="profile"><h1>@{_esc(h)}</h1><p class="lab">{_esc(meta)}</p></section>'
             + f'<div class="pub-grid"><div>{lists}</div>'
             + (f'<figure class="pgraph"><figcaption class="lab">How these notes link</figcaption>{svg}</figure>'
                if svg else "")
             + "</div>\n" + _foot(site, site.base or "index.html"))
    (build_dir / "index.html").write_text(index, encoding="utf-8")
    state = {"published_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
             "notes": shown}
    (root / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    publish.swap_in(build_dir, h)
    return {"notes": len(shown), "links": len(pairs), "images": sum(1 for v in copied.values() if v),
            "url": site.base, "published_at": state["published_at"]}


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


# The landing's plate (site/DESIGN.md): paper, one ink, one signal, square
# corners. Martian Mono for the labels because it ships with the app; the
# landing's JetBrains Mono does not.
_CSS = """@font-face{font-family:"Host Grotesk";src:url("host-grotesk-latin.woff2") format("woff2");font-weight:300 800;font-display:swap}
@font-face{font-family:"Martian Mono";src:url("martian-mono-latin.woff2") format("woff2");font-weight:300 800;font-display:swap}
:root{--paper:#FFFDF9;--ink:#1A1815;--ink-2:#4A453C;--hair:#CFC8BA;--signal:#0A6070;
--sans:"Host Grotesk",ui-sans-serif,system-ui,"Helvetica Neue",Arial,sans-serif;
--mono:"Martian Mono",ui-monospace,Menlo,Consolas,monospace;color-scheme:light}
*{box-sizing:border-box}html,body{margin:0}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);font-size:17px;line-height:1.55}
a{color:var(--ink);text-decoration-color:var(--signal);text-underline-offset:3px}a:hover{color:var(--signal)}
:focus-visible{outline:2px solid var(--signal);outline-offset:2px}
.lab{font-family:var(--mono);font-size:12px;font-weight:500;letter-spacing:.04em;text-transform:uppercase;color:var(--ink-2);margin:0}
.top{display:flex;align-items:center;gap:14px;min-height:58px;padding:0 max(16px,3vw);border-bottom:1px solid var(--hair)}
.brand{font-weight:700;letter-spacing:.02em;text-decoration:none}.crumb{font-family:var(--mono);font-size:12.5px;color:var(--ink-2)}
.spacer{flex:1}
.btn{display:inline-flex;align-items:center;height:44px;padding:0 1.1rem;background:var(--ink);color:var(--paper);border:1px solid var(--ink);font:600 16px/1 var(--sans);text-decoration:none}
.btn:hover{background:var(--signal);border-color:var(--signal);color:var(--paper)}
.btn.ghost{background:transparent;color:var(--ink)}.btn.ghost:hover{color:var(--signal);background:transparent}
.btn.small{height:36px;font-size:14.5px;padding:0 .9rem}
.profile{padding:30px max(16px,3vw) 18px;border-bottom:1px solid var(--ink);display:grid;gap:6px}
.profile h1{margin:0;font-size:clamp(1.8rem,1.3rem + 2vw,2.6rem);letter-spacing:-.03em;line-height:1.05}
.pub-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,560px)}
.plist{list-style:none;margin:0;padding:6px max(16px,3vw) 22px}
.plist li{display:grid;grid-template-columns:1fr auto 70px;gap:14px;align-items:baseline;padding:12px 0;border-bottom:1px solid var(--hair)}
.plist a{font-size:18px;font-weight:600;text-decoration:none;letter-spacing:-.01em}.plist a:hover{text-decoration:underline}
.m{font-family:var(--mono);font-size:11.5px;color:var(--ink-2);text-align:right}
.sub{padding:18px max(16px,3vw) 0}
.pgraph{margin:0;border-left:1px solid var(--ink);padding:18px 22px;display:grid;gap:8px;align-content:start}
.pgraph svg{width:100%;height:auto}.pgraph line{stroke:var(--ink);stroke-width:1.1}
.pgraph circle{fill:var(--paper);stroke:var(--ink);stroke-width:1.5}.pgraph a:hover circle{fill:var(--signal)}
.pgraph text{font-family:var(--sans);font-size:12.5px;font-weight:600;fill:var(--ink)}
.art-grid{display:grid;grid-template-columns:minmax(0,1fr) 300px}
.art{padding:34px max(16px,3vw) 30px;max-width:780px}
.art .meta{margin-bottom:10px}
.art h1{margin:0 0 6px;font-size:clamp(2rem,1.4rem + 2.4vw,2.8rem);line-height:1.02;letter-spacing:-.035em}
.tags{margin:0 0 22px;font-family:var(--mono);font-size:12.5px;color:var(--ink-2)}
.body{font-size:18px;line-height:1.6}.body p,.body ul,.body ol,.body pre,.body blockquote,.body table{max-width:66ch}
.body img{max-width:100%;height:auto}.body pre{overflow-x:auto;padding:12px;border:1px solid var(--hair);font-size:14px}
.body code{font-family:var(--mono);font-size:.85em}.body table{border-collapse:collapse;display:block;overflow-x:auto}
.body th,.body td{border:1px solid var(--hair);padding:4px 8px}
.body blockquote{margin:0;padding:2px 14px;border-left:2px solid var(--ink)}
.body .note-link{text-decoration:underline;text-decoration-color:var(--signal);text-decoration-thickness:1.5px}
.body .tag{font-family:var(--mono);font-size:.8em;color:var(--ink-2)}
.banner{border:1px solid var(--ink);padding:12px 14px;margin:0 0 22px;font-size:15.5px;max-width:66ch}
.banner a{color:var(--signal)}
.actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:28px}.cap{font-size:14px;color:var(--ink-2)}
.side{border-left:1px solid var(--hair);padding:34px 24px;display:grid;gap:22px;align-content:start}
.side ul{list-style:none;margin:8px 0 0;padding:0;display:grid;gap:6px}.side a{text-decoration:none}.side a:hover{text-decoration:underline}
.foot{display:flex;gap:6px;flex-wrap:wrap;padding:14px max(16px,3vw);border-top:1px solid var(--ink);font-size:13.5px;color:var(--ink-2)}
.foot a{color:var(--ink-2)}
@media (max-width:860px){.pub-grid,.art-grid{grid-template-columns:1fr}.pgraph,.side{border-left:0;border-top:1px solid var(--hair)}
.plist li{grid-template-columns:1fr auto}.plist li .m:last-child{display:none}}
"""
