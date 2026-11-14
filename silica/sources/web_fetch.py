# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""`web_fetch` — read one URL and return its text. No third party in the path.

ADR-0015 staged acquisition: Silica may fetch on request but never decides what
enters the vault. This module returns text to its caller (the /web-search
research loop, or /fetch); nothing here writes to the vault.

`web_fetch` is `sensitive=True` (ADR-0009): the main agent's default toolset
excludes it, so it is reachable only where it is named explicitly in
AgentConstraints, or called directly by a command.

Direct httpx plus stdlib html.parser, no Jina and no trafilatura: a third-party
reader puts every fetched URL in front of someone else, which contradicts the
local-first posture. The price of dropping it is that SSRF becomes ours, and
`_validated` is that price paid in full.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import shutil
import socket
import subprocess
import sysconfig
import tempfile
import textwrap
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from silica.config import live_env
from silica.tools import tool

logger = logging.getLogger(__name__)

# ~7.5k tokens of a 60k default context budget. A LinkedIn guest page is 374 KB
# raw; without this ceiling one fetch eats the window.
_MAX_CHARS = 30_000
_MAX_REDIRECTS = 3
_HTTP_TIMEOUT = 30
# _MAX_CHARS bounds what reaches the model; this bounds what reaches memory,
# which nothing did before: a buffered GET materialises the whole body (and then
# a whole str) before any ceiling of ours runs, so one hostile or merely enormous
# URL OOMs the process while `truncate` waits its turn. ~270x the char ceiling,
# so no real page meets it.
MAX_BODY_BYTES = 8 * 1024 * 1024
# The ceiling in force: _MAX_CHARS for text a model reads (the agent's tool, the
# research loop, Discover), the body ceiling for a page the person asked to keep
# as a note (`for_a_note`). /fetch had the model's until 2026-11-14, when a
# 28-minute video's transcript (53,386 characters) became a note cut at 56%
# (ADR-0162 amendment 2026-11-14).
_CEILING: ContextVar[int] = ContextVar("web_fetch_ceiling", default=_MAX_CHARS)


def ceiling() -> int:
    """How many characters a fetched page keeps here: see `_CEILING`."""
    return _CEILING.get()


@contextmanager
def for_a_note() -> Iterator[None]:
    """Fetch for a note, not for a model: only the body ceiling holds. A
    ContextVar like site_fetch.PERSON_ASKED, because the ceiling is applied five
    calls down (a transcript, a page, a Reddit thread, a GitHub file)."""
    token = _CEILING.set(MAX_BODY_BYTES)
    try:
        yield
    finally:
        _CEILING.reset(token)


# `timeout` is per socket operation, not a deadline: an endless stream dripping
# one byte before each timeout window never trips it and never ends, so the body
# gets its own wall clock.
_BODY_DEADLINE = 120
YT_DOMAINS: tuple[str, ...] = ("youtube.com", "youtu.be")
# Languages only: `--sub-lang` does not choose between auto-generated and
# uploaded subs (that is `--write-auto-sub` / `--write-sub`, and we ask for
# both). Not `en.*`, which it was until 2026-10-17: YouTube names its machine
# translations `<to>-<from>` in lowercase (`en-de` is English from German),
# so `en.*` asked for every one of them, the 2nd came back HTTP 429, and the
# error ended yt-dlp before it wrote the info file, the note's title (measured
# on jNQXAC9IVRw). yt-dlp matches without case, so a region cannot be told
# from a source language by pattern (`en-[A-Z]+` took `en-de` too): the
# regions are named, `-orig` being the auto-captions' own language.
# Which of the downloaded files wins is alphabetical, not preferential:
# `sorted(glob("*.vtt"))[0]`, where `sub.en-GB.vtt` beats `sub.en.vtt` because
# `-` (0x2D) sorts before `.` (0x2E).
_YT_SUB_LANGS = "en,en-orig,en-US,en-GB,en-CA,en-AU,en-IE,en-IN,it,it-orig,it-IT,it-CH"
_YT_TIMEOUT = 120
# What YouTube answers a datacenter address with no signed-in session (the
# pilot host, 2026-10-17), and a session whose account is gone or whose
# cookies expired. The renewal is the operator's (silica-hosted
# bin/youtube-cookies), so it is named in the log and not to the student.
_YT_SIGN_IN = "Sign in to confirm"
# YouTube refusing a subtitle file, not the page. Measured 2026-11-13 on
# W4PIkmjwGsQ from a home address: `en` and `it` came back 429 while
# `en-orig`, the track `--list-subs` names "(Original)", downloaded in the
# same run (the video has no uploaded subtitles), with yt-dlp
# 2026.03.17 and 2026.10.26 plus curl_cffi alike. A refused track is
# therefore a warning (`--ignore-errors`), and this is said only when every
# requested track was refused.
_YT_TOO_MANY = "HTTP Error 429"
YOUTUBE_COOKIES_ENV = "SILICA_YOUTUBE_COOKIES"
IG_DOMAINS: tuple[str, ...] = ("instagram.com",)
# One reel's audio, about 1 MB a minute; the transcription after it has its own clock.
_IG_TIMEOUT = 120
# Hashtags and mentions, which a caption's first line is often all of.
_IG_TAG = re.compile(r"[#@][\w.]+")
# A reel's or a post's path, with or without the handle in front
# (/reel/C0de/, /p/C0de/, /tv/C0de/, /patty/reel/C0de/); a profile is not one.
_IG_POST = re.compile(r"^/(?:[\w.]+/)?(?:reels?|p|tv)/[\w-]+")


class WrongType(ValueError):
    """The server's content type is not one the caller reads."""


class TooLarge(ValueError):
    """The body passed the caller's ceiling."""


