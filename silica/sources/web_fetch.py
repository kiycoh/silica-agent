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
import socket
from html.parser import HTMLParser
from urllib.parse import urlsplit

# ~7.5k tokens of a 60k default context budget. A LinkedIn guest page is 374 KB
# raw; without this ceiling one fetch eats the window.
_MAX_CHARS = 30_000

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
