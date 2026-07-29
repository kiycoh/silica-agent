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
import re
import shutil
import socket
import subprocess
import tempfile
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from silica.tools import tool

# ~7.5k tokens of a 60k default context budget. A LinkedIn guest page is 374 KB
# raw; without this ceiling one fetch eats the window.
_MAX_CHARS = 30_000
_MAX_REDIRECTS = 3
_HTTP_TIMEOUT = 30
_YT_DOMAINS: tuple[str, ...] = ("youtube.com", "youtu.be")
# Auto-generated subs first, then uploaded ones, English then Italian.
_YT_SUB_LANGS = "en.*,it.*,en"
_YT_TIMEOUT = 120
# A bare httpx user agent collects more 403s than a browser string does, and
# the 401/403/429 branch below is how we surface the ones that remain.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
}
_TEXT_TYPES = ("text/", "application/xhtml", "application/xml", "application/json")

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


def _validated(url: str) -> None:
    """Fail closed on anything we should not open a socket to.

    Rejects non-HTTP schemes, embedded credentials, and any hostname that
    resolves to a non-global address. `ipaddress.is_global` is the single
    primitive that covers loopback, RFC1918, link-local (including
    169.254.169.254, the cloud metadata endpoint), CGNAT and unique-local IPv6.
    Every resolved address must pass: one global answer cannot launder a
    private sibling.

    ponytail: residual TOCTOU. httpx resolves the hostname again after this
    check, so a hostile DNS server could rebind between the two. Closing it
    means pinning the resolved IP into the transport and setting the Host
    header by hand; build that only if Silica ever fetches attacker-supplied
    URLs unattended. Today they come from Tavily results or from the user.
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
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"refusing non-global address {ip} for host {host!r}")


_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "svg", "iframe",
    "nav", "header", "footer", "form", "aside",
})
_BREAK_TAGS = frozenset({
    "p", "div", "br", "li", "tr", "td", "th", "section", "article",
    "blockquote", "pre", "title", "h1", "h2", "h3", "h4", "h5", "h6",
})


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
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BREAK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            # clamped: real pages ship stray close tags, and a negative counter
            # would swallow everything after one
            self._skip = max(0, self._skip - 1)
        elif tag in _BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _extract_text(html: str) -> str:
    """HTML to readable plain text: drop boilerplate, collapse whitespace."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines: list[str] = []
    for raw in "".join(parser.parts).splitlines():
        line = " ".join(raw.split())
        # keep at most one blank line between blocks, and none at the top
        if line or (lines and lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip()


def _truncate(text: str, limit: int = _MAX_CHARS) -> str:
    """Hard ceiling with a visible marker, so the model knows it saw a prefix."""
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}\n\n[truncated at {limit} characters]"


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
        raise ValueError(
            f"{resp.status_code} at {url}: the site refuses unauthenticated "
            "reads (bot wall or paywall). Try a different source."
        )
    if resp.status_code == 429:
        raise ValueError(
            f"429 at {url}: rate limited. Back off, or use a different source."
        )
    resp.raise_for_status()


def _fetch(url: str) -> tuple[httpx.Response, str]:
    """GET with redirects followed by hand, revalidating every hop.

    Open WebUI validates the first URL and then hands it to a client that
    follows redirects itself, so a perfectly global URL can 302 into link-local
    space. Following them here closes that.
    """
    for _ in range(_MAX_REDIRECTS + 1):
        _validated(url)
        resp = httpx.get(
            url, follow_redirects=False, timeout=_HTTP_TIMEOUT, headers=_HEADERS
        )
        if not resp.is_redirect or resp.next_request is None:
            _raise_for_status(resp, url)
            return resp, url
        url = str(resp.next_request.url)
    raise ValueError(f"more than {_MAX_REDIRECTS} redirects, giving up at {url}")


class WebFetchArgs(BaseModel):
    url: str


@tool(WebFetchArgs, cls="atomic", sensitive=True)
def web_fetch(url: str) -> str:
    """Read one web page and return its text, boilerplate stripped and
    truncated. Call this on a promising search result instead of guessing from
    its snippet. The first line is `Source: <url>`, which is what to cite."""
    if host_matches(url, *_YT_DOMAINS):
        return _youtube_transcript(url)
    resp, final_url = _fetch(url)
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype and not ctype.startswith(_TEXT_TYPES):
        raise ValueError(f"refusing to read {ctype} content at {final_url}")
    # ponytail: no charset sniffing, httpx already decoded from the header.
    body = resp.text
    text = _extract_text(body) if ("html" in ctype or not ctype) else body
    return _render(final_url, _truncate(text))


_VTT_TAG_RE = re.compile(r"<[^>]*>")
_VTT_NOISE = ("WEBVTT", "Kind:", "Language:", "NOTE ", "STYLE", "REGION")


def _vtt_to_text(vtt: str) -> str:
    """VTT to plain lines: drop cue timings and inline markup, and collapse the
    rolling duplication auto-subs produce (each cue repeats the line before it).

    ponytail: adjacent-equal dedup, not a diff of overlapping cues. It clears
    the common rolling case; upgrade to longest-common-suffix trimming only if
    real transcripts come out visibly doubled.
    """
    lines: list[str] = []
    for raw in vtt.splitlines():
        s = raw.strip()
        if not s or "-->" in s or s.isdigit() or s.startswith(_VTT_NOISE):
            continue
        s = unescape(_VTT_TAG_RE.sub("", s)).strip()
        if s and (not lines or lines[-1] != s):
            lines.append(s)
    return "\n".join(lines)


def _youtube_transcript(url: str) -> str:
    """Subtitles via yt-dlp, keyless.

    There is no shortcut worth trying: the watch page does carry
    `captionTracks`, but every `baseUrl` now returns HTTP 200 with 0 bytes in
    every format, because timedtext is gated behind a PO token. yt-dlp handles
    the token and player-client dance.

    Auto-subs are ASR output: transcription errors, no speaker labels. Anything
    nucleated from them inherits that noise, so the sources/ leaf matters more
    here than usual.

    ponytail: no installer and no doctor for one optional binary. A clear
    prescription at call time beats a health-check subsystem. If stale-venv
    shims start confusing users, the upgrade is Agent-Reach's probe taxonomy
    (missing / broken / timeout / error), not an installer.
    """
    exe = shutil.which("yt-dlp")
    if not exe:
        raise ValueError(
            "reading YouTube needs yt-dlp on PATH: install it with "
            '`python -m pip install -U "yt-dlp[default]"`. The watch page '
            "itself is a JavaScript shell with no transcript in the HTML."
        )
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "sub"
        proc = subprocess.run(
            [
                exe, "--skip-download", "--write-auto-sub", "--write-sub",
                "--sub-lang", _YT_SUB_LANGS, "--sub-format", "vtt",
                "--no-playlist", "--playlist-items", "1", "-o", str(out), "--", url,
            ],
            capture_output=True,
            text=True,
            timeout=_YT_TIMEOUT,
        )
        files = sorted(Path(tmp).glob("*.vtt"))
        if not files:
            tail = (proc.stderr or proc.stdout or "").strip()[-300:]
            raise ValueError(
                f"yt-dlp found no subtitles for {url}" + (f": {tail}" if tail else "")
            )
        text = _vtt_to_text(files[0].read_text(encoding="utf-8", errors="replace"))
    return _render(url, _truncate(text))
