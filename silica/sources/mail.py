# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""MailAdapter, ADR-0090 Part A: mail enters as one note per thread, from a
file the user hands over (`.eml`, `.mbox`, a Google Takeout `.mbox`).

Standard library only (`email`, `mailbox`, `html.parser`): no credential, no
network, no new dependency. Zero-trust (ADR-0009): a stranger chose every byte
of a message, so `read()` reduces each one to the few fields point 7 lists
before anything can reach a note, and no model reads the mail at ingest (the
terminal lane, ADR-0013).
"""
from __future__ import annotations

import datetime as dt
import email
import hashlib
import mailbox
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from html import escape as html_escape
from html.parser import HTMLParser
from pathlib import Path

from silica.sources.base import GroundedStub, RawItem

_MSGID_RE = re.compile(r"<[^<>\s]+>")
# The two files Part A reads (point 9). Also what the Mail row's picker offers
# and what a chat drop refuses to stage (server.py), from this one tuple.
SUFFIXES = (".eml", ".mbox")
# What one message may hold in memory before it is reduced. A newsletter's HTML
# runs to ~100k chars; past this a message is a payload, not correspondence,
# and a cut inside a hidden element hides the rest (the pre-pass fails closed).
_RAW_CAP = 400_000


def _ids(value) -> list[str]:
    return [_unlinked_id(i) for i in _MSGID_RE.findall(str(value or ""))]


def _unlinked_id(value: str) -> str:
    """An id a stranger chose, as the stamp will hold it: Silica's graph reads
    the frontmatter, so `<[[x]]@host>` or `ADR-0001` in a Message-ID or a
    thread id made an edge from the note (2026-10-12). Applied where the id is
    read, so threading and dedupe compare the same strings the stamp holds."""
    return _ADR_REF_RE.sub(r"ADR \1", re.sub(r"[\[\]]", "", value))


# C0 and C1 controls but tab and newline: NUL, ESC and CSI are terminal and
# renderer instructions, never text a reader was meant to see, and a stranger
# chose them (found end to end 2026-10-12: both reached the note).
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _content_hash(m: EmailMessage) -> str:
    """A message's identity when it carries no Message-ID: sender, date,
    subject and body, so a re-import finds it again (a counter or a random id
    would not) and two header-less messages stay two (sender, date and subject
    alone gave them one key, 2026-10-12)."""
    try:
        part = m.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None  # a body the parser cannot walk still has headers to key on
    basis = "\x1f".join([*(_header(m, h) for h in ("From", "Date", "Subject")),
                         _text_of(part) if part is not None else ""])
    return "h:" + hashlib.sha1(basis.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def thread_key(m: EmailMessage) -> str:
    """ADR-0090 point 2: the provider's thread id when the file carries one,
    else the References root, else In-Reply-To, else the message's own id,
    else `_content_hash`."""
    thrid = _unlinked_id(_header(m, "X-GM-THRID"))
    if thrid:
        return thrid
    for header in ("References", "In-Reply-To", "Message-ID"):
        found = _ids(m.get(header))
        if found:
            return found[0]
    return _content_hash(m)


@dataclass(frozen=True)
class Attachment:
    name: str
    ctype: str
    size: int


@dataclass(frozen=True)
class Mail:
    """One message, held as the few fields ADR-0090 point 7 lets reach a note
    plus the headers the thread and the participation filter need."""

    msg_id: str
    # `msg_id`, or `_content_hash` when there is none: what dedupe compares, so
    # a reply with no Message-ID still counts as a message the note lacks.
    ident: str
    in_reply_to: str
    key: str
    sender: str                 # display name, "" when the From has none
    address: str                # lowercased
    date: dt.datetime | None    # timezone-aware; None when the Date is unreadable
    subject: str
    labels: frozenset[str]      # Takeout's X-Gmail-Labels, the first header only
    delivered_to: tuple[str, ...]
    to: tuple[str, ...]
    body: str                   # the text/plain part, or the text/html one when there is none
    html: bool
    attachments: tuple[Attachment, ...]


@dataclass(frozen=True)
class Thread:
    key: str
    mails: tuple[Mail, ...]     # date order


def _raw_header(m: EmailMessage, name: str) -> str:
    """The first `name` header as its own bytes said it, RFC 2047 words decoded.

    The modern policy decodes raw 8-bit bytes as ASCII, so `Subject: Caf\\xe9`
    came out "Caf\\ufffd" and reached the filename (2026-10-12). UTF-8 first,
    since that is what a client writes today, then Windows-1252, the charset a
    client that writes no charset declaration nearly always meant."""
    raw = next((v for k, v in m.raw_items() if k.lower() == name.lower()), None)
    if raw is None:
        return ""
    data = str(raw).encode("utf-8", "surrogateescape")
    for charset in ("utf-8", "cp1252", "latin-1"):
        try:
            text = data.decode(charset)
            break
        except UnicodeDecodeError:
            continue
    try:
        return str(make_header(decode_header(text)))
    except (LookupError, UnicodeError, ValueError):
        # An encoded word naming a charset Python lacks, or a broken one: the
        # undecoded text is still the header, and the policy fared no better.
        return text


def _header(m: EmailMessage, name: str) -> str:
    try:
        value = str(m.get(name) or "")
    except Exception:
        # A header the modern policy cannot parse (a stranger wrote it) is
        # read as absent: one bad header must not drop the whole message.
        value = ""
    if "�" in value:
        value = _raw_header(m, name)
    return " ".join(_CONTROL_RE.sub("", value).split())


# `Name <addr>` at the end of a From, for when the address parser gives up.
_ANGLE_ADDR_RE = re.compile(r"<\s*([^<>\s]+@[^<>\s]+?)\s*>\s*$")
_BARE_ADDR_RE = re.compile(r"[^\s<>\"'\[\]()]+@[^\s<>\"'\[\]()]+")


def _from(m: EmailMessage) -> tuple[str, str]:
    """(display name, address) of the first From.

    The address parser reads an unquoted `[x]` as a domain literal and gives
    up on the whole header ("<>"), so a stranger who writes markup in a
    display name went in as "unknown sender" with an empty `mail_from`
    (2026-10-12). Past that failure the address is the `<...>` that ends the
    raw header, as every client writes it."""
    sender, address = (getaddresses([_header(m, "From")]) or [("", "")])[0]
    if "@" in address:
        return sender, address
    raw = " ".join(_CONTROL_RE.sub("", _raw_header(m, "From")).split())
    angle = _ANGLE_ADDR_RE.search(raw)
    if angle:
        return raw[:angle.start()].strip().strip('"').strip(), angle.group(1)
    bare = _BARE_ADDR_RE.search(raw)
    return ("", bare.group(0)) if bare else (raw, "")


def _all_addresses(m: EmailMessage, name: str) -> tuple[str, ...]:
    try:
        values = [str(v) for v in m.get_all(name) or ()]
    except Exception:
        return ()  # an unparseable address header names no one, as above
    return tuple(a.lower() for _, a in getaddresses(values) if "@" in a)


def _date(m: EmailMessage) -> dt.datetime | None:
    raw = _header(m, "Date")
    try:
        when = parsedate_to_datetime(raw) if raw else None
    except (TypeError, ValueError, IndexError):
        return None  # an unreadable Date sorts last instead of failing the file
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when


def _text_of(part) -> str:
    try:
        text = part.get_content()
    except Exception:
        # Unknown charset or a broken transfer encoding: the bytes decoded
        # leniently are still the message, and a replacement character is
        # better than a thread with a hole in it.
        payload = part.get_payload(decode=True) or b""
        text = payload.decode("utf-8", errors="replace")
    return str(text)[:_RAW_CAP]


def _labels(m: EmailMessage) -> frozenset[str]:
    # The first header only: Takeout writes its own at the top, and a second
    # one further down is a header the sender wrote.
    raw = _header(m, "X-Gmail-Labels")
    return frozenset(x.strip().strip('"').strip() for x in raw.split(",") if x.strip())


def _attachments(m: EmailMessage, body) -> tuple[Attachment, ...]:
    out: list[Attachment] = []
    for part in m.walk():
        if part.is_multipart() or part is body:
            continue
        name = part.get_filename()
        if part.get_content_disposition() != "attachment" and not name:
            continue
        payload = part.get_payload(decode=True) or b""
        out.append(Attachment(name=" ".join(str(name or "unnamed").split())[:120],
                              ctype=part.get_content_type(), size=len(payload)))
    return tuple(out)


def _mail(m: EmailMessage) -> Mail:
    body = m.get_body(preferencelist=("plain",))
    html = body is None
    if html:
        body = m.get_body(preferencelist=("html",))
    # The first address only: a From naming two people is a stranger's
    # header, and the first is the one a client shows.
    sender, address = _from(m)
    in_reply_to = _ids(_header(m, "In-Reply-To"))
    msg_id = (_ids(_header(m, "Message-ID")) or [""])[0]
    return Mail(
        msg_id=msg_id,
        ident=msg_id or _content_hash(m),
        in_reply_to=in_reply_to[0] if in_reply_to else "",
        key=thread_key(m),
        sender=" ".join(sender.split()),
        address=address.lower(),
        date=_date(m),
        subject=_header(m, "Subject"),
        labels=_labels(m),
        delivered_to=_all_addresses(m, "Delivered-To"),
        to=_all_addresses(m, "To"),
        body=_text_of(body) if body is not None else "",
        html=html and body is not None,
        attachments=_attachments(m, body),
    )


def parse(path: str | Path) -> list[Mail]:
    """Every message in an `.eml` or `.mbox`, reduced.

    The mbox is read through `mailbox.mbox` and never locked, flushed or
    added to: the file is the user's own export, and Silica only reads it.

    A file that is not mail is refused by name: `mailbox.mbox` reads any bytes
    as an mbox of no messages and the email parser any bytes as a message of
    no headers, so a `.txt` or a corrupt export read as "0 threads" (2026-10-12),
    which says the file is empty when it says nothing at all."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUFFIXES:
        raise ValueError(f"{path.name} is not a mail file: /mail import reads an .mbox or an .eml")
    if suffix == ".eml":
        with path.open("rb") as f:
            m = email.message_from_binary_file(f, policy=policy.default)
        if not any(h in m for h in ("From", "Date", "Subject", "Message-ID")):
            raise ValueError(f"{path.name} is not a mail file: it has no From, Date, Subject "
                             "or Message-ID header")
        return [_mail(m)]
    with path.open("rb") as f:
        head = f.read(5)
    if head and head != b"From ":
        raise ValueError(f'{path.name} is not a mail file: an mbox starts with a "From " line')
    box = mailbox.mbox(str(path), create=False)
    try:
        return [_mail(email.message_from_bytes(box.get_bytes(k), policy=policy.default))
                for k in box.iterkeys()]
    finally:
        box.close()