class YoutubeBlocked(ValueError):
    """YouTube asked for a sign-in this session does not have."""


class Refused(ValueError):
    """A 401, 403 or 429: the site said no. `status` and `headers` are for a
    caller that names the site (site_fetch), which the message alone hid."""

    def __init__(self, status: int, message: str, headers: httpx.Headers | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.headers = headers if headers is not None else httpx.Headers()


class SiteRefused(ValueError):
    """A site adapter's refusal (ADR-0162), in words written for the person:
    the vault's + shows `said` as the row's reason, as it does YouTube's wall."""

    def __init__(self, said: str) -> None:
        super().__init__(said)
        self.said = said


def youtube_cookies() -> Path | None:
    """The account cookies yt-dlp signs in with (ADR-0120 amendment), when the
    operator set SILICA_YOUTUBE_COOKIES to a readable file; None otherwise."""
    import os

    raw = live_env(YOUTUBE_COOKIES_ENV)
    path = Path(raw) if raw else None
    return path if path is not None and os.access(path, os.R_OK) else None


def ytdlp() -> str | None:
    """yt-dlp as silica's `youtube` extra installed it, else the one on PATH.

    The extra's first: a uv tool or pipx environment keeps its scripts off PATH,
    so `shutil.which` alone never finds what the extra installed, and the one on
    PATH can be a distribution's that went stale (apt's was 2026.03.17 on
    2026-11-13). The script and not `python -m yt_dlp`, which puts the working
    directory first on sys.path, and silica's is the vault.
    """
    return shutil.which("yt-dlp", path=sysconfig.get_path("scripts")) or shutil.which("yt-dlp")
_WP_DOMAINS: tuple[str, ...] = ("wikipedia.org",)
_WP_PREFIX = "/wiki/"
WP_API_PATH = "/w/api.php"
_WP_MATH_MAXLEN = 3
# Wikimedia's user-agent policy asks for a descriptive agent with a contact URL
# and throttles generic browser strings, so the API branch does not send
# HEADERS. Their own docs call the browser-string case "discouraged".
WP_HEADERS = {
    "User-Agent": "silica-agent (https://github.com/kiycoh/silica-agent)",
    "Accept": "application/json",
}
# A bare httpx user agent collects more 403s than a browser string does, and
# the 401/403/429 branch below is how we surface the ones that remain.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
}
TEXT_TYPES = ("text/", "application/xhtml", "application/xml", "application/json")
# What Discover reads with (ADR-0137 point 11): a reader that runs because a
# view opened announces itself, as the Wikipedia branch does, instead of
# passing for a person at a browser. Measured 2026-10-28: a descriptive agent
# got 200 from all ten tested feeds and the three article pages behind them.
READER_HEADERS = {
    "User-Agent": WP_HEADERS["User-Agent"],
    "Accept": ("application/rss+xml, application/atom+xml, application/feed+json, "
               "application/xml;q=0.9, text/html;q=0.8, */*;q=0.5"),
}
FEED_TYPES = (*TEXT_TYPES, "application/rss+xml", "application/atom+xml",
              "application/feed+json", "application/rdf+xml")

_SCHEMES = ("http", "https")


def host_matches(url: str, *domains: str) -> bool:
    """True when `url` is http(s), carries no userinfo, and its host is one of
    `domains` or a subdomain of one.

    Anchored on a leading dot, so `x.com.evil.test` (a substring match would
    pass) and `x.com@evil.test` (userinfo disguise) both come back False.
    Malformed ports only raise when `.port` is touched, so touch it.
    """
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() not in _SCHEMES:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False
    return any(
        host == d or host.endswith("." + d) for d in (x.lower() for x in domains)
    )


def _validated(url: str) -> str:
    """Fail closed on anything we should not open a socket to, and return the
    address that passed, which is the one `_stream` connects to.

    Rejects non-HTTP schemes, embedded credentials, and any hostname that
    resolves to a non-global address. `ipaddress.is_global` is the single
    primitive that covers loopback, RFC1918, link-local (including
    169.254.169.254, the cloud metadata endpoint), CGNAT and unique-local IPv6.
    Every resolved address must pass: one global answer cannot launder a
    private sibling.

    The address is returned because checking a name and then letting httpx
    resolve it again left a DNS-rebinding window between the two. That was
    deferred until Silica fetched attacker-supplied URLs unattended, and
    Discover does (ADR-0137 point 11): a feed names its own item links and
    image URLs, and they are read because a view opened.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise ValueError(f"malformed URL {url!r}: {e}") from e
    if parts.scheme.lower() not in _SCHEMES:
        raise ValueError(f"refusing non-HTTP URL: {url!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"refusing URL with embedded credentials: {url!r}")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError(f"URL has no host: {url!r}")
    default_port = 443 if parts.scheme.lower() == "https" else 80
    try:
        infos = socket.getaddrinfo(host, port or default_port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve {host!r}: {e}") from e
    if not infos:
        # An empty answer means we checked nothing, so the loop below approves
        # everything. The one function whose job is failing closed must not have
        # a branch that fails open.
        raise ValueError(f"cannot resolve {host!r}: no addresses returned")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"refusing non-global address {ip} for host {host!r}")
    return str(infos[0][4][0])


@contextmanager
def _stream(url: str, ip: str, headers: dict[str, str]) -> Iterator[httpx.Response]:
    """GET `url` over a connection to `ip`, the address `_validated` approved.

    The name stays the request's in every way a server or a certificate can
    see: the Host header is the URL's own authority and TLS is verified
    against the hostname through httpcore's `sni_hostname` extension, so a
    pinned fetch reads exactly the page an unpinned one would. Only the socket
    is told where to go, and it no longer asks DNS a second time.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    where = f"[{ip}]" if ":" in ip else ip
    if parts.port:
        where += f":{parts.port}"
    pinned = urlunsplit(parts._replace(netloc=where))
    ext = {"sni_hostname": host} if parts.scheme.lower() == "https" else {}
    with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=False) as client:
        req = client.build_request("GET", pinned, headers={**headers, "Host": parts.netloc},
                                   extensions=ext)
        resp = client.send(req, stream=True)
        try:
            yield resp
        finally:
            resp.close()


