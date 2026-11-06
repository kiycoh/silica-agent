# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A Page's files (ADR-0143, ADR-0151): the Page that lists the account's
Public vaults, and for each vault its own page, one page and one markdown
file per note, its images, and two files the script and a reader's Silica
read (`vault.json`, `notes.json`).

Rendered by the note drawer's own renderer (`server._linkify`), so a page
reads as the note reads in the app, with one difference that is the point:
the resolver only knows the vault's published notes, so a link to anything
else comes out as its words (ADR-0143 point 4). A vault is built in a
temporary folder and swapped in whole (`publish.swap_in`).
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

from silica.kernel import publish, rights
from silica.kernel.image_meta import strip_location

logger = logging.getLogger(__name__)

# What a page shows of a note's properties (ADR-0143 point 4); everything
# else in a frontmatter (sources, paths, a paper's ids) stays in the vault.
_SHOWN_PROPS = ("title", "tags", "date")
_ANCHOR = re.compile(r'<a class="note-link" data-path="([^"]*)">')
_ASSET_SRC = re.compile(r'/asset\?path=([^"\'\s>]+)')
# The sheet, the script and their fonts: the host serves them from /_s/ (ADR-0151 point 7); a page
# built on a laptop carries its own copy in _s/ so its folder opens from disk. mermaid.min.js is
# the app's own copy, which pages.js loads only on a note that holds a diagram (owner, 2026-11-02):
# the policy runs scripts from /_s/ alone, so it is a file there and never a script in a page.
_STATIC = ("pages/pages.css", "pages/pages.js", "archivo-latin.woff2", "martian-mono-latin.woff2",
           "host-grotesk-latin.woff2", "host-grotesk-OFL.txt", "mermaid.min.js")
_LANGS = {"italian": "it", "english": "en", "spanish": "es", "french": "fr",
          "german": "de", "portuguese": "pt"}
# shortcut: past this many linked notes the cover keeps the best linked ones, so the layout stays
# near a second; reopen if a vault this large asks for every mark (count them in the caption)
_COVER_MAX = 1500
# The local Silica a reader may save into (ADR-0151 point 8), `silica --gui`'s default port.
LOCAL_APP = "http://localhost:8765"
_BRAND = ('<svg viewBox="0 0 256 256" aria-hidden="true"><path class="i" d="M16 112 L144 112 L144 240 '
          'L112 240 L112 144 L16 144 Z M16 64 L192 64 L192 240 L160 240 L160 96 L16 96 Z M16 16 L240 16 '
          'L240 240 L208 240 L208 48 L16 48 Z"/><path class="s" d="M16 160 L96 160 L96 240 L16 240 Z"/></svg>')
_THEME = ('<button class="theme" type="button" data-theme-btn aria-label="Switch between paper and graphite">'
          '<svg class="moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20.5 14.5A8.5 8.5 0 0 1 9.5 '
          '3.5a8.5 8.5 0 1 0 11 11z"/></svg><svg class="sun" viewBox="0 0 24 24" aria-hidden="true">'
          '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 '
          '12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg></button>')


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
    folders get -2, -3 in path order, so a republish gives each the same one.
    The vault's own files keep their names."""
    out, used = {}, {"index", "vault", "notes", "assets", "readers"}
    for p in sorted(paths):
        base = publish.slugify(titles[p]) or "note"
        slug, n = base, 2
        while slug in used:
            slug, n = f"{base}-{n}", n + 1
        used.add(slug)
        out[p] = slug
    return out


def _author(fm: dict, h: str) -> str:
    """The handle a saved note credits, "" for the page's own notes."""
    a = fm.get("author")
    a = a.strip().lstrip("@") if isinstance(a, str) else ""
    return "" if not a or a == h else a


def _fmt(n: int) -> str:
    return f"{n:,}"


def _plural(n: int, one: str, many: str = "") -> str:
    return f"{_fmt(n)} {one if n == 1 else (many or one + 's')}"


def _day(iso: str) -> str:
    """"2026-11-02" -> "2 Nov 2026"; anything else as it came."""
    try:
        d = datetime.date.fromisoformat(iso[:10])
    except ValueError:
        return iso
    return f"{d.day} {d.strftime('%b %Y')}"