_LAST = dt.datetime.max.replace(tzinfo=dt.timezone.utc)


def _when(m: Mail) -> dt.datetime:
    return m.date or _LAST


def threads_of(mails: list[Mail]) -> list[Thread]:
    """Messages grouped by thread key, each thread in date order, threads by
    their first message: the order "the first 200" counts in, stable across
    re-imports so a run after a capped one picks up where it stopped."""
    groups: dict[str, list[Mail]] = {}
    for m in mails:
        groups.setdefault(m.key, []).append(m)
    threads = [Thread(key, tuple(sorted(ms, key=_when))) for key, ms in groups.items()]
    return sorted(threads, key=lambda t: _when(t.mails[0]))


MAIL_DIR = "mail"
# `#`, `^`, `[` and `]` on top of what a filesystem refuses: a filename that
# holds them cannot be a wikilink target, and a note that links to its mail
# through `## Sources` names the file in one.
_UNLINKABLE_RE = re.compile(r"[#^\[\]]")
# A subject is a stranger's text and has no length limit; the path carries the
# date and the key, so 80 chars of it identify a thread to a reader and keep
# the name far under the 255-byte filename limit.
_SUBJECT_CHARS = 80


def _subject_for_path(subject: str) -> str:
    from silica.kernel.text.sanitize import _ILLEGAL_FILENAME_CHARS_RE

    clean = _UNLINKABLE_RE.sub("", _ILLEGAL_FILENAME_CHARS_RE.sub("", _unlinked_id(subject)))
    clean = " ".join(clean.split())[:_SUBJECT_CHARS].strip(" .")
    return clean or "no subject"