_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "svg", "iframe",
    "nav", "header", "footer", "form", "aside",
})
# `pre` is handled on its own, not here: it fences instead of breaking.
_BREAK_TAGS = frozenset({
    "p", "div", "br", "li", "tr", "td", "th", "section", "article",
    "blockquote", "title", "h1", "h2", "h3", "h4", "h5", "h6",
})
_FENCE = "```"


class _TextExtractor(HTMLParser):
    """Collect visible text, skipping boilerplate containers.

    ponytail: stdlib html.parser, not trafilatura. Measured on four real pages
    both cut raw HTML by 10x to 16x and trafilatura is only 1.05x to 1.5x
    tighter; lxml plus trafilatura is a heavy transitive tree for roughly 30%
    fewer boilerplate tokens. Revisit if that boilerplate measurably pollutes
    nucleated notes.

    Second ceiling: html.parser treats `<script>` and `<style>` as CDATA, so an
    unclosed or truncated one swallows every byte after it with no error and
    no truncation marker. A real HTML5 tokenizer (lxml/html5lib) recovers from
    unclosed CDATA where html.parser cannot; that recovery is the concrete
    reason to pay for that dependency, if this ever bites on real pages.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._last_alt = ""
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "pre":
            self._fence()
        elif tag == "img":
            self._image_alt(attrs)
        elif tag in _BREAK_TAGS:
            self.parts.append("\n")

    def _image_alt(self, attrs) -> None:
        """Emit an image's alt text, marked as an image.

        This parser reads no attributes anywhere else, so until now a fetched
        page arrived with its figures simply absent. On a technical page the alt
        is frequently the only description of a diagram that exists in the
        markup, which makes it the difference between a figure being in the vault
        and not.

        Marked `[image: ...]`, not inlined as prose the way omniparse inlines it:
        an alt is a caption, and a caption indistinguishable from body text is a
        small lie in a note somebody later quotes. Adjacent-equal dedup drops the
        icon rows that repeat one alt down a page; requiring a letter drops the
        `alt="***"` spacer. Decorative one-word alts still ride through, which
        the marker makes cheap: a reader skips a bracketed line.
        """
        if self._skip:
            return
        alt = " ".join((dict(attrs).get("alt") or "").split())
        if not alt or not any(c.isalpha() for c in alt) or alt == self._last_alt:
            return
        self._last_alt = alt
        self.parts.append(f"\n[image: {alt}]\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            # clamped: real pages ship stray close tags, and a negative counter
            # would swallow everything after one
            self._skip = max(0, self._skip - 1)
        elif tag == "pre":
            self._fence()
        elif tag in _BREAK_TAGS:
            self.parts.append("\n")

    def _fence(self) -> None:
        """Open or close a markdown code fence around a `<pre>` block.

        Suppressed inside a skipped container: a `<pre>` in a nav or an aside
        contributes no text, and a lone unpaired fence would turn the whole rest
        of the page into one code block.
        """
        if not self._skip:
            self.parts.append(f"\n{_FENCE}\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def extract_text(html: str) -> str:
    """HTML to readable plain text: drop boilerplate, collapse whitespace.

    Whitespace collapsing stops inside a fence. A `<pre>` block that keeps its
    fence but loses its indentation is half a job: the page's code arrives as
    prose either way. Lines between fences keep their leading whitespace.

    ponytail: fence state is tracked by counting our own emitted ``` lines, so
    a page whose *prose* contains a bare ``` line (a markdown document served as
    HTML) flips it. The alternative is threading a nesting depth out of the
    parser for a case that inverts one block's formatting, not the note.
    """
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines: list[str] = []
    in_code = False
    just_opened = False
    for raw in "".join(parser.parts).splitlines():
        if raw.strip() == _FENCE:
            if in_code and lines and not lines[-1]:
                # the source newline before `</pre>`, which every real page has;
                # a blank line is never significant at the end of a code block
                lines.pop()
            in_code = not in_code
            just_opened = in_code
            lines.append(_FENCE)
            continue
        if just_opened and not raw.strip():
            # browsers drop the newline right after `<pre>`, and real HTML
            # nearly always has one; keeping it opens every block with a blank
            just_opened = False
            continue
        just_opened = False
        line = raw.rstrip() if in_code else " ".join(raw.split())
        # keep at most one blank line between blocks, and none at the top;
        # inside a fence every line is significant, blanks included
        if line or in_code or (lines and lines[-1]):
            lines.append(line)
    if in_code:
        # truncated page, or a stray ``` in the prose: never hand the vault an
        # open fence, which renders every line after it as code
        lines.append(_FENCE)
    return "\n".join(lines).strip()


def truncate(text: str, limit: int | None = None) -> str:
    """Hard ceiling with a visible marker, so the model knows it saw a prefix.
    `limit` defaults to the ceiling in force, which /fetch lifts."""
    limit = ceiling() if limit is None else limit
    if len(text) <= limit:
        return text
    # A cut inside a code block closes it, or the truncation marker (and, in
    # the vault, everything after it) renders as code.
    return f"{close_fence(text[:limit].rstrip())}\n\n[truncated at {limit} characters]"


def close_fence(text: str) -> str:
    """`text` with a code fence it left open closed at its end. An opening
    fence names its language (```bash) and only the closing one is bare, so
    a bare line is not the rule; a backtick after the run is, since a fence's
    info string holds none (CommonMark): ```pip install x``` is inline code."""
    if sum(1 for ln in text.splitlines() if _FENCE_LINE.fullmatch(ln)) % 2:
        return f"{text}\n{_FENCE}"
    return text


_FENCE_LINE = re.compile(r"[ \t]*`{3,}[^`]*")


_MAX_TITLE = 120
# Separators real pages put between a page title and their own site name.
_TITLE_SEPS = (" | ", " - ", " – ", " — ", " :: ", " · ", " » ")


class _TitleExtractor(HTMLParser):
    """`<title>`, `og:title`, `twitter:title` and `og:site_name`, nothing else.

    Structured harvest, because the alternative in use was positional: take the
    first line of the extracted text and hope it is the title. That hope fails
    on every page whose text path is not the rendered HTML (the Wikipedia API
    extract opens with the lead sentence, a YouTube transcript with the first
    thing said) and on every page that has no `<title>` at all.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.og_title = ""
        self.twitter_title = ""
        self.site_name = ""
        # The image a page declares for sharing: Discover's cover when its
        # feed item carried none (ADR-0137 point 7). `twitter:image` is the
        # second choice, as twitter:title is.
        self.og_image = ""
        self.twitter_image = ""
        self._in_title = False
        self._seen_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "title" and not self._seen_title:
            # first one wins: `<svg><title>` is an accessibility label, and it
            # comes after the real one in the head
            self._in_title = True
            self._seen_title = True
        elif tag == "meta":
            a = dict(attrs)
            # `property` is the OG spec, `name` is what half the web ships
            # instead; twitter cards only ever use `name`
            key = (a.get("property") or a.get("name") or "").lower()
            content = (a.get("content") or "").strip()
            if not content:
                return
            if key == "og:title" and not self.og_title:
                self.og_title = content
            elif key == "twitter:title" and not self.twitter_title:
                self.twitter_title = content
            elif key == "og:site_name" and not self.site_name:
                self.site_name = content
            elif key in ("og:image", "og:image:url", "og:image:secure_url") and not self.og_image:
                self.og_image = content
            elif key in ("twitter:image", "twitter:image:src") and not self.twitter_image:
                self.twitter_image = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def _strip_site_suffix(title: str, site_name: str) -> str:
    """Drop a trailing `<separator><site name>` when the page names its own site.

    Only a suffix we can positively identify is removed. Splitting on the
    separator alone would eat the back half of "The Pragmatic Programmer - 20th
    Anniversary Edition", and a truncated title is no better for the reranker
    than a polluted one.

    ponytail: a page that omits `og:site_name` keeps its suffix. If polluted
    titles show up in the vault, derive the candidate site name from the host,
    never from the separator position.
    """
    if not site_name:
        return title
    for sep in _TITLE_SEPS:
        tail = sep + site_name
        if title.endswith(tail):
            return title[: -len(tail)].strip() or title
    return title


def page_title(html: str) -> str:
    """The page's own title: `og:title`, else `twitter:title`, else `<title>`.

    OG first because it is authored for sharing and so usually arrives without
    the site-name suffix that `<title>` carries for SEO. Empty string when the
    page declares no title at all, which is the caller's cue to fall back.
    """
    parser = _TitleExtractor()
    parser.feed(html)
    parser.close()
    site_name = " ".join(parser.site_name.split())
    for candidate in (parser.og_title, parser.twitter_title, parser.title):
        title = " ".join(candidate.split())
        if title:
            return _strip_site_suffix(title, site_name)[:_MAX_TITLE]
    return ""


def page_image(html: str, base: str) -> str:
    """The page's own cover, `og:image` else `twitter:image`, as an absolute
    http(s) URL; "" when it declares none.

    html.parser, not a pattern: one of the three pages measured for ADR-0137
    writes `content` before `property` and leaves the value unquoted
    (`property=og:image`), which a regex keyed on `property="og:image"` misses.
    """
    parser = _TitleExtractor()
    parser.feed(html)
    parser.close()
    for candidate in (parser.og_image, parser.twitter_image):
        # An empty value joins to the page itself, which is no cover.
        if not candidate.strip():
            continue
        url = urljoin(base, candidate.strip())
        if urlsplit(url).scheme in _SCHEMES:
            return url
    return ""


def _render(url: str, text: str) -> str:
    """Header line plus body.

    `Source:` carries the final URL after redirects, so a citation points at
    what was actually read, and web_research can lift it out of the tool trace.
    """
    return f"Source: {url}\n\n{text}"


def _raise_for_status(resp: httpx.Response, url: str) -> None:
    """401, 403 and 429 are the failures a direct fetcher actually meets, and
    they mean different things to the caller. Distinct messages, not one
    generic HTTPStatusError."""
    if resp.status_code in (401, 403):
        raise Refused(
            resp.status_code,
            f"{resp.status_code} at {url}: the site refuses unauthenticated "
            "reads (bot wall or paywall). Try a different source.",
            resp.headers,
        )
    if resp.status_code == 429:
        raise Refused(
            429, f"429 at {url}: rate limited. Back off, or use a different source.", resp.headers
        )
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        # The request went to the pinned address (`_stream`), so httpx's words
        # name an IP the person never typed; theirs is `url`.
        raise httpx.HTTPStatusError(
            f"{resp.status_code} {httpx.codes.get_reason_phrase(resp.status_code)} at {url}",
            request=resp.request, response=resp) from None


def content_type(resp: httpx.Response) -> str:
    """The bare media type, lowercased, without parameters."""
    return resp.headers.get("content-type", "").split(";")[0].strip().lower()


def _refuse_binary(resp: httpx.Response, url: str, types: tuple[str, ...] = TEXT_TYPES) -> None:
    """Decided on the headers, before a byte of the body is spent: a PDF or a
    video is refused either way, and refusing it after buffering it is the
    expensive way to reach the same answer. `types` is what the caller reads:
    text for a page, feeds for Discover, `image/` for a cover."""
    ctype = content_type(resp)
    if ctype and not ctype.startswith(types):
        raise WrongType(f"refusing to read {ctype} content at {url}")
    if not ctype and "text/" not in types:
        # A caller that reads no text (a cover) takes a body only on its
        # word: an untyped one can be anything, and a page has no business
        # being saved as an image.
        raise WrongType(f"refusing untyped content at {url}")


def _decoded_headers(headers: httpx.Headers) -> httpx.Headers:
    """The response headers minus the ones that describe the wire body.

    `iter_bytes` hands back DECODED bytes, so carrying `Content-Encoding: gzip`
    onto the rebuilt response makes httpx decompress an already-decompressed
    body — every gzip site (i.e. most of the web) died on `.text` with
    "Error -3 while decompressing data: incorrect header check". Content-Length
    describes the compressed body too, and is equally a lie once decoded.
    """
    out = httpx.Headers(headers)
    for name in ("content-encoding", "content-length"):
        if name in out:
            del out[name]
    return out


def _read_capped(resp: httpx.Response, url: str, cap: int = MAX_BODY_BYTES) -> bytes:
    """Accumulate the body against a size ceiling and a wall clock, aborting on
    either. Content-Length is not consulted: it is the server's claim, and a
    chunked response makes none."""
    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + _BODY_DEADLINE
    for chunk in resp.iter_bytes():
        total += len(chunk)
        if total > cap:
            raise TooLarge(
                f"body at {url} passed {cap} bytes; refusing to read on"
            )
        if time.monotonic() > deadline:
            raise ValueError(
                f"body at {url} still arriving after {_BODY_DEADLINE}s; giving up"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def fetch(url: str, headers: dict[str, str] = HEADERS, *, types: tuple[str, ...] = TEXT_TYPES,
           cap: int = MAX_BODY_BYTES) -> tuple[httpx.Response, str]:
    """GET with redirects followed by hand, revalidating every hop, and a body
    read under a ceiling instead of buffered whole.

    Open WebUI validates the first URL and then hands it to a client that
    follows redirects itself, so a perfectly global URL can 302 into link-local
    space. Following them here closes that, and each hop connects to the
    address its own check approved (`_stream`).

    Streamed, so the two cheap refusals (wrong content type, oversized body)
    happen while the body is still on the wire. The response handed back carries
    the bytes we accepted, so callers keep reading `.text` / `.json()`.

    A 304 comes back as it is, with no body: only a caller that sent
    `If-None-Match` or `If-Modified-Since` can get one, and that caller holds
    the copy it means.
    """
    for _ in range(_MAX_REDIRECTS + 1):
        ip = _validated(url)
        with _stream(url, ip, headers) as resp:
            if resp.status_code == 304:
                return httpx.Response(304, headers=_decoded_headers(resp.headers),
                                      request=resp.request), url
            location = resp.headers.get("location")
            if resp.is_redirect and location:
                # Against the URL as named, never the pinned one: a relative
                # Location resolved on the IP would send the next hop there.
                hop = urljoin(url, location)
                if urlsplit(hop).hostname != urlsplit(url).hostname:
                    # A token is for the host it was sent to (GitHub's, ADR-0162):
                    # a hop to another host goes without it.
                    headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
                url = hop
                continue
            _raise_for_status(resp, url)
            _refuse_binary(resp, url, types)
            body = _read_capped(resp, url, cap)
            return (
                httpx.Response(
                    resp.status_code,
                    headers=_decoded_headers(resp.headers),
                    content=body,
                    request=resp.request,
                ),
                url,
            )
    raise ValueError(f"more than {_MAX_REDIRECTS} redirects, giving up at {url}")


def _strip_mathml(extract: str) -> str:
    """Drop the exploded MathML that `explaintext` leaves beside every formula.

    TextExtracts strips tags from the rendered `<math>` element, so a formula
    arrives twice: first its presentation MathML, one glyph per indented line
    (`P` / `R` / `(` / `E` / `)`), then the readable
    `{\\displaystyle PR(E).}`. Measured on PageRank, Transformer and Maxwell's
    equations the glyph lines are 40-47% of the extract, and those articles run
    60k to 122k chars against a 30k ceiling: the junk is being truncated in
    ahead of the prose.

    Indented AND at most three characters is the whole rule. Prose survives it
    (Wikipedia indents inline fragments by one space, MathML by two or more),
    and so do the indented pseudocode blocks, which a plain drop-every-indented
    -line rule ate. ponytail: about a dozen multi-glyph operator labels per
    article ("masked tokens", "otherwise") ride through. A longer cut buys 0.6%
    and starts eating real lines, so it stays at three.
    """
    return "\n".join(
        line
        for line in extract.splitlines()
        if not (line.startswith("  ") and len(line.strip()) <= _WP_MATH_MAXLEN)
    )


def _wikipedia_extract(url: str) -> tuple[str, str] | None:
    """Article plaintext and canonical title, from the MediaWiki API instead of
    the rendered page.

    Wikipedia is the one site where extract_text is measurably poor. On
    /wiki/PageRank the HTML path returns 30k chars that stop 14 sections in and
    spend their tail on the footer navbox, while `prop=extracts` returns the
    whole 60k-char article and nothing else: no nav, no image captions, no
    navbox. Keyless, and the host carries the language, so it.wikipedia.org
    answers in Italian. Prose comes out identical to the HTML path minus the
    `[6]` reference markers; display math arrives doubled and
    `_strip_mathml` keeps the readable half.

    None means "not a plain article", and the caller reads the HTML instead:
    a query string (`?action=history`), an empty title, or a page the API has
    no extract for (Special:Random, a red link). Errors are not swallowed --
    a 429 from the API raises like any other fetch.

    The title comes back from the response, not from the path: with
    `redirects=1` the API answers /wiki/Page_rank with "PageRank", and that
    canonical form is what the note should be called.
    """
    parts = urlsplit(url)
    if parts.query or not parts.path.startswith(_WP_PREFIX):
        return None
    title = unquote(parts.path[len(_WP_PREFIX):])
    if not title:
        return None
    query = urlencode(
        {
            "action": "query",
            "format": "json",
            "formatversion": "2",  # pages as a list, not keyed by pageid
            "prop": "extracts",
            "explaintext": "1",
            "exsectionformat": "wiki",  # `== Heading ==`, pinned: the default
            "redirects": "1",           # /wiki/Page_rank -> PageRank
            "titles": title,
        }
    )
    api = f"{parts.scheme}://{parts.hostname}{WP_API_PATH}?{query}"
    resp, _ = fetch(api, headers=WP_HEADERS)
    pages = resp.json().get("query", {}).get("pages") or [{}]
    extract = _strip_mathml(pages[0].get("extract", "")).strip()
    if not extract:
        return None
    return extract, " ".join(str(pages[0].get("title", "")).split())[:_MAX_TITLE]


class Page(NamedTuple):
    """What one fetch yielded: the rendered text, and the page's own title.

    `title` is "" when the source declares none, which is the caller's cue to
    fall back. It is deliberately not folded into `text`: the `Source: <url>`
    header is a parsed contract with web_research, and a second header line
    would move the body at every consumer that hardcodes that shape.
    """

    text: str
    title: str


def fetch_page(url: str, headers: dict[str, str] = HEADERS) -> Page:
    """`web_fetch` plus the title, for callers that name a note after the page.

    The tool wrapper below drops the title; nothing else about the text differs,
    so both paths stay one code path. `headers` is for Discover, which reads an
    article as itself (`READER_HEADERS`) and not as a browser.
    """
    if host_matches(url, *YT_DOMAINS):
        return _youtube_transcript(url)
    from silica.sources import site_fetch

    # Only for an address the person typed (/fetch): a reel has no subtitle
    # track, so reading one is a download and a transcription, minutes that a
    # model following a link it found must not spend. Its page as HTML otherwise.
    if host_matches(url, *IG_DOMAINS) and site_fetch.PERSON_ASKED.get():
        if not _IG_POST.match(urlsplit(url).path):
            # yt-dlp's profile reader is marked broken (2026.10.26) and answers
            # a profile with "please report this issue", which is not ours to say.
            raise SiteRefused(f"{url} is not a reel or a post: /fetch reads one Instagram "
                              "address at a time (instagram.com/reel/... or /p/...), "
                              "several in one /fetch, and not a whole profile")
        return _instagram_post(url)
    # A Reddit thread or a GitHub repository, file, issue or pull request, read
    # from the site's JSON (ADR-0162); None for any other page of those sites,
    # which takes the HTML path below.
    if (site := site_fetch.page(url, headers)) is not None:
        return site
    if host_matches(url, *_WP_DOMAINS):
        # Cites the requested URL, not the redirect target: it is what resolves
        # in a browser, and the anchor in /wiki/PageRank#History survives it.
        article = _wikipedia_extract(url)
        if article:
            extract, title = article
            return Page(_render(url, truncate(extract)), title)
    return html_page(url, headers)


def html_page(url: str, headers: dict[str, str] = HEADERS) -> Page:
    """The page at `url` read as a page: HTML as its text, plain text and JSON
    as they are. Also `site_fetch`'s, for a GitHub address the API does not know."""
    resp, final_url = fetch(url, headers)
    # the type gate already ran in fetch, on the headers, before the body
    ctype = content_type(resp)
    # no charset sniffing, httpx already decoded from the header.
    body = resp.text
    is_html = "html" in ctype or not ctype
    text = extract_text(body) if is_html else body
    # plain text and JSON carry no title element; "" says so rather than
    # promoting their first line to one
    return Page(_render(final_url, truncate(text)), page_title(body) if is_html else "")


def link_title(url: str) -> str:
    """The title a pasted address takes as its link's words (ADR-0161), through `fetch`'s
    guards, ceiling and clocks; "" when it names none or is not a page. A YouTube video asks
    the site's oEmbed, a few hundred bytes: `_youtube_transcript` runs yt-dlp for a minute,
    and a server's watch page can be the consent wall, whose title is the wall's."""
    if host_matches(url, *YT_DOMAINS):
        resp, _ = fetch(f"https://www.youtube.com/oembed?{urlencode({'url': url, 'format': 'json'})}")
        return " ".join(str(resp.json().get("title") or "").split())[:_MAX_TITLE]
    resp, _ = fetch(url)
    ctype = content_type(resp)
    return page_title(resp.text) if "html" in ctype or not ctype else ""


# ingress: /web, a page's text.
@tool(cls="atomic", sensitive=True, ingress=True, label=("web fetch", "url"))
def web_fetch(url: str) -> str:
    """Read one web page and return its text, boilerplate stripped and
    truncated. Call this on a promising search result instead of guessing from
    its snippet. The first line is `Source: <url>`, which is what to cite."""
    return fetch_page(url).text


_VTT_TAG_RE = re.compile(r"<[^>]*>")
_VTT_NOISE = ("WEBVTT", "Kind:", "Language:", "NOTE ", "STYLE", "REGION")
# `00:01:02.500 --> 00:01:04.000`, hours optional (whisper.cpp omits them).
_VTT_CUE_RE = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)


def _cue_times(line: str) -> tuple[float, float] | None:
    """(start, end) in seconds for a VTT cue-timing line, else None."""
    m = _VTT_CUE_RE.search(line)
    if not m:
        return None
    h1, m1, s1, ms1, h2, m2, s2, ms2 = m.groups()
    start = int(h1 or 0) * 3600 + int(m1) * 60 + int(s1) + int(ms1.ljust(3, "0")) / 1000
    end = int(h2 or 0) * 3600 + int(m2) * 60 + int(s2) + int(ms2.ljust(3, "0")) / 1000
    return start, end


# A paragraph ends at the first sentence end past this many characters, about a
# minute of speech at 150 words a minute, and at the first line past twice that
# when no sentence ends (auto-captions can come unpunctuated). YouTube's rolling
# cues carry no pause to cut on: 0 gaps over 2,987 cues on W4PIkmjwGsQ
# (2026-11-14), so without this a transcript was one paragraph.
_VTT_PARAGRAPH_CHARS = 1000
_VTT_SPEAKER = ">>"


def vtt_to_text(vtt: str, paragraph_gap_s: float = 0.0) -> str:
    """VTT to prose paragraphs: drop cue timings and inline markup, collapse the
    rolling duplication auto-subs produce (each cue repeats the line before it),
    and join a paragraph's caption lines with spaces, as `_paragraphs_at_pauses`
    does for a verbose_json transcript.

    Lines were joined by newlines until 2026-11-14: a 40-character line per row
    in Obsidian, and in the page one paragraph whose first `>>` opened a nested
    quote holding 97% of the text (W4PIkmjwGsQ). YouTube's `>>`, a change of
    speaker, now starts a paragraph and is dropped.

    ponytail: adjacent-equal dedup, not a diff of overlapping cues. It clears
    the common rolling case; upgrade to longest-common-suffix trimming only if
    real transcripts come out visibly doubled.

    `paragraph_gap_s` > 0 also ends a paragraph wherever the speaker paused for
    at least that long, which matters for a transcript headed into the vault: a
    text with no blank line anywhere is ONE paragraph, `_split_by_size` leaves an
    oversized paragraph whole, and a two-hour talk would land as a single inbox
    note whose concepts RECON caps at 40.
    """
    paras: list[str] = []
    para: list[str] = []
    size, last, pause, prev_end = 0, "", False, None
    for raw in vtt.splitlines():
        s = raw.strip()
        if not s or s.isdigit() or s.startswith(_VTT_NOISE):
            continue
        if "-->" in s:
            if times := _cue_times(s):
                pause = pause or bool(paragraph_gap_s and prev_end is not None
                                      and times[0] - prev_end >= paragraph_gap_s)
                prev_end = times[1]
            continue
        s = unescape(_VTT_TAG_RE.sub("", s)).strip()
        if not s or s == last:
            continue
        last = s
        turn = s.startswith(_VTT_SPEAKER)
        if para and (pause or turn or size >= 2 * _VTT_PARAGRAPH_CHARS
                     or (size >= _VTT_PARAGRAPH_CHARS and para[-1].endswith((".", "?", "!")))):
            paras.append(" ".join(para))
            para, size = [], 0
        pause = False
        # Every leading `>`, not the two of `>>`: a `>>>` (a change of topic in
        # broadcast captions) left one, and a paragraph opening with it is a quote.
        if s := s.lstrip(">").strip():
            para.append(s)
            size += len(s) + 1
    if para:
        paras.append(" ".join(para))
    return "\n\n".join(paras)


def _youtube_transcript(url: str) -> Page:
    """Subtitles via yt-dlp, keyless.

    There is no shortcut worth trying: the watch page does carry
    `captionTracks`, but every `baseUrl` now returns HTTP 200 with 0 bytes in
    every format, because timedtext is gated behind a PO token. yt-dlp handles
    the token and player-client dance.

    Auto-subs are ASR output: transcription errors, no speaker labels. Anything
    nucleated from them inherits that noise, so the sources/ leaf matters more
    here than usual.

    ponytail: the `youtube` extra installs yt-dlp, and no doctor checks it. A
    clear prescription at call time beats a health-check subsystem. If stale-venv
    shims start confusing users, the upgrade is Agent-Reach's probe taxonomy
    (missing / broken / timeout / error), not an installer.
    """
    exe = ytdlp()
    if not exe:
        raise ValueError(
            "reading YouTube needs yt-dlp: install silica with its `youtube` extra "
            '(`"silica-agent[gui,mcp,youtube]"`, your install command\'s extras plus '
            "`youtube`), or put yt-dlp on PATH. The watch page itself is a "
            "JavaScript shell with no transcript in the HTML."
        )
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "sub"
        signed_in: list[str] = []
        if cookies := youtube_cookies():
            # A copy, not the file: yt-dlp writes its cookie jar back to the
            # path it was given, and every tenant reads the one file at once.
            jar = Path(tmp) / "cookies.txt"
            shutil.copyfile(cookies, jar)
            signed_in = ["--cookies", str(jar)]
        proc = subprocess.run(
            [
                exe, "--skip-download", "--write-auto-sub", "--write-sub",
                "--write-info-json", *signed_in,
                # Without it the first refused track (a 429 on a translation)
                # ends yt-dlp before the tracks after it and before the info
                # file, the note's title: see _YT_TOO_MANY.
                "--ignore-errors",
                "--sub-lang", _YT_SUB_LANGS, "--sub-format", "vtt",
                "--no-playlist", "--playlist-items", "1", "-o", str(out), "--", url,
            ],
            capture_output=True,
            text=True,
            timeout=_YT_TIMEOUT,
        )
        files = sorted(Path(tmp).glob("*.vtt"))
        if not files and _YT_SIGN_IN in (proc.stderr or ""):
            # The line the operator greps for; the probe pages on the same.
            # Two literals and no argument: the server's log keeps a silica
            # record's template only (silica/ui/logging.py), never its args.
            if signed_in:
                logger.warning("youtube: sign-in wall with the cookies set: renew them, "
                               "silica-hosted bin/youtube-cookies")
            else:
                logger.warning("youtube: sign-in wall with no cookies: set SILICA_YOUTUBE_COOKIES")
            raise YoutubeBlocked(f"YouTube asked to sign in before it would show {url}")
        if not files and _YT_TOO_MANY in (proc.stderr or ""):
            raise SiteRefused(f"YouTube refused every subtitle track of {url} with HTTP 429, "
                              "too many requests: try again later")
        if not files:
            raise ValueError(f"no subtitles came back from yt-dlp for {url}"
                             + (f": {said}" if (said := _ytdlp_said(proc)) else ""))
        text = vtt_to_text(files[0].read_text(encoding="utf-8", errors="replace"))
        title = _yt_info_title(Path(tmp))
    return Page(_render(url, truncate(text)), title)


def _ytdlp_said(proc) -> str:
    """The one line of yt-dlp's output that says why no subtitles came: its last
    `ERROR:`, else its "There are no subtitles" notice, else its last line.

    Not the last 300 characters of stderr, which it was until 2026-11-13: that
    cut a warning mid-word ("se impersonation") and carried the ERROR as a
    second line the chat drew as a box of its own. The notice is on stdout and
    the warnings on stderr, so stderr alone hid it behind a JS-runtime warning.
    """
    lines = [ln.strip() for ln in f"{proc.stdout or ''}\n{proc.stderr or ''}".splitlines() if ln.strip()]
    said = next((ln for ln in reversed(lines) if ln.startswith("ERROR:") or "no subtitles" in ln),
                lines[-1] if lines else "")
    return textwrap.shorten(said.removeprefix("ERROR:").removeprefix("[info]"), 300, placeholder="…")


def _yt_info_title(tmp: Path) -> str:
    """The video's title from the sidecar `--write-info-json` writes.

    One extra flag on a subprocess we already run, and it is the only real title
    a transcript has: the alternative is the first thing said in the video.
    Best effort by design -- a missing or malformed sidecar costs the note its
    title, not the transcript.
    """
    for info in sorted(tmp.glob("*.info.json")):
        try:
            data = json.loads(info.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        title = " ".join(str(data.get("title") or "").split())
        if title:
            return title[:_MAX_TITLE]
    return ""


def _instagram_post(url: str) -> Page:
    """A reel's or a video post's speech, transcribed, under its caption.

    Instagram serves no subtitle track, so unlike YouTube the audio is
    downloaded (`bestaudio`, never the video) and goes through /convert's
    transcriber, with its admission and charge. An image post has no video,
    and yt-dlp's error says so.

    shortcut: a carousel gives its first item only (`--playlist-items 1`), so
    one whose video is not first ends with no audio. Recheck by 2027-02-15:
    count the "no audio came back" lines in the logs, and read every item
    (`--playlist-items` dropped) from the first one that was a carousel.
    """
    exe = ytdlp()
    if not exe:
        raise ValueError("reading an Instagram reel needs yt-dlp: install silica with its "
                         "`youtube` extra, or put yt-dlp on PATH")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        proc = subprocess.run(
            [exe, "-f", "bestaudio/best", "--write-info-json", "--no-write-playlist-metafiles",
             "--no-playlist", "--playlist-items", "1", "-o", str(out / "post.%(ext)s"), "--", url],
            capture_output=True, text=True, timeout=_IG_TIMEOUT,
        )
        media = [p for p in out.glob("post.*") if p.suffix not in (".json", ".part", ".ytdl")]
        if not media:
            raise ValueError(f"no audio came back from yt-dlp for {url}"
                             + (f": {said}" if (said := _ytdlp_said(proc)) else ""))
        info = _ig_info(out / "post.info.json")
        from silica.sources.convert import transcribe_media

        (out / "work").mkdir()
        transcript = transcribe_media(media[0], out / "work").strip()
    caption = str(info.get("description") or "").strip()
    if not transcript and not caption:
        raise ValueError(f"no speech and no caption in {url}")
    handle = str(info.get("channel") or info.get("uploader") or "").strip()
    raw_day = str(info.get("upload_date") or "")
    day = f"{raw_day[:4]}-{raw_day[4:6]}-{raw_day[6:]}" if len(raw_day) == 8 else ""
    posted = "Posted" + (f" by @{handle}" if handle else "") + (f" on {day}" if day else "") + "."
    body = "\n\n".join(filter(None, (
        posted,
        f"## Caption\n\n{caption}" if caption else "",
        f"## Transcript\n\n{transcript or 'No speech in the audio.'}",
    )))
    return Page(_render(url, truncate(body)), _ig_title(caption, handle, day, str(info.get("id") or "")))


def _ig_info(path: Path) -> dict:
    """The post's yt-dlp sidecar, {} when it is missing or unreadable: it
    carries the caption and the date, and the transcript stands without them."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _ig_title(caption: str, handle: str, day: str, shortcode: str) -> str:
    """The caption's first line with words in it, hashtags and mentions out;
    else who and when. yt-dlp's own title is "Video by <handle>" for every
    reel, so a profile's notes would all slug to one name plus a counter."""
    for line in caption.splitlines():
        if lead := " ".join(_IG_TAG.sub("", line).split()):
            return textwrap.shorten(lead, _MAX_TITLE, placeholder="…")
    return " ".join(filter(None, (handle, day, shortcode))) or "Instagram post"