class _Site:
    """Where things are and how a page names another, for one build.

    Served under /@handle when the host says where, with the sheet at /_s/;
    plain files beside each other otherwise, so the folder also opens from
    disk. `lvl` is how deep the page being written sits: 0 the Page, 1 a
    vault's folder."""

    def __init__(self, h: str, base: str):
        self.h, self.base = h, base
        self.root = f"/@{h}" if base else ""

    def _up(self, lvl: int) -> str:
        return "../" * lvl

    def origin(self) -> str:
        return self.base.split("/@", 1)[0]

    def static(self, name: str, lvl: int) -> str:
        return f"/_s/{name}" if self.base else f"{self._up(lvl)}_s/{name}"

    def page(self, lvl: int) -> str:
        return f"{self.root}/" if self.base else f"{self._up(lvl)}index.html"

    def vault(self, slug: str, lvl: int) -> str:
        if self.base:
            return f"{self.root}/{slug}/"
        return "index.html" if lvl == 1 else f"{slug}/index.html"

    def note(self, vslug: str, slug: str, lvl: int) -> str:
        if self.base:
            return f"{self.root}/{vslug}/{slug}"
        return f"{slug}.html" if lvl == 1 else f"{vslug}/{slug}.html"

    def asset(self, vslug: str, name: str, lvl: int) -> str:
        return f"{self.root}/{vslug}/assets/{name}" if self.base else f"assets/{name}"

    def absolute(self, path: str) -> str:
        """An absolute address of a path this site wrote, "" without a base."""
        return f"{self.origin()}{path}" if self.base else ""

    def catalog(self) -> str:
        return f"{self.origin()}/" if self.base else ""


def _report(url: str, site: _Site) -> str:
    """The Report button (ADR-0151 point 11): a short form the public host's
    pages service answers. A page built without an address has no host to
    answer it, so it shows the channel, if any (ADR-0143 Open 5)."""
    if site.base:
        reasons = (("rights", "It publishes a work its author has no right to publish"),
                   ("personal", "It shows someone's personal data"),
                   ("other", "Something else"))
        radios = "".join(f'<label><input type="radio" name="why" value="{v}"{" checked" if i == 0 else ""}> '
                         f'{_esc(t)}</label>' for i, (v, t) in enumerate(reasons))
        return (f'<details class="report"><summary class="btn-o">Report</summary>'
                f'<form class="panel" method="post" action="/_report">'
                f'<p class="pt">What is wrong with this page?</p>{radios}'
                f'<input type="hidden" name="page" value="{_esc(url)}">'
                f'<textarea class="sel" name="text" rows="3" maxlength="2000" '
                f'placeholder="What should be looked at? (optional)" aria-label="Details"></textarea>'
                f'<div class="row"><button type="submit" class="btn btn--small">Send report</button></div>'
                f'</form></details>')
    to = os.environ.get("SILICA_PUBLIC_REPORT", "").strip()
    if not to:
        return ""
    href = (f"mailto:{to}?subject={quote('Report: ' + url)}" if "@" in to and "://" not in to else to)
    return f'<a class="btn-o report" href="{_esc(href)}">Report</a>'


def _app() -> str:
    """The hosted app a reader is sent to: to save a page, or to join."""
    return os.environ.get("SILICA_PUBLIC_SAVE_URL", "").strip().rstrip("/") or "https://app.silica-agent.dev"


def _save_form(url: str, label: str, form_id: str = "save") -> str:
    """Save into a Silica the reader names (ADR-0151 point 8): two submit
    buttons, one per Silica the page knows, for a reader without the script.
    With it the room asks instead, and reads the two addresses from here. A
    GET, as ADR-0143 point 8 asks: the reader's Silica asks before it writes."""
    return (f'<form class="vp-save" id="{form_id}" method="get" action="{_esc(_app())}/" data-save>'
            f'<input type="hidden" name="save" value="{_esc(url)}">'
            f'<details class="where"><summary class="btn">{_esc(label)}</summary>'
            f'<div class="panel"><p class="pt">Save into which Silica?</p>'
            f'<div class="row"><button type="submit" class="btn btn--small" formaction="{_esc(_app())}/">'
            f'Silica hosted</button><button type="submit" class="btn-o" formaction="{LOCAL_APP}/">'
            f'Silica on this computer</button></div>'
            f'<p class="dim">Your Silica asks before it writes anything, and every note keeps its author\'s '
            f'name.</p></div></details></form>')


def _room(site: _Site, vslug: str, note: str = "") -> str:
    """What the script reads to turn a vault's page, or a note's, into the room (ADR-0151, amendment
    of 2026-11-06). Absolute on the host: caddy also serves the vault at /@h/<vault> with no slash,
    where a relative "vault.json" would name the Page's folder instead."""
    here = f"{site.root}/{vslug}/" if site.base else ""
    vaults = f"{site.root}/vaults.json" if site.base else "../vaults.json"
    return (f' data-room="{_esc(here)}vault.json" data-here="{_esc(here)}" data-vaults="{_esc(vaults)}"'
            + ("" if site.base else ' data-ext=".html"')
            + (f' data-note="{_esc(note)}"' if note else ""))