def key6(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:6]


def note_path(thread: Thread) -> str:
    """ADR-0090 point 3: `<sources>/mail/<date> <subject> <key6>.md`, from the
    thread's first message. `<key6>` is what keeps two "Re: meeting" threads
    of one day apart: without it the second would restage over the first."""
    from silica.kernel.recall.paths import SOURCES_DIR
    from silica.kernel.vault_manifest import in_write_dir

    first = thread.mails[0]
    date = first.date.date().isoformat() if first.date else "undated"
    name = f"{date} {_subject_for_path(first.subject)} {key6(thread.key)}.md"
    return f"{in_write_dir(SOURCES_DIR)}/{MAIL_DIR}/{name}"


# --- point 7: what reaches the note ---------------------------------------------

_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                   "meta", "param", "source", "track", "wbr"})
_ZERO_PROPS = ("opacity", "max-height")
_NUMBER_RE = re.compile(r"[+-]?(\d*\.?\d+)")


# A comment that never closes runs to the end of the style, as in a browser.
_CSS_COMMENT_RE = re.compile(r"/\*.*?(?:\*/|$)", re.DOTALL)
_CSS_ESCAPE_RE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\r\n\f]?|(.))", re.DOTALL)
_IMPORTANT_RE = re.compile(r"!\s*important\b")


def _css_unescape(m: re.Match) -> str:
    if m.group(1) is None:
        return m.group(2)
    cp = int(m.group(1), 16)
    return chr(cp) if 0 < cp <= 0x10FFFF and not 0xD800 <= cp <= 0xDFFF else "�"


def _css(style: str) -> str:
    """An inline style as a browser reads it: comments gone, escapes decoded.

    `display:/**/none` and `disp\\61 lay:none` are `display:none` to a client;
    matched as written they hid nothing from this pass (2026-10-12). A comment
    is deleted rather than read as the token break it is, so `disp/**/lay`
    hides too: over-hiding loses text, which this pass can afford."""
    return _CSS_ESCAPE_RE.sub(_css_unescape, _CSS_COMMENT_RE.sub("", style)).lower()


# CSS px per unit, at the 16px default font size for em, rem and %.
_PX_PER = {"px": 1.0, "pt": 4 / 3, "pc": 16.0, "in": 96.0, "cm": 96 / 2.54, "mm": 96 / 25.4,
           "q": 96 / 101.6, "em": 16.0, "rem": 16.0, "%": 0.16}
_SIZE_RE = re.compile(r"([+-]?(?:\d+\.?\d*|\.\d+))(px|pt|pc|in|cm|mm|q|em|rem|%)?$")
# A font this small is a line no reader sees: the tester's 1px near-white text
# (2026-10-12). The cut sits at 1px and not higher because 2px glyphs are still
# marks on the screen, and losing a visible line is this pass's cheaper failure
# only when the line was meant to be unseen.
_TINY_FONT_PX = 1.0


def _font_px(token: str) -> float | None:
    """A font size in px, None for a keyword or a unitless number other than 0
    (in the `font` shorthand that is the weight, `700`, not the size)."""
    m = _SIZE_RE.match(token.split("/", 1)[0])
    if not m:
        return None
    n = float(m.group(1))
    return n * _PX_PER[m.group(2)] if m.group(2) else (0.0 if n == 0 else None)


def _hides(attrs: list[tuple[str, str | None]]) -> bool:
    """Whether an element's own attributes hide it from the reader: the
    `hidden` attribute, or an inline style a mail client renders as nothing.

    Every `style` attribute counts, not the last: a browser keeps the first of
    two, and `dict(attrs)` kept the second, so `style="display:none"
    style="color:red"` showed its text here and hid it in the client.

    Point 7's list is a floor: beyond it a zero height or width that clips its
    overflow, Outlook's `mso-hide:all` and a font of 1px or less hid text the
    tester found in notes (2026-10-12). A zero box that does not clip shows its
    text, overflowing, so it is not hidden."""
    decls: list[tuple[str, str]] = []
    for name, raw in attrs:
        if name == "hidden":
            return True
        if name == "style":
            for decl in _css(raw or "").split(";"):
                prop, _, value = decl.partition(":")
                decls.append((prop.strip(), _IMPORTANT_RE.sub("", value).strip()))
    clips = any(p in ("overflow", "overflow-x", "overflow-y") and {"hidden", "clip"} & set(v.split())
                for p, v in decls)
    for prop, value in decls:
        if (prop, value) in (("display", "none"), ("visibility", "hidden"), ("mso-hide", "all")):
            return True
        if prop == "font-size":
            px = _font_px(value)
            if px is not None and px <= _TINY_FONT_PX:
                return True
        if prop == "font":
            px = next((s for s in map(_font_px, value.split()) if s is not None), None)
            if px is not None and px <= _TINY_FONT_PX:
                return True
        if prop in _ZERO_PROPS or (clips and prop in ("height", "width")):
            num = _NUMBER_RE.match(value)
            if num and float(num.group(1)) == 0:
                return True
    return False


