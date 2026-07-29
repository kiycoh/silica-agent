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
from urllib.parse import urlsplit

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