def _head(title: str, desc: str, site: _Site, lvl: int, *, canonical: str = "", md: str = "",
          lang: str = "", kind: str = "website") -> str:
    meta = [f"<title>{_esc(title)}</title>",
            f'<meta name="description" content="{_esc(desc)}">',
            f'<link rel="stylesheet" href="{_esc(site.static("pages.css", lvl))}">',
            f'<script src="{_esc(site.static("pages.js", lvl))}" defer></script>']
    if canonical:
        meta += [f'<link rel="canonical" href="{_esc(canonical)}">',
                 f'<meta property="og:url" content="{_esc(canonical)}">']
    meta += [f'<meta property="og:title" content="{_esc(title)}">',
             f'<meta property="og:description" content="{_esc(desc)}">',
             f'<meta property="og:type" content="{kind}">',
             '<meta property="og:site_name" content="Silica">']
    if md:
        meta.append(f'<link rel="alternate" type="text/markdown" href="{_esc(md)}">')
    return ('<!DOCTYPE html>\n<html lang="' + (lang or "en") + '">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            + "\n".join(meta) + "\n</head>\n")


def top(catalog: str, app: str, current: str = "", page: str = "", h: str = "") -> str:
    """The bar every public page shares, the catalog's included."""
    cur = ' aria-current="page"'
    pages = f'<a href="{_esc(catalog)}"{cur if current == "pages" else ""}>Pages</a>' if catalog else ""
    own = (f'<a class="opt" href="{_esc(page)}"{cur if current == "page" else ""}>@{_esc(h)}</a>'
           if page and h else "")
    brand = f'<a class="brand" href="{_esc(app)}/" aria-label="Silica">{_BRAND}<b>Silica</b></a>'
    return (f'<div class="top-bar"><header class="top wrap">{brand}<nav aria-label="Primary">{pages}{own}'
            f'{_THEME}<a class="btn btn--small" href="{_esc(app)}/">Open Silica</a></nav></header></div>\n')


def foot(left: str, app: str) -> str:
    return (f'<footer class="foot wrap"><span>{left}</span><span><a href="{_esc(app)}/privacy">Privacy</a>'
            f' · <a href="{_esc(app)}/terms">Terms</a> · <a href="{_esc(app)}/">Published with Silica</a>'
            f'</span></footer>\n</body>\n</html>\n')


def _desc(body: str) -> str:
    """The first words of a note for a link preview, markup and links stripped."""
    text = re.sub(r"!?\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", body)
    text = re.sub(r"[#>*_`~\[\]()|-]+", " ", text)
    return " ".join(text.split())[:160]


def _md_for_reader(body: str, resolve, href_of) -> str:
    """The note as a reader saves it (ADR-0143 point 8): a link to a note of
    the vault becomes a web link to its page, any other becomes its words, an
    embedded image points at the vault's copy.
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


def cover(nodes: list[str], pairs: set[tuple[str, str]], titles: dict[str, str]) -> dict:
    """The vault's graph as the cover prints it and Explore groups it
    (ADR-0151 point 5): the linked notes laid out by a spring layout (seed 7,
    so a republish of the same links draws the same picture), the areas
    Louvain's on the same links, each named by its hub note. A note with no
    link is not drawn and belongs to no area."""
    import networkx as nx

    from silica.kernel.recall.graph_export import hub_of, louvain_sorted

    g = nx.Graph()
    # sorted: Louvain and the layout both follow insertion order, and the view's preview and the
    # published vault reach the same links in different orders (4 areas against 5, 2026-11-02)
    g.add_edges_from(sorted((a, b) for a, b in pairs if a != b))
    if g.number_of_nodes() > _COVER_MAX:
        g = g.subgraph(sorted(g.nodes, key=lambda p: (-g.degree(p), p))[:_COVER_MAX]).copy()
    drawn = sorted(g.nodes)
    areas = louvain_sorted(g) if drawn else []
    deg = dict(g.degree())
    group = {n: i for i, members in enumerate(areas) for n in members}
    names = [titles[hub_of(members, deg)] for members in areas]
    pos = nx.spring_layout(g, seed=7, k=1.6 / max(len(drawn), 1) ** 0.5) if len(drawn) > 1 \
        else {n: (0.0, 0.0) for n in drawn}
    pos = _lie_flat(pos)
    xs = [pos[n][0] for n in drawn] or [0.0]
    ys = [pos[n][1] for n in drawn] or [0.0]
    sx, sy = (max(xs) - min(xs)) or 1.0, (max(ys) - min(ys)) or 1.0
    at = {n: i for i, n in enumerate(drawn)}
    return {
        "nodes": drawn,
        "n": [[round((pos[n][0] - min(xs)) / sx, 4), round((pos[n][1] - min(ys)) / sy, 4),
               group.get(n, -1), deg[n]] for n in drawn],
        "e": sorted([at[a], at[b]] for a, b in g.edges),
        "areas": [[names[i], len(m)] for i, m in enumerate(areas)],
        "unlinked": [n for n in nodes if n not in at],
    }


def _lie_flat(pos: dict) -> dict:
    """The layout turned so its longest spread runs across: the cover is 46:21, each axis is
    stretched to fill it, and a layout whose spread runs down came out as two flat smears (the
    Silica Wiki's two groups and their one bridge, 2026-11-02)."""
    import numpy as np

    if len(pos) < 3:
        return pos
    keys = list(pos)
    xy = np.array([pos[k] for k in keys], dtype=float)
    xy -= xy.mean(axis=0)
    _vals, vecs = np.linalg.eigh(np.cov(xy.T))
    turned = xy @ vecs[:, ::-1]  # the largest spread first, as x
    return {k: (float(turned[i, 0]), float(turned[i, 1])) for i, k in enumerate(keys)}


def _svg(c: dict) -> str:
    """The cover a reader without the script sees: the same points and
    lines, in one ink, drawn once at publish."""
    w, h, pad = 460, 210, 10
    X = lambda i: pad + c["n"][i][0] * (w - 2 * pad)  # noqa: E731
    Y = lambda i: pad + c["n"][i][1] * (h - 2 * pad)  # noqa: E731
    out = [f'<svg viewBox="0 0 {w} {h}" aria-hidden="true">']
    out += [f'<line x1="{X(a):.1f}" y1="{Y(a):.1f}" x2="{X(b):.1f}" y2="{Y(b):.1f}"/>' for a, b in c["e"]]
    out += [f'<circle cx="{X(i):.1f}" cy="{Y(i):.1f}" r="{1.6 if n[3] >= 12 else 1.1}"/>'
            for i, n in enumerate(c["n"])]
    return "".join(out) + "</svg>"


def build(slug: str, vault: Path | None = None) -> dict:
    """Write one vault from the current choice and put it where its
    visibility says: served when Public, beside the served folder for the
    relay when Private (ADR-0151 points 3, 9). Returns what went out, for
    the view's line after Publish."""
    from silica.driver import DRIVER
    from silica.kernel.recall.mindmap import note_resolver
    from silica.kernel.write import frontmatter as fm
    from silica.ui.web.server import _find_asset, _linkify, _strip_ofm_meta

    vault = vault or publish._vault()
    h = publish.handle(vault)
    if not publish.available(vault):
        raise ValueError("this account has no public address yet")
    publish.enforce_tier(vault)
    conf = publish.find(slug, vault)
    if conf["visibility"] == "off":
        raise ValueError("turn the wiki Public or Private before publishing it")
    private = conf["visibility"] == "private"
    sel = publish.selection(vault)
    fms = sel["frontmatter"]
    # Here and not in the screen: the check before Publish only reads these answers, so a wiki
    # nobody looked at goes out as one nobody answered for (ADR-0153 points 20 and 26).
    wiki = rights.read_wiki(slug, vault)
    said = rights.answers(vault)
    plan = rights.decide(wiki, said)
    public = sorted(plan["notes"])
    on = set(public)
    titles = {p: _title(p, fms.get(p) or {}) for p in public}
    slugs = _slugs(public, titles)
    site = _Site(h, publish.page_url(vault))
    vurl = site.absolute(site.vault(slug, 0))
    lang = _LANGS.get(_language().strip().lower(), "")

    base_resolve = note_resolver()

    def resolve(ref: str) -> str:
        # "" and not None: the renderer then writes the words with no
        # "broken" mark, which would say that a note stands behind them.
        hit = base_resolve(ref)
        return hit if hit in on else ""

    root = publish.site_root(h)
    root.mkdir(parents=True, exist_ok=True)
    build_dir = root / f".build-{slug}-{os.getpid()}"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    (build_dir / "assets").mkdir(parents=True)

    copied: dict[str, str] = {}

    def copy_asset(target: str) -> str:
        """The vault's name for an image a published note cites, copied once."""
        if target in copied:
            return copied[target]
        found = plan["files"].get(target) or _find_asset(target)
        if found is None:
            copied[target] = ""
            return ""
        name = f"{len(copied) + 1}-{publish.slugify(found.stem) or 'image'}{found.suffix.lower()}"
        (build_dir / "assets" / name).write_bytes(strip_location(found.read_bytes()))
        copied[target] = name
        return name

    def note_url(p: str) -> str:
        # A Private vault is never served, but its notes keep the address they would have, so a
        # reader's Silica tells its links apart as it does a Public vault's (saved_page.save_vault).
        return site.absolute(site.note(slug, slugs[p], 1)) or f"{slugs[p]}.md"

    def href_of(item) -> str:
        kind, target = item
        if kind == "asset":
            name = copy_asset(target)
            if not name:
                return ""
            return f"assets/{name}" if private or not site.base else site.absolute(site.asset(slug, name, 1))
        return note_url(target) if site.base or private else site.note(slug, slugs[target], 1)

    rendered: dict[str, dict] = {}
    edges: set[tuple[str, str]] = set()
    for p in public:
        try:
            text = DRIVER.read_note(p).content
        except Exception as exc:
            # One unreadable note must not take the whole vault down with it;
            # it stays out and the log names it.
            logger.warning("public page: %s skipped (%s)", p, exc)
            continue
        props, _raw, body = fm.split(text)
        props = props or {}
        # Once, here: the page, the `.md`, notes.json and the description all read this
        # body, and only _linkify stripped comments, so a `%% remark %%` the page hid went
        # out in the `.md` and into the room's text search, and hidden text reached the
        # vault of whoever saved the note.
        body = rights.place_images(_drop_title_heading(_strip_ofm_meta(body, published=True), titles[p]), plan)
        page_html = _linkify(body, resolve, remote_images=False)
        targets = set(_ANCHOR.findall(page_html))
        edges.update((p, html.unescape(t)) for t in targets if html.unescape(t) in on)
        page_html = _ANCHOR.sub(
            lambda m: f'<a class="note-link" href="{_esc(site.note(slug, slugs[html.unescape(m.group(1))], 1))}">'
            if html.unescape(m.group(1)) in slugs else "<a>", page_html)
        page_html = _ASSET_SRC.sub(
            lambda m: site.asset(slug, copy_asset(_unquote(m.group(1))), 1) if copy_asset(_unquote(m.group(1))) else "",
            page_html)
        rendered[p] = {"html": page_html, "props": props, "body": body, "credit": plan["note_credits"].get(p, "")}

    shown = [p for p in public if p in rendered]
    pairs = {(min(a, b), max(a, b)) for a, b in edges if a in rendered and b in rendered and a != b}
    c = cover(shown, pairs, titles)
    today = datetime.date.today().isoformat()
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    blocked = rights.blocks(wiki, said, plan)
    manifest = {
        "v": 1, "handle": h, "slug": slug, "title": conf["title"], "line": conf["line"],
        "license": conf["license"], "published": today, "listed": conf["listed"] and not private and not blocked,
        "blocked": bool(blocked), "rights": rights.statement(wiki, said, plan, slugs),
        "url": vurl,
        "count": {"notes": len(shown), "links": len(pairs), "drawn": len(c["n"]), "areas": len(c["areas"])},
        "areas": c["areas"],
        # a drawn note: x, y, area, links, title, slug; the cover and the areas read nothing else
        "n": [[*row, titles[p], slugs[p]] for row, p in zip(c["n"], c["nodes"])],
        "e": c["e"],
        "unlinked": [[titles[p], slugs[p]] for p in c["unlinked"]],
    }
    notes_md: dict[str, str] = {}
    for p in shown:
        props = rendered[p]["props"]
        credit = _author(props, h)
        source = props.get("source") if isinstance(props.get("source"), str) else ""
        source = source if source.startswith(("http://", "https://")) else ""
        kept = {"title": titles[p], **{k: props[k] for k in _SHOWN_PROPS if k in props and k != "title"},
                "vault": conf["title"]}
        if credit:
            kept.update({"author": f"@{credit}", **({"source": source} if source else {})})
        if rendered[p]["credit"]:
            kept["credit"] = rendered[p]["credit"]
        md = fm.dump(kept, _md_for_reader(rendered[p]["body"], resolve, href_of))
        notes_md[slugs[p]] = md
        if not private:
            (build_dir / f"{slugs[p]}.md").write_text(md, encoding="utf-8")
    (build_dir / "vault.json").write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                                          encoding="utf-8")
    (build_dir / "notes.json").write_text(json.dumps(notes_md, ensure_ascii=False), encoding="utf-8")

    if private:
        (build_dir / "readers.json").write_text(json.dumps(conf["readers"]), encoding="utf-8")
        dest = root / "private" / slug
    else:
        backlinks: dict[str, list[str]] = {p: [] for p in shown}
        for a, b in edges:
            if b in backlinks and a in rendered:
                backlinks[b].append(a)
        for p in shown:
            _write_note(build_dir, p, rendered[p], titles, slugs, backlinks[p], shown, conf, site, slug, h,
                        lang, today)
        (build_dir / "index.html").write_text(_vault_page(manifest, c, site, h, lang), encoding="utf-8")
        dest = root / "live" / slug
    _readable(build_dir, root, private)
    publish.swap_in(build_dir, dest)
    if not private:
        shutil.rmtree(root / "paused" / slug, ignore_errors=True)
    _mark_published(slug, today, vault)
    state = last_published(h) or {"vaults": {}}
    # The reasons stay here, for the author: the manifest is public, and one of them says a note
    # holds someone's personal data.
    state["vaults"][slug] = {"published_at": stamp, "notes": shown, "blocked": blocked}
    (root / "state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    write_page(vault)
    return {"vault": slug, "notes": len(shown), "links": len(pairs),
            "images": sum(1 for v in copied.values() if v), "url": vurl, "published_at": stamp,
            "private": private}


def _mark_published(slug: str, day: str, vault: Path) -> None:
    items = publish.vaults(vault)
    for v in items:
        if v["slug"] == slug:
            v["published"] = day
    publish._save_vaults(items, vault)


def _write_note(build_dir: Path, p: str, r: dict, titles: dict, slugs: dict, backlinks: list[str],
                shown: list[str], conf: dict, site: _Site, vslug: str, h: str, lang: str, today: str) -> None:
    props, slug = r["props"], slugs[p]
    url = site.absolute(site.note(vslug, slug, 1))
    credit = _author(props, h)
    source = props.get("source") if isinstance(props.get("source"), str) else ""
    source = source if source.startswith(("http://", "https://")) else ""
    # A republished copy points engines and savers at its original
    # (ADR-0143 point 10), so the copy cannot outrank the author.
    canonical = source if credit and source else url
    head = _head(f"{titles[p]} · {conf['title']} · @{h}", _desc(r["body"]), site, 1, canonical=canonical,
                 md=f"{slug}.md", lang=lang, kind="article")
    crumb = ('<nav class="crumb" aria-label="Breadcrumb">'
             + (f'<a href="{_esc(site.catalog())}">Pages</a> / ' if site.base else "")
             + f'<a href="{_esc(site.page(1))}">@{_esc(h)}</a> / '
             f'<a href="{_esc(site.vault(vslug, 1))}">{_esc(conf["title"])}</a></nav>')
    banner = ""
    if credit:
        saved = props.get("saved") or ""
        orig = f' <a href="{_esc(source)}">Read the original</a>' if source else ""
        banner = (f'<p class="banner">Saved from <b>@{_esc(credit)}</b>'
                  f'{" on " + _esc(saved) if saved else ""}. This is a copy, and @{_esc(credit)} '
                  f'may have changed the original since.{orig}</p>')
    date = props.get("date") or today
    byline = f"@{_esc(credit)} · shared by @{_esc(h)}" if credit else f"@{_esc(h)} · {_esc(date)}"
    tags = _tags(props)
    rep = _report(url or slug, site)
    if site.base:
        target = source if credit and source else url
        actions = (_save_form(target, f"Save @{credit}'s original to Silica" if credit and source
                              else "Save to Silica", "save-note") + f'<div class="row">{rep}</div>')
    else:
        actions = f'<div class="row">{rep}</div>' if rep else ""
    linked = "".join(f'<li><a href="{_esc(site.note(vslug, slugs[b], 1))}">{_esc(titles[b])}</a></li>'
                     for b in sorted(backlinks, key=lambda x: titles[x].lower()))
    more = [q for q in shown if q != p][:3]
    more_html = "".join(f'<li><a href="{_esc(site.note(vslug, slugs[q], 1))}">{_esc(titles[q])}</a></li>'
                        for q in more)
    side = ""
    if linked:
        side += f'<div><h2>Linked from</h2><ul>{linked}</ul></div>'
    if more_html:
        side += (f'<div><h2>More from {_esc(conf["title"])}</h2><ul>{more_html}'
                 f'<li><a href="{_esc(site.vault(vslug, 1))}">All {_fmt(len(shown))} notes</a></li></ul></div>')
    page = (head + "<body>\n" + top(site.catalog(), _app(), page=site.page(1), h=h)
            + f'<main class="sheet"{_room(site, vslug, slug)}><div class="art-grid"><article class="art">'
            + crumb + banner
            + f'<p class="meta">{byline}</p><h1>{_esc(titles[p])}</h1>'
            + (f'<p class="meta">From {_esc(r["credit"])}</p>' if r["credit"] else "")
            + (f'<p class="tags">{" · ".join(_esc(t) for t in tags)}</p>' if tags else "")
            + f'<div class="body">{r["html"]}</div>{actions}</article>'
            + (f'<aside class="side">{side}</aside>' if side else "") + "</div></main>\n"
            + foot(f"A note @{_esc(h)} chose to publish.", _app()))
    (build_dir / f"{slug}.html").write_text(page, encoding="utf-8")


def _vault_page(m: dict, c: dict, site: _Site, h: str, lang: str) -> str:
    """A vault's own page (ADR-0151 point 4) as a reader without the script
    has it: the cover, the facts, Save by areas, Report and every note by area.
    With the script the same address opens as the room (`_room`)."""
    count = m["count"]
    vslug = m["slug"]
    url = m["url"] or "index.html"
    unl = len(m["unlinked"])
    caption = (f'<span class="num">{_fmt(count["drawn"])}</span> of <span class="num">{_fmt(count["notes"])}</span> '
               f'notes drawn · <span class="num">{_fmt(count["links"])}</span> links'
               + (f' · the <span class="num">{_fmt(unl)}</span> notes with no link are in the wiki but not in '
                  f'the picture' if unl else ""))
    facts = [("Notes", f'<span class="num">{_fmt(count["notes"])}</span>'),
             ("Links", f'<span class="num">{_fmt(count["links"])}</span>'),
             ("Areas", f'<span class="num">{_fmt(count["areas"])}</span>'),
             ("Published", _esc(_day(m["published"]))),
             ("License", "Not stated by the author" if m["license"] == "not stated" else _esc(m["license"]))]
    dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts)
    rows = [(i, name, size) for i, (name, size) in enumerate(m["areas"])]
    if unl:
        rows.append((-1, "In no area: notes with no link", unl))
    owner = ' form="save"' if site.base else " disabled"
    areas = "".join(
        f'<li data-area="{i}"><label><input type="checkbox" name="areas" value="{i}"{owner} checked>'
        f'<span class="nm">{_esc(name)}</span><span class="ct num">{_fmt(size)}</span></label></li>'
        for i, name, size in rows)
    by_area: dict[int, list[tuple[str, str]]] = {}
    for row in m["n"]:
        by_area.setdefault(row[2], []).append((row[4], row[5]))
    for title, slug in m["unlinked"]:
        by_area.setdefault(-1, []).append((title, slug))
    notes = "".join(
        f'<details><summary>{_esc(name)}<span class="num">{_fmt(size)}</span></summary><ul>'
        + "".join(f'<li><a href="{_esc(site.note(vslug, s, 1))}">{_esc(t)}</a></li>'
                  for t, s in sorted(by_area.get(i, []), key=lambda x: x[0].lower()))
        + "</ul></details>" for i, name, size in rows)
    save = (_save_form(url, f"Save {_plural(count['notes'], 'note')} to Silica") if site.base else
            '<p class="dim">Saving needs the wiki on a Silica host.</p>')
    crumb = ('<nav class="crumb" aria-label="Breadcrumb">'
             + (f'<a href="{_esc(site.catalog())}">Pages</a> / ' if site.base else "")
             + f'<a href="{_esc(site.page(1))}">@{_esc(h)}</a> / {_esc(m["title"])}</nav>')
    head = _head(f"{m['title']} · @{h}", m["line"] or f"{_plural(count['notes'], 'note')} by @{h}", site, 1,
                 canonical=m["url"], lang=lang)
    return (head + "<body>\n" + top(site.catalog(), _app(), page=site.page(1), h=h)
            + f'<main class="sheet"{_room(site, vslug)}><section class="wrap vp grid" aria-labelledby="vp-h">'
            + crumb
            + f'<figure class="vp-cover"><div class="well">{_svg(c)}</div><figcaption>{caption}</figcaption>'
            f'</figure>'
            + f'<div class="vp-side"><h1 id="vp-h">{_esc(m["title"])}</h1>'
            f'<p class="vp-by">by <a href="{_esc(site.page(1))}">@{_esc(h)}</a></p>'
            + (f'<p class="vp-line">{_esc(m["line"])}</p>' if m["line"] else "")
            + f'<dl class="facts">{dl}</dl>{save}'
            f'<div class="row"><a class="btn-o" href="#notes">Read the notes</a>{_report(url, site)}</div></div>'
            + f'<div class="vp-areas"><div class="vp-areas-head"><h2>Areas</h2>'
            f'<p>Choose what to save.</p></div>'
            f'<ul class="areas">{areas}</ul></div>'
            + f'<div class="vp-notes" id="notes"><h2>The notes</h2>{notes}</div>'
            + "</section></main>\n" + foot(f"A wiki @{_esc(h)} chose to publish.", _app()))


def card(e: dict, href: str, graph: str, who: bool = True) -> str:
    """One vault as a card, on a Page and in Pages: the cover the script
    prints from `graph`, the title, the line, the handle and the counts."""
    meta = ((f'<span class="who">@{_esc(e["handle"])}</span>' if who else "")
            + f'<span><span class="num">{_fmt(e["count"]["notes"])}</span> notes</span>'
            f'<span data-in><span class="num">{_fmt(e["count"]["areas"])}</span> areas</span>')
    return (f'<a class="vcard" href="{_esc(href)}" data-graph="{_esc(graph)}" data-title="{_esc(e["title"])}"'
            f' data-line="{_esc(e["line"])}" data-who="{_esc(e["handle"])}" data-notes="{int(e["count"]["notes"])}"'
            f' data-date="{_esc(e["published"])}" data-areas="{_esc(json.dumps(e["areas"], ensure_ascii=False))}">'
            f'<div class="well"><canvas role="img" aria-label="{_esc(e["title"])} as a graph of its notes"></canvas>'
            f'</div><div><h2>{_esc(e["title"])}</h2>'
            + (f'<p class="vline">{_esc(e["line"])}</p>' if e["line"] else "")
            + f'<p class="vmeta">{meta}</p></div></a>')


def write_page(vault: Path | None = None) -> None:
    """The Page, `/@handle/` (ADR-0151 point 3): the account's Public vaults
    from their published manifests, and `vaults.json`, which a vault's page
    reads for "Also by". One Public vault is one map to open, so `data-one`
    sends the script there (amendment of 2026-11-06, item 3). With no Public
    vault the Page goes, so its address answers that there is no page; a
    laptop's copy of the sheet goes with it."""
    vault = vault or publish._vault()
    h = publish.handle(vault)
    if not h:
        return
    root = publish.site_root(h)
    live = root / "live"
    public = {v["slug"] for v in publish.vaults(vault) if v["visibility"] == "public"}
    entries = []
    for slug in sorted(public):
        try:
            entries.append(json.loads((live / slug / "vault.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    if live.is_dir():
        for stale in [p for p in live.iterdir() if p.is_dir() and p.name not in public and p.name != "_s"]:
            # a vault Off or Private whose folder stayed, after a crash between two renames
            shutil.rmtree(stale)
    if not entries:
        for name in ("index.html", "vaults.json"):
            (live / name).unlink(missing_ok=True)
        shutil.rmtree(live / "_s", ignore_errors=True)
        return
    site = _Site(h, publish.page_url(vault))
    entries.sort(key=lambda m: m["published"], reverse=True)
    lang = _LANGS.get(_language().strip().lower(), "")
    notes = sum(m["count"]["notes"] for m in entries)
    cards = "".join(card(m, site.vault(m["slug"], 0), f"{site.root}/{m['slug']}/vault.json" if site.base
                         else f"{m['slug']}/vault.json", who=False) for m in entries)
    head = _head(f"@{h} · Silica", f"{_plural(len(entries), 'wiki')} by @{h}", site, 0,
                 canonical=site.absolute(site.page(0)), lang=lang)
    page = (head + "<body>\n" + top(site.catalog(), _app(), current="page", page=site.page(0), h=h)
            + '<main class="sheet"><section class="wrap cat" aria-labelledby="pg-h"'
            + (f' data-one="{_esc(site.vault(entries[0]["slug"], 0))}"' if len(entries) == 1 else "") + ">"
            + f'<div class="cat-head grid"><h1 id="pg-h">@{_esc(h)}</h1>'
            f'<p>{_plural(len(entries), "vault")} · {_plural(notes, "note")}. Read one here, or save a copy into '
            f'your own vault: every note in the copy keeps its author\'s name.</p></div>'
            + f'<div class="cat-grid">{cards}</div></section></main>\n'
            + foot(f"Wikis @{_esc(h)} chose to publish.", _app()))
    live.mkdir(parents=True, exist_ok=True)
    _atomic(live / "index.html", page)
    listing = [{"slug": m["slug"], "title": m["title"], "line": m["line"], "count": m["count"],
                "published": m["published"], "areas": m["areas"], "handle": h} for m in entries]
    _atomic(live / "vaults.json", json.dumps(listing, ensure_ascii=False))
    if not site.base:
        _copy_static(live / "_s")
    _readable(live, root, False)


def set_listed(slug: str, listed: bool, vault: Path | None = None) -> None:
    """Ask to list reaches the published manifest at once, since the host reads
    the request there; the rest of the vault waits for Publish."""
    vault = vault or publish._vault()
    h = publish.handle(vault)
    path = publish.site_root(h) / "live" / slug / "vault.json" if h else None
    if path is None or not path.is_file():
        return
    m = json.loads(path.read_text(encoding="utf-8"))
    m["listed"] = bool(listed) and not m.get("blocked")
    _atomic(path, json.dumps(m, ensure_ascii=False, separators=(",", ":")))


def _language() -> str:
    from silica.kernel.vault_manifest import get_active_manifest

    return get_active_manifest().conventions.language or ""


def _atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o644)
    os.replace(tmp, path)


def _readable(build_dir: Path, root: Path, private: bool) -> None:
    """Every published file readable by others, whatever the umask: on the
    server the account's unit writes with UMask=0077 and the web server reads
    as another user (silica-hosted, ADR-0143 point 6). A Private build is
    read by the relay, as root, and by no one else."""
    root.chmod(0o755)
    if private:
        return
    build_dir.chmod(0o755)
    for p in build_dir.rglob("*"):
        p.chmod(0o755 if p.is_dir() else 0o644)
    live = root / "live"
    if live.is_dir():
        live.chmod(0o755)


def last_published(h: str) -> dict:
    """{"vaults": {slug: {"published_at", "notes"}}} of the last builds, {} before the first."""
    try:
        state = json.loads((publish.site_root(h) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) and isinstance(state.get("vaults"), dict) else {}


def _drop_title_heading(body: str, title: str) -> str:
    """A body that opens on `# <its title>` loses that line: the page heads
    itself with the title already, and a note read twice in a row is noise."""
    m = re.match(r"\s*#[ \t]+(.+?)[ \t#]*\r?\n", body)
    return body[m.end():] if m and m.group(1).strip().lower() == title.strip().lower() else body


def _unquote(s: str) -> str:
    from urllib.parse import unquote

    return unquote(html.unescape(s))


def static_files() -> dict[str, bytes]:
    """{name: bytes} of the sheet, the script and the fonts, as /_s/ serves
    them; the host copies them from the release (ADR-0151 point 7)."""
    static = importlib.resources.files("silica.ui.web") / "static"
    out = {}
    for name in _STATIC:
        res = static.joinpath(*name.split("/"))
        if res.is_file():
            out[name.rsplit("/", 1)[-1]] = res.read_bytes()
    return out


def _copy_static(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in static_files().items():
        (dest / name).write_bytes(data)