class _VisiblePass(HTMLParser):
    """Re-emit the HTML a client would show, for `web_fetch._extract_text`.

    Drops every element that hides itself (`_hides`), every image (its URL is
    never requested, and its `alt` is text a stranger wrote that the reader
    may never see), `<head>` with its `<title>`, and with `cut_quotes` the
    reply's quoted copy: Gmail's `gmail_quote` element, and everything from
    Outlook's reply header to the end, since Outlook puts the original below
    it unquoted.

    An element that never closes hides the rest of the message: losing text a
    client would have shown is the failure this pass can afford, showing text
    a client hid is not.

    ponytail: inline styles only. Text hidden by a class in a `<style>` block,
    or by a colour equal to the background, passes through; the plain part is
    preferred and almost every client sends one. Upgrade to a computed-style
    render the day one gate-1 fixture of that kind lands in a note
    (ADR-0090 point 7).
    """

    def __init__(self, cut_quotes: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.cut_quotes = cut_quotes
        self.out: list[str] = []
        self._hide_tag = ""
        self._hide = 0
        self._cut = False

    def _quote(self, attrs: dict) -> str:
        if not self.cut_quotes:
            return ""
        if "gmail_quote" in str(attrs.get("class") or ""):
            return "element"
        if str(attrs.get("id") or "") in ("divRplyFwdMsg", "appendonsend"):
            return "rest"
        return ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if self._cut:
            return
        if self._hide:
            if tag == self._hide_tag and tag not in _VOID:
                self._hide += 1
            return
        a = dict(attrs)
        quote = self._quote(a)
        if quote == "rest":
            self._cut = True
            return
        if tag in _VOID:
            if tag != "img" and not _hides(attrs):
                self.out.append(self.get_starttag_text() or "")
            return
        if tag == "head" or quote or _hides(attrs):
            self._hide_tag, self._hide = tag, 1
            return
        self.out.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag: str) -> None:
        if self._cut or tag in _VOID:
            return
        if self._hide:
            if tag == self._hide_tag:
                self._hide -= 1
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not (self._cut or self._hide):
            # Escaped again: a visible "&lt;img&gt;" must stay text for the
            # second parser, not become the tag it spells.
            self.out.append(html_escape(data, quote=False))


def _visible_html(html: str, cut_quotes: bool) -> str:
    p = _VisiblePass(cut_quotes)
    p.feed(html)
    p.close()
    return "".join(p.out)


# Per message and per note, in chars. A reply runs to a few thousand chars; a
# message past 20k is a forwarded document or a newsletter, and a thread past
# 100k would outgrow every window recall serves a note in.
MESSAGE_CAP = 20_000
NOTE_CAP = 100_000
_RULE_RE = re.compile(r"^\s*(?:_{10,}|-{2,}\s*Original Message\s*-{2,})\s*$", re.IGNORECASE)


def _outlook_header_at(lines: list[str], i: int) -> bool:
    """Outlook's reply header: a rule then `From:`, or `From:` then `Sent:`
    with no rule. The original follows it unquoted, so the cut runs to the end."""
    nxt = lines[i + 1].lstrip() if i + 1 < len(lines) else ""
    line = lines[i].lstrip()
    return ((bool(_RULE_RE.match(lines[i])) and nxt.startswith("From:"))
            or (line.startswith("From:") and nxt.startswith(("Sent:", "Date:"))))


# The line a client writes above a quote: "On <date>, <name> wrote:", in the
# languages Gmail and Outlook ship it in for the users this serves. The opener is
# case-sensitive and must start the line, so "Here is what you wrote:" stays.
_ATTRIBUTION_OPENER_RE = re.compile(r"^\s*(?:On|Il|Am|Le|El|Em|Op|Den|Dnia|W dniu)\b")
_ATTRIBUTION_VERB_RE = re.compile(
    r"\b(?:wrote|writes|schrieb|a écrit|ha scritto|escribió|escreveu|schreef|skrev|napisał)\b",
    re.IGNORECASE)


def _attribution_above(lines: list[str], i: int) -> range:
    """The attribution of the quote that starts at line `i`: one line, or two
    when the client wrapped it ("On ... <" / "addr> wrote:"). Cut with the
    quote, since it introduces text the note no longer holds (2026-10-12)."""
    j = i - 1
    while j >= 0 and not lines[j].strip():
        j -= 1
    if j < 0 or not lines[j].rstrip().endswith(":"):
        return range(0)
    for start in (j, j - 1):
        if start < 0 or not lines[start].strip():
            break
        if (_ATTRIBUTION_OPENER_RE.match(lines[start])
                and _ATTRIBUTION_VERB_RE.search(" ".join(lines[start:j + 1]))):
            return range(start, j + 1)
    return range(0)


def _cut_plain_quotes(text: str) -> str:
    lines = text.split("\n")
    end = next((i for i in range(len(lines)) if _outlook_header_at(lines, i)), len(lines))
    drop = {i for i in range(end) if lines[i].startswith(">")}
    for i in sorted(drop):
        if i - 1 not in drop:
            drop.update(_attribution_above(lines, i))
    return "\n".join(ln for i, ln in enumerate(lines[:end]) if i not in drop)


def _cut_signature(text: str, separator: str) -> str:
    """RFC 3676 §4.3: everything after the `-- ` line is the signature. Text
    from HTML arrives with its trailing space collapsed, so it looks for `--`."""
    lines = text.split("\n")
    end = next((i for i, ln in enumerate(lines) if ln == separator), len(lines))
    return "\n".join(lines[:end])


# --- point 7 and gate 1: no image reference and no live markup reach the note -------
# The note is markdown that Obsidian and the GUI render, and markdown passes
# raw HTML through. A plain part went in verbatim and the HTML pass's own text
# was not filtered, so a stranger's `![](url)`, `<img src=url>` or an escaped
# `&lt;img&gt;` became an image the drawer fetched on open (three tracker hits,
# 2026-10-12). The fix is at the source, for every part type: an image
# reference is dropped whole, and every `<` that could open a tag is written as
# `&lt;`, which both renderers show as the `<` it was. The GUI's own rendering of
# the user's notes is untouched: this is the mail adapter's job (gate 1).

