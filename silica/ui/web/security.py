# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Who may reach the GUI: the name a request is addressed to, and the page a
write comes from. Nothing here knows a route, so every router sits above it."""
from __future__ import annotations

import os
from urllib.parse import urlsplit as _urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _authority(value: str) -> tuple[str, str]:
    """`host[:port]` split into (host, port), with the loopback spellings folded
    into one host — the browser repeats whatever the user typed, and the server
    was reached on the loopback interface either way."""
    raw = value.strip().lower()
    host, _, port = raw.rpartition(":")
    if not host or "]" in port:  # no port at all, or a bare IPv6 literal
        host, port = raw, ""
    return ("loopback" if host in _LOOPBACK_HOSTS else host), port


def require_same_origin(request: Request) -> None:
    """Refuse a state-changing request another origin made on the user's behalf.

    The GUI has no auth (localhost, one user), so it answers with the browser's
    ambient authority, and a multipart POST is CORS-safelisted: it crosses
    origins with no preflight. Any page the user happened to visit could
    therefore fetch() an upload into the vault Inbox and run a whole agent turn
    with the write tools. A browser always sends Origin on a cross-origin POST,
    so checking it closes that while a client that sends no Origin at all (curl,
    the tests) keeps working.
    """
    site = (request.headers.get("sec-fetch-site") or "").lower()
    if site and site not in ("same-origin", "none"):
        raise HTTPException(403, "cross-origin request refused")
    origin = request.headers.get("origin")
    if origin is None:
        return
    parsed = _urlsplit(origin)
    if parsed.scheme not in ("http", "https") or _authority(parsed.netloc) != _authority(
        request.headers.get("host", "")
    ):
        raise HTTPException(403, "cross-origin request refused")


def same_origin_writes(request: Request) -> None:
    """The app's one origin rule: every method that can change something is
    checked, on every router, with no route left to remember it. A Depends and
    not a middleware, so an unknown path is still a 404 and a wrong method a 405."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        require_same_origin(request)



# The names this server answers to. It binds 127.0.0.1 only (serve()), so a
# browser that reached it legitimately typed a loopback name. A page on any
# other name got here by DNS rebinding (its name re-resolved to 127.0.0.1), and
# Origin cannot tell: the page sets Origin and Host to its own name alike
# (audit 2026-10-09, S3). Tests add TestClient's "testserver" in conftest.
# A hosted tenant also answers to the public names its unit lists in
# SILICA_GUI_HOSTS: the proxy forwards Host unchanged (ADR-0085 point 4), and
# the socket it listens on is reachable by the proxy alone (serve()). An
# environment variable and not a setting, so the GUI can never widen itself.
def _public_hosts() -> set[str]:
    return {h.strip().lower() for h in os.environ.get("SILICA_GUI_HOSTS", "").split(",") if h.strip()}


_ALLOWED_HOSTS = {"loopback", *_public_hosts()}


async def hosted_image_policy(request: Request, call_next):
    """On the server, the page draws only its own images (ADR-0098 H6): the
    layer under `_linkify(remote_images=False)`, and the one that also covers
    notes, the drawer and the editor's preview. The page loads nothing from
    another origin, so only web images inside notes stop drawing; locally
    they still draw (the ADR's open item 3). A response with its own policy
    (`/asset`'s sandbox) keeps it."""
    from silica.config import hosted

    response = await call_next(request)
    if hosted() and "content-security-policy" not in response.headers:
        response.headers["Content-Security-Policy"] = "img-src 'self' data: blob:"
    return response


async def withheld_views(request: Request, call_next):
    """A view this account does not get answers as a route that does not exist
    (ADR-0140 point 4), and so does what a demo refuses (ADR-0139 point 6).
    The page hides the control, but a request does not come from the page."""
    from silica.config import withheld

    path = request.url.path
    if any(path == r or path.startswith(r + "/") for r in withheld("routes")):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await call_next(request)


async def require_loopback_host(request: Request, call_next):
    if _authority(request.headers.get("host", ""))[0] not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": "unknown host refused"}, status_code=403)
    return await call_next(request)
