# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A Silica on a computer linked to a hosted account (ADR-0157 points 4, 9, 10 and 11): the code
the person confirms on the gateway, the token that comes back, and the one thing it does, send a
wiki's notes to the account, which builds the pages and publishes them (`sources.linked_publish`,
which reads a wiki file's cap from above this layer). Unlinked, a computer only saves wikis as files.

Named account_link and not link: silica.kernel.link is the wikilink package.

The token publishes in the person's name for 90 days, so it lives in one file, 0600, and goes to
the host that issued it alone: it is in no log line, no exception, no answer to the page and not
in `state()`. Every call to the gateway is the app server's, never the browser's: the gateway
refuses a fetch from a page on localhost (silica-hosted bin/gateway, `same_origin`)."""
from __future__ import annotations

import datetime
import json
import logging
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

from silica.config import live_env, silica_home

logger = logging.getLogger(__name__)

DEFAULT_HOST = "https://app.silica-agent.dev"
# the link's life, the owner's (ADR-0157 Open 1), as the gateway's LINK_LIFE
LIFE = datetime.timedelta(days=90)
NOT_LINKED = "Link a Silica account to publish."
# The gateway hands out token_urlsafe(32), 43 characters. The bound keeps a hand-written file from
# putting into a header what no proxy carries; the alphabet keeps a newline out of it.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,200}")
_CODE = re.compile(r"[A-Z0-9]{3}-[A-Z0-9]{3}")


def _origin(raw: str) -> str:
    """`raw` as scheme://host[:port], "" for an address a token must not travel to: https alone,
    since the token rides in a header, but for this machine's own, where a test's gateway listens."""
    try:
        u = urlsplit(raw.strip().rstrip("/"))
        _ = u.port  # raises on a port out of range
    except ValueError:
        return ""
    here = u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")
    if not (u.scheme == "https" or here) or not u.hostname or u.username is not None \
            or u.path or u.query or u.fragment:
        return ""
    return f"{u.scheme}://{u.netloc.lower()}"


def host() -> str:
    """The gateway a new link goes to; "" when SILICA_LINK_HOST names one it must not."""
    return _origin(live_env("SILICA_LINK_HOST") or DEFAULT_HOST)


def _path() -> Path:
    # read per call: a tenant's and the tests' _SILICA_HOME move, and a path bound at import would not
    return silica_home() / "link.json"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _keep(token: str, handle: str, at: str) -> None:
    """Write the link as the gateway gave it. Through a 0600 temporary file and a rename, so the
    token is never readable by another account, nor half written."""
    from silica.kernel.base.durable import atomic_write_bytes

    now = _now()
    row = {"token": token, "handle": handle, "host": at, "linked_at": now.isoformat(),
           "expires": (now + LIFE).isoformat()}
    atomic_write_bytes(_path(), json.dumps(row).encode("utf-8"), mode=0o600)


def _read() -> dict | None:
    """The link the file holds, None when there is none to use: no file, one that does not parse
    or has another shape, one for a host a token must not go to, or one past its 90 days."""
    from silica.kernel.publish import HANDLE_RE

    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # no file is no link, and a file that does not parse is one no Publish can trust
        return None
    if not isinstance(raw, dict):
        return None
    token, at, handle = raw.get("token"), raw.get("host"), raw.get("handle")
    if not (isinstance(token, str) and _TOKEN.fullmatch(token) and isinstance(at, str) and at
            and _origin(at) == at):
        return None
    try:
        since = datetime.datetime.fromisoformat(raw["linked_at"])
        expires = datetime.datetime.fromisoformat(raw["expires"])
        if expires <= _now():
            return None
    except (KeyError, TypeError, ValueError):
        # a date missing, not one, or without its zone, which cannot be compared with now
        return None
    return {"token": token, "host": at, "handle": handle if isinstance(handle, str) and HANDLE_RE.fullmatch(handle)
            else "", "since": since.isoformat(), "expires": expires.isoformat()}


def state() -> dict:
    """{"linked", "host", "handle", "since", "expires"}, what the view shows; never the token."""
    got = _read()
    if got is None:
        return {"linked": False, "host": host(), "handle": "", "since": "", "expires": ""}
    return {"linked": True, **{k: got[k] for k in ("host", "handle", "since", "expires")}}


def unlink() -> None:
    """Forget the link on this computer. The account's Settings remove it on the host's side."""
    _path().unlink(missing_ok=True)
    _forget_sent()


def _forget_sent() -> None:
    """Forget what an account was told: the listing request each row's last Publish carried and a
    published date only a Publish set, so a link to another account reads as never sent."""
    from silica.config import CONFIG
    from silica.kernel import publish

    if not CONFIG.vault_path:
        return
    vault = Path(CONFIG.vault_path)
    try:
        items = publish.vaults(vault)
        if any(v["sent"] or v["sent_listed"] for v in items):
            for v in items:
                if v["sent"]:
                    v["published"] = ""
                v["sent"] = v["sent_listed"] = False
            publish._save_vaults(items, vault)
    except (OSError, ValueError) as exc:
        # The link is gone or new, which is what was asked for; only the view's "Asked" and
        # "Published" lines read one Publish late, until the next one (ValueError: vault.yaml not
        # UTF-8, or one that does not parse, which the manifest refuses to write over)
        logger.warning("account link: rows not cleared (%s)", type(exc).__name__)


def _linked_to() -> tuple[str, str] | None:
    """(host, handle) the link file names, past its 90 days too: a link renewed to the same account
    keeps what that account was told. None with no file, or one that cannot say."""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # no file, or one no Publish could use: nothing says which account the rows were sent to
        return None
    if not isinstance(raw, dict):
        return None
    host, handle = raw.get("host"), raw.get("handle")
    return (host, handle) if isinstance(host, str) and isinstance(handle, str) else None


def _named(at: str) -> str:
    return urlsplit(at).netloc or "Silica hosted"


def _post(url: str, at: str, **kw):
    import httpx

    try:
        # no redirect: one would take the Authorization header somewhere this code never chose
        return httpx.post(url, follow_redirects=False, **kw)
    except httpx.HTTPError as exc:
        # Its words name the address at most, and still never reach the page or the log: the class
        # is enough to tell a refused connection from a timeout.
        logger.warning("account link: %s not reached (%s)", url.split("?", 1)[0], type(exc).__name__)
    # Raised out here and not inside the except: there it would keep the httpx error as its
    # __context__, and that error holds the request, Authorization header included.
    raise ValueError(f"{_named(at)} could not be reached. Check the connection and try again.")


# The gateway's and the tenant's answers are a few hundred bytes; JSON grows as it parses (9 MB of
# [] held 192 MiB, 06c69d5), so a longer one is refused before the parse.
_ANSWER_MAX = 64 * 1024
# a code's life on the gateway (LINK_TTL): the page polls no longer than that, whatever is said
_CODE_LIFE = 600


def _body(r, at: str) -> dict:
    # shortcut: httpx has read the body whole before this check; stream the response if a gateway
    # ever answers megabytes, which no answer of bin/gateway or a tenant does (2026-11-08)
    if len(r.content) > _ANSWER_MAX:
        raise ValueError(f"{_named(at)} answered with more than Silica reads. Try again in a minute.")
    try:
        out = r.json()
    except ValueError:
        # An edge's HTML error page, or an empty body: no field is read from it, and each caller's
        # check of the status and the fields it needs says what went wrong in a sentence of its own
        return {}
    return out if isinstance(out, dict) else {}


def _gateway() -> str:
    at = host()
    if not at:
        raise ValueError("SILICA_LINK_HOST is not an https address, so this Silica cannot link to it.")
    return at


def start() -> dict:
    """A code for the person to confirm: {"code", "poll", "expires_in", "url"}, `url` being the
    page of the gateway that shows it (ADR-0157 point 9, at /link/confirm since Task 9's review).
    Whoever holds `poll` collects the token once the code is confirmed: the caller keeps it."""
    at = _gateway()
    r = _post(f"{at}/link/start", at, json={}, timeout=15)
    if r.status_code == 429:
        wait = r.headers.get("retry-after", "")
        when = f"in {max(1, -(-int(wait) // 60))} minutes" if wait.isdigit() else "later"
        raise ValueError(f"Too many link codes from this network. Try again {when}.")
    got = _body(r, at)
    code, poll, ttl = got.get("code"), got.get("poll"), got.get("expires_in")
    if r.status_code != 200 or not (isinstance(code, str) and _CODE.fullmatch(code) and isinstance(poll, str)
                                    and poll and type(ttl) is int and ttl > 0):
        raise ValueError(f"{_named(at)} gave no link code (it answered {r.status_code}). Try again in a minute.")
    return {"code": code, "poll": poll, "expires_in": min(ttl, _CODE_LIFE),
            "url": f"{at}/link/confirm?code={quote(code)}"}


def poll(poll: str) -> dict:
    """{"state": "pending"} or {"state": "gone"}; once the code is confirmed, the link kept and
    {"state": "linked", **state()}. The gateway hands the token out once, here, and it goes
    straight to the file: what this returns never holds it."""
    from silica.kernel.publish import HANDLE_RE

    at = _gateway()
    r = _post(f"{at}/link/poll", at, json={"poll": poll}, timeout=15)
    got = _body(r, at)
    said = got.get("state")
    if r.status_code != 200 or said not in ("pending", "linked", "gone"):
        raise ValueError(f"{_named(at)} did not say whether the code was confirmed "
                         f"(it answered {r.status_code}). Try again in a minute.")
    if said != "linked":
        return {"state": said}
    token, handle = got.get("token"), got.get("handle")
    if not (isinstance(token, str) and _TOKEN.fullmatch(token)):
        raise ValueError(f"{_named(at)} answered with a link this Silica cannot keep. Link again.")
    handle = handle if isinstance(handle, str) and HANDLE_RE.fullmatch(handle) else ""
    before = _linked_to()
    _keep(token, handle, at)
    if before != (at, handle):
        # another account, or one this computer cannot tell is the same: it holds none of the copies
        # the rows say were sent, and its "Turn off on your account" would upload them
        _forget_sent()
    logger.info("account link: this computer is linked to @%s on %s", handle or "?", at)
    if handle:
        _take_handle(handle)
    return {"state": "linked", **state()}


def _take_handle(handle: str) -> None:
    """The account's handle becomes this vault's, so My wikis never asks a linked computer for one
    and a wiki file saved here says the @handle its Publish goes out under."""
    from silica.config import CONFIG
    from silica.kernel.publish import PUBLIC_KEY
    from silica.kernel.base.manifest import set_block_key

    if not CONFIG.vault_path:
        return  # no vault open: nothing to name, and "." would put a vault.yaml in the working folder
    try:
        set_block_key(CONFIG.vault_path, PUBLIC_KEY, "handle", handle)
    except (OSError, ValueError) as exc:
        # A vault.yaml not writable, or not UTF-8 (UnicodeDecodeError is a ValueError): the link is
        # kept and works, and only the handle overlay asks again until vault.yaml is fixed
        logger.warning("account link: @%s not written into vault.yaml (%s)", handle, type(exc).__name__)