_IMAGE_EXT = r"(?:png|gif|jpe?g|jfif|webp|avif|apng|svg|bmp|ico|tiff?|heic|heif)"
_URL_CHAR = r"[^\s<>\"'`()\[\]{}]"
_IMAGE_REF_RE = re.compile(
    # Obsidian's embed, then a markdown image (inline or by reference, one level
    # of brackets in the alt and of parentheses in the URL), then an HTML image
    # tag of any case and any line breaks.
    r"!\[\[[^\[\]\n]*\]\]"
    r"|!\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\](?:\([^()\n]*(?:\([^()\n]*\)[^()\n]*)*\)|\[[^\[\]\n]*\])?"
    r"|<\s*(?:img|image)\b[^<>]*>"
    # A URL that names an image, bare or inside markup left as text: gate 1
    # counts image URLs in the note, not only the ones a renderer would fetch.
    rf"|(?:\b(?:https?|ftp):|(?<![\w/:]))//{_URL_CHAR}+?\.{_IMAGE_EXT}(?![\w./-])"
    rf"(?:[?#]{_URL_CHAR}*)?"
    rf"|\bwww\.{_URL_CHAR}+?\.{_IMAGE_EXT}(?![\w./-])(?:[?#]{_URL_CHAR}*)?"
    r"|\bdata:image/[^\s<>\"'()]*",
    re.IGNORECASE)
# What CommonMark and Obsidian read as the start of raw HTML: a tag, a closing
# tag, a comment, a declaration, a processing instruction, an autolink.
_TAG_OPEN_RE = re.compile(r"<(?=[A-Za-z/!?])")


def _no_images(text: str) -> str:
    # A `![` left over (an alt split over lines) is escaped, so it can open no image.
    return _IMAGE_REF_RE.sub("", _CONTROL_RE.sub("", text)).replace("![", "!\\[")


# --- and no link or tag: a stranger chooses no edge of the owner's graph ------------
# A body's `[[Secret]]` made a node of the Explore graph and an edge from the
# mail note, and `#urgent` a tag (2026-10-12). Each form is reduced to its text,
# as `_unlinked` reduces a heading. Silica's graph reads links through a
# markdown parse (kernel/link/ast.py), so a backslash escape or an entity that
# decodes to `[` is a link there too: both are written so they stay as typed.
_ESCAPE_RE = re.compile(r"\\(?=[!-/:-@\[-`{-~])")
_ENTITY_RE = re.compile(r"&(?=#\d+;|#[xX][0-9a-fA-F]+;|[A-Za-z][A-Za-z0-9]*;)")
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]*)\]\]")
# A markdown link to anything but the web: a vault path, and so an edge.
_LOCAL_LINK_RE = re.compile(
    r"\[([^\[\]\n]*)\]\((?!\s*<?(?:https?://|mailto:))[^()\n]*(?:\([^()\n]*\)[^()\n]*)*\)")
# A reference definition, wherever it sits: unbracketed, `[x][r]` and `[x]`
# name nothing. Footnote definitions are among them; Silica's parser reads one
# as a link too.
_REF_DEF_RE = re.compile(r"\[([^\[\]\n]+)\]:")
_ADR_REF_RE = re.compile(r"\bADR-(\d{4})\b")  # kernel/link/ast.py's numbered-decision link
# Obsidian's inline tag: `#` at a word's start, with a letter in what follows,
# so "#12" and a URL's "page#anchor" stay as written.
_INLINE_TAG_RE = re.compile(r"(?:^|(?<=[\s(\[{<>\"'*_,;!|]))#(?=[\w/-]*[^\W\d])(?=[\w/-])")


def _no_links(text: str) -> str:
    text = _WIKILINK_RE.sub(r"\1", text)
    text = re.sub(r"\]{2,}", "]", re.sub(r"\[{2,}", "[", text))
    text = _REF_DEF_RE.sub(r"\1:", _LOCAL_LINK_RE.sub(r"\1", text))
    return _INLINE_TAG_RE.sub("", _ADR_REF_RE.sub(r"ADR \1", text))


def _inert(text: str) -> str:
    """A stranger's text as markdown that renders as that text: no image, no
    raw HTML, no link into the vault, no tag.

    `&lt;` also shows inside a code block, where nothing needed escaping: a
    cosmetic cost taken because telling code from prose in text a stranger
    shaped is where a renderer and this pass would disagree."""
    from silica.kernel.link.ast import extract_links

    text = _ENTITY_RE.sub("&amp;", _ESCAPE_RE.sub(r"\\\\", _CONTROL_RE.sub("", text)))
    text = _TAG_OPEN_RE.sub("&lt;", _no_links(_no_images(text)))
    if extract_links(text):
        # The graph's own reader still finds a link the rules above missed: no
        # bracket is left to make one. Losing a web link's brackets is the
        # cheaper failure next to an edge a stranger chose.
        text = re.sub(r"[\[\]]", "", text)
    return text


def _unlinked(text: str) -> str:
    """A header field as a heading or a property: inert, and without the
    characters `_UNLINKABLE_RE` takes from the filename. A subject or a display
    name holding `[[Secret]]` or `#tag` made a link and a graph node, and a
    tag, in the vault (2026-10-12)."""
    return " ".join(_UNLINKABLE_RE.sub("", _inert(text)).split())


def _message_text(m: Mail, cut_quotes: bool) -> str:
    from silica.kernel.text.sanitize import strip_degenerate_runs
    from silica.sources.web_fetch import _extract_text, _truncate

    if m.html:
        text = _cut_signature(_extract_text(_visible_html(m.body, cut_quotes)), "--")
    else:
        text = m.body.replace("\r\n", "\n").replace("\r", "\n")
        if cut_quotes:
            text = _cut_plain_quotes(text)
        text = _cut_signature(text, "-- ")
    text = re.sub(r"\n{3,}", "\n\n", strip_degenerate_runs(_inert(text))).strip()
    return _truncate(text, MESSAGE_CAP)


def _size(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB" if n >= 1_048_576 else f"{max(1, round(n / 1024))} KB"


def _who(m: Mail) -> str:
    if m.sender and m.address:
        who = f"{m.sender} ({m.address})"
    else:
        who = m.sender or m.address
    return _unlinked(who) or "unknown sender"


def render(thread: Thread) -> str:
    """The note's body: the subject, then every message in date order under
    `## <sender>, <date>`. A reply's quoted copy is cut only when the message
    it quotes is in this note, so nothing the note holds once is held twice
    and nothing is lost when the original is not here."""
    from silica.sources.web_fetch import _truncate

    ids = {m.msg_id for m in thread.mails if m.msg_id}
    parts = [f"# {_unlinked(thread.mails[0].subject) or 'no subject'}"]
    for m in thread.mails:
        when = m.date.strftime("%Y-%m-%d %H:%M") if m.date else "undated"
        block = f"## {_who(m)}, {when}\n\n{_message_text(m, m.in_reply_to in ids)}"
        if m.attachments:
            listed = ", ".join(f"{_unlinked(a.name)} ({_unlinked(a.ctype)}, {_size(a.size)})"
                               for a in m.attachments)
            block += f"\n\nAttachments: {listed}"
        parts.append(block)
    return _truncate("\n\n".join(parts), NOTE_CAP) + "\n"


# --- point 1: the adapter --------------------------------------------------------

_TARGET_RE = re.compile(r"^(.+?\.(?:mbox|eml))(?:#(.+))?$", re.IGNORECASE)
# One parsed file, by identity: `/mail import` stages one target per thread,
# and `stage()` reads each, so without this a 3,000-thread mbox would be parsed
# once per thread written.
_PARSED: dict[tuple[str, int, int], list[Thread]] = {}
# Every parse on one thread of its own. glibc gives each live thread its own
# malloc arena and never hands one arena's free memory to another, so parses
# landing on different workers of the GUI's thread pool each kept a whole
# export's worth (measured 2026-10-12 on a 1 GiB export: 310, 566, 822 MB
# after three parses on three live workers; 292 MB flat on one). The lock
# makes two dry runs in flight wait for one parse instead of running two.
_PARSER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mail-parse")
_PARSE_LOCK = threading.Lock()


def split_target(target: str) -> tuple[str, str]:
    """`<file>#<thread-key>` into its two halves; the key is "" for a bare file."""
    m = _TARGET_RE.match((target or "").strip())
    if not m:
        raise ValueError(f"not a mail file: {target}")
    return m.group(1), m.group(2) or ""


def threads_in(path: str | Path) -> list[Thread]:
    path = Path(path).resolve()
    st = path.stat()
    ident = (str(path), st.st_mtime_ns, st.st_size)
    with _PARSE_LOCK:
        if ident not in _PARSED:
            _PARSED.clear()
            _PARSED[ident] = _PARSER.submit(lambda: threads_of(parse(path))).result()
        return _PARSED[ident]


def _resolve(file: str) -> Path:
    from silica.sources.convert import _resolve_input

    return _resolve_input(file)


def _stamp(thread: Thread, origin: str, path: str) -> dict:
    first = thread.mails[0]
    # Not `_inert`: a property is shown as text by Obsidian and the GUI alike,
    # where `&lt;` would read as itself. Images and links still go: a property
    # holding `[[x]]` is a link in Obsidian's graph.
    name = " ".join(_UNLINKABLE_RE.sub("", _no_images(first.sender)).split())
    sender = _unlinked_id(f"{name} <{first.address}>" if name and first.address
                          else first.address or name)
    return {
        "source": "mail",
        # The ingress stamp web capture writes (forms.py): clipped outside
        # content, so a later /nucleate picks the clip lens without sniffing.
        "form": "clip",
        "mail_thread": thread.key,
        # Every Message-ID, and for a message with none its content hash: the
        # set dedupe compares, so such a message is not taken as already here.
        "mail_ids": [m.ident for m in thread.mails],
        "mail_from": sender,
        "mail_date": first.date.date().isoformat() if first.date else "",
        "mail_origin": origin,
        "source_id": path.rsplit("/", 1)[-1],
    }


def _defaults_hold(file: str, src: Path) -> None:
    """A bare file reaches `read()` from /nucleate, which has no mail branch
    ("GUI and TUI"): the import's own plan decides, so it writes under the same
    defaults. A keyed target comes from /mail import, which already planned it,
    or from a user who typed the key, which is the same choice --all makes."""
    p = plan(src)
    if p.guessed and not p.takeout:
        raise ValueError(f"no address of yours is recorded for this vault; /mail import {file} "
                         "shows the thread and asks for it")
    if p.write:
        return
    if p.present:
        raise ValueError(f"{file} is already in the vault")
    raise ValueError(f"you took no part in the thread in {file}; "
                     f"/mail import {file} --all --apply writes it anyway")


class MailAdapter:
    name = "mail"

    def matches(self, target: str) -> bool:
        return bool(_TARGET_RE.match((target or "").strip()))

    def read(self, target: str) -> RawItem:
        file, key = split_target(target)
        src = _resolve(file)
        threads = threads_in(src)
        if key:
            thread = next((t for t in threads if t.key == key), None)
            if thread is None:
                raise ValueError(f"{file} holds no thread {key}")
        elif len(threads) != 1:
            raise ValueError(f"{file} names {len(threads)} threads; /mail import {file} "
                             "lists them and writes the ones you took part in")
        else:
            thread = threads[0]
            _defaults_hold(file, src)
        return RawItem(target=target, text=render(thread),
                       meta={"thread": thread, "origin": src.name})

    def to_stub(self, item: RawItem) -> GroundedStub:
        from silica.kernel.write import frontmatter

        thread: Thread = item.meta["thread"]
        path = note_path(thread)
        # Recorded before stage() writes, not after: a path recorded for a
        # write that then failed only taints a note that is not there, while
        # a write recorded late leaves a window where its note taints nothing.
        record_written(path, thread)
        return GroundedStub(lane="terminal", note_path=path,
                            body=frontmatter.dump(_stamp(thread, item.meta["origin"], path),
                                                  item.text))


MAIL = MailAdapter()


# --- point 4: the user's addresses, and the record outside the vault -------------

MAX_THREADS = 200
STATE_FILE = "mail_import.json"
LIMIT_ERROR = "--limit takes a whole number of threads, 1 or more"


def parse_limit(value) -> int:
    """`--limit` from the command or the Mail row; MAX_THREADS when not given.
    A negative limit sliced the plan from the far end, and "abc" surfaced
    int()'s own message in the row (2026-10-12)."""
    if value is None or str(value).strip() == "":
        return MAX_THREADS
    try:
        n = int(str(value).strip())
    except ValueError:
        raise ValueError(LIMIT_ERROR) from None
    if n < 1:
        raise ValueError(LIMIT_ERROR)
    return n


def _state_path(vault: str | None = None) -> Path:
    from silica.config import CONFIG
    from silica.kernel.recall.paths import index_dir_for

    return index_dir_for(vault or CONFIG.vault_path) / STATE_FILE


def load_state(vault: str | None = None) -> dict:
    import json

    try:
        state = json.loads(_state_path(vault).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(state, dict):
        raise ValueError(f"{_state_path(vault)} is not a mail import record")
    return state


def _save_state(state: dict, vault: str | None = None) -> None:
    import json

    from silica.kernel.recall.paths import atomic_write_bytes

    path = _state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: this file is also the taint gate's record (point 8), and a torn
    # write that emptied it would untaint every mail note at once.
    atomic_write_bytes(path, json.dumps(state, ensure_ascii=False, indent=1).encode("utf-8"))


def addresses(vault: str | None = None) -> tuple[str, ...]:
    return tuple(load_state(vault).get("me") or ())


def remember_addresses(given: list[str] | tuple[str, ...], vault: str | None = None) -> tuple[str, ...]:
    me = tuple(dict.fromkeys(a.strip().lower() for a in given if a.strip()))
    state = load_state(vault)
    state["me"] = list(me)
    _save_state(state, vault)
    return me


def guess_address(threads: list[Thread]) -> str:
    """The address the file was delivered to most often, else the one most
    often in To: the prefill the user confirms or corrects (point 4)."""
    from collections import Counter

    for field in ("delivered_to", "to"):
        seen = Counter(a for t in threads for m in t.mails for a in getattr(m, field))
        if seen:
            return seen.most_common(1)[0][0]
    return ""


def is_takeout(threads: list[Thread]) -> bool:
    """Every message carries X-Gmail-Labels, as Google Takeout writes them.
    Every, not any: one stranger's message with a forged label header must not
    switch the whole file to trusting labels over From."""
    mails = [m for t in threads for m in t.mails]
    return bool(mails) and all(m.labels for m in mails)


def took_part(thread: Thread, me: tuple[str, ...], takeout: bool) -> bool:
    """A message of the thread was sent by the user: in a Takeout file, one
    Gmail labelled `Sent`, a label a stranger cannot set; in any other file,
    one whose From is the user's.

    ponytail: outside Takeout, From can be forged, so a stranger who spoofs
    the user's own address passes. The ceiling holds while imports come from
    Takeout or from the user's own client's Sent folder; the upgrade is to
    require a matching Message-ID in the user's Sent folder, due if gate 1's
    spoofed-From fixture shows up in practice (ADR-0090 point 4)."""
    if takeout:
        return any("Sent" in m.labels for m in thread.mails)
    return any(m.address in me for m in thread.mails)


def _pdfs(thread: Thread) -> list[str]:
    return [a.name for m in thread.mails for a in m.attachments
            if a.ctype == "application/pdf" or a.name.lower().endswith(".pdf")]


def record_written(path: str, thread: Thread, vault: str | None = None) -> None:
    """ADR-0090 point 8: the path, in the record no chat tool writes, so the
    taint does not depend on the note's text. Its PDF attachments ride along
    in import order, which is what gate 6 counts."""
    state = load_state(vault)
    threads = state.setdefault("threads", [])
    if any(t.get("path") == path for t in threads):
        return
    threads.append({"path": path, "pdfs": _pdfs(thread)})
    _save_state(state, vault)


def record_import(file: str, written: int, run_id: str, vault: str | None = None) -> None:
    """One `/mail import --apply`, for the status line and gate 4's count of
    imports used."""
    state = load_state(vault)
    state.setdefault("imports", []).append({"at": dt.date.today().isoformat(), "file": file,
                                            "written": written, "run_id": run_id})
    _save_state(state, vault)


def recorded_paths(vault: str | None = None) -> set[str]:
    return {str(t.get("path")) for t in load_state(vault).get("threads") or () if t.get("path")}


def imported_threads(vault: str | None = None) -> list[dict]:
    """The recorded threads whose note is still where the import wrote it,
    in import order: what /mail, the Mail row and gate 6 count.

    The record itself is never pruned: it is the taint's ground truth (point
    8), and a path whose note a /revert removed taints nothing. Counted from
    it, a reverted import still read "13 threads imported" (2026-10-12). A
    note the user moved away drops out of the count too; it is still a mail
    note to the taint, by its stamp."""
    from silica.config import CONFIG

    root = Path(vault or CONFIG.vault_path or ".")
    return [t for t in load_state(vault).get("threads") or ()
            if t.get("path") and (root / str(t["path"])).is_file()]


_STAMP_RE = re.compile(r"^source:\s*['\"]?mail['\"]?\s*$", re.MULTILINE)


def _norm(path: str) -> str:
    """Vault-relative, `.md` dropped: recall's paths carry no extension, the
    driver's and this record's do."""
    p = path.replace("\\", "/").lstrip("/")
    return p[:-3] if p.lower().endswith(".md") else p


def _stamped(path: str) -> bool:
    from silica.config import CONFIG
    from silica.kernel.write.frontmatter import FM_RE

    p = Path(CONFIG.vault_path or ".") / (_norm(path) + ".md")
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False  # a path that is no note here (another vault's, a PDF) holds no stamp
    m = FM_RE.match(text)
    return bool(m and _STAMP_RE.search(m.group(1)))


def mail_note_among(paths: list[str]) -> str:
    """The first of `paths` that is a mail note, "" when none is (point 8).

    By the record first, which no chat tool writes, so a write that deletes
    the stamp does not untaint; then by the `source: mail` stamp, which is
    what still marks a note `silica_move` took off its recorded path.
    ponytail: a move followed by a write that deletes the stamp launders that
    one note; both sit in the journal for /revert. The upgrade is re-keying
    the record on `silica_move`, due if gate 3's launder case is seen outside
    a test."""
    recorded = {_norm(p) for p in recorded_paths()}
    hit = next((p for p in paths if _norm(p) in recorded), "")
    return hit or next((p for p in paths if _stamped(p)), "")


# Gate 6: if 10 of the first 50 imported threads carry a PDF the owner then
# nucleates by hand, attachments get their own ADR.
GATE6_WINDOW = 50
GATE6_FIRES = 10


def _nucleated_pdfs() -> set[str]:
    """Basenames of the documents /nucleate converted, from the `source_file`
    every converted segment carries, in the inbox (a run in progress) and in
    done/ (a finished one): the places cli._prior_conversions reads."""
    from silica.config import CONFIG
    from silica.driver import DRIVER
    from silica.kernel.vault_manifest import active_done_dir, active_inbox_dir
    from silica.kernel.write import frontmatter

    rels = [r.path for r in DRIVER.list_inbox_files() if r.path.endswith(".md")] \
        if active_inbox_dir() else []
    if done := active_done_dir():
        rels += [r.path for r in DRIVER.list_files(done)]
    out: set[str] = set()
    for rel in rels:
        try:
            data, _, _ = frontmatter.split((Path(CONFIG.vault_path) / rel).read_text(encoding="utf-8"))
        except OSError:
            continue  # a segment removed mid-scan was not nucleated from here
        src = str((data or {}).get("source_file") or "")
        if src:
            out.add(Path(src).name.lower())
    return out


def gate6(vault: str | None = None) -> dict:
    threads = imported_threads(vault)[:GATE6_WINDOW]
    done = _nucleated_pdfs()
    hits = sum(1 for t in threads if any(n.lower() in done for n in t.get("pdfs") or ()))
    return {"threads": len(threads), "with_pdf": sum(1 for t in threads if t.get("pdfs")),
            "nucleated": hits, "fires": hits >= GATE6_FIRES}


# --- point 6: dedupe by Message-ID ------------------------------------------------

def _ids_at(path: str) -> set[str] | None:
    """`mail_ids` of the note at `path`; None when there is no note there."""
    from silica.config import CONFIG
    from silica.kernel.write import frontmatter

    try:
        text = (Path(CONFIG.vault_path or ".") / path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    data, _, _ = frontmatter.split(text)
    return {str(i) for i in ((data or {}).get("mail_ids") or ())}


@dataclass
class Plan:
    """What one import would write: the dry run's listing, and --apply's work."""

    file: Path
    takeout: bool
    me: tuple[str, ...]
    guessed: bool               # `me` is the prefill, not an address the user gave
    in_file: int                # threads in the file, after the label
    took_part: int
    added_by_all: int           # threads the filter leaves out, which --all adds
    present: int                # already in the vault with every message
    grown: int                  # in `write` because their note lacks messages
    over_limit: int
    write: list[Thread]


def plan(file: str | Path, *, me: tuple[str, ...] | None = None, label: str = "",
         all_: bool = False, limit: int = MAX_THREADS) -> Plan:
    """ADR-0090 points 4 and 6, before anything is written.

    A thread whose messages are all in its note is skipped, so importing the
    same file twice writes nothing; one that gained messages is restaged whole.
    ponytail: whole means the file's copy, so an export holding only the newer
    half of a thread drops the older half from the note (one /revert restores
    it). Upgrade to a merge by Message-ID if an import does that by 2026-11-08,
    gate 4's 28 days after Part A."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(LIMIT_ERROR)
    src = _resolve(str(file))
    threads = threads_in(src)
    takeout = is_takeout(threads)
    if label:
        # Any case: the user types the label as Gmail shows it, and Takeout
        # writes it as Gmail stores it ("Category Promotions" vs "promotions").
        want = label.strip().casefold()
        threads = [t for t in threads
                   if any(want in {x.casefold() for x in m.labels} for m in t.mails)]
    me = tuple(a.strip().lower() for a in me or () if a.strip()) or addresses()
    guessed = not me
    if guessed:
        me = tuple(filter(None, [guess_address(threads)]))
    kept = [t for t in threads if took_part(t, me, takeout)]
    write: list[Thread] = []
    present = grown = 0
    for t in threads if all_ else kept:
        have = _ids_at(note_path(t))
        if have is not None and {m.ident for m in t.mails} <= have:
            present += 1
            continue
        grown += have is not None and len(write) < limit
        write.append(t)
    return Plan(file=src, takeout=takeout, me=me, guessed=guessed, in_file=len(threads),
                took_part=len(kept), added_by_all=len(threads) - len(kept), present=present,
                grown=grown, over_limit=max(0, len(write) - limit), write=write[:limit])


def apply(p: Plan) -> dict:
    """Write a plan's threads in one undo run (point 6), so one /revert takes
    the whole import back; the /mail command and the Mail row share it."""
    from silica.config import CONFIG
    from silica.kernel.write.undo_journal import get_undo_journal
    from silica.sources.registry import stage

    run_id = get_undo_journal().start_run(source="mail", vault=CONFIG.vault_path.strip() or None)
    written, failed = 0, []
    for t in p.write:
        result = stage(MAIL, f"{p.file}#{t.key}", run_id=run_id)
        if result["status"] == "ok":
            written += 1
        else:
            failed.append((t.mails[0].subject, str(result.get("message", ""))))
    record_import(p.file.name, written, run_id)
    return {"written": written, "run_id": run_id, "failed": failed}
