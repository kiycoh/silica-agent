# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""OAuth for HTTP MCP servers: the 2.x SDK's provider over the Class B store.

`OAuthClientProvider` does discovery, registration, PKCE, the `state` and
`iss` checks (RFC 9207, the mix-up defence the 1.x client lacks) and refresh
(ADR-0091 point 3). What is Silica's:

- where the grant is kept: the keyring, or the opted-in file, under the
  server's scope (`mcp/<key>`), never the environment (ADR-0089 point 7);
- when the OAuth flow may run at all: discovery, registration and the
  browser happen only during a login the user asked for. Outside one, a
  stored grant is presented (and refreshed) and nothing more: a 401 fails
  as "needs login", the grant stays stored, and the row offers Log in;
- the browser tab of a login says how it ended, written once the client has
  checked the answer (`state`, `iss`) and exchanged the code;
- a configured `oauth.clientId` is pinned to the issuer it is first used
  with; a later issuer is an error naming both, never the ID sent elsewhere;
- the callback: a one-shot listener on 127.0.0.1 at a fixed port,
  `oauth.callbackPort` to override it.

Registration follows the spec's order: a configured `oauth.clientId`, then
CIMD when a URL is set and the server advertises it, then dynamic
registration, then a client ID the user pastes (`client_id` in the block).
No CIMD URL ships (the owner left it open, ADR-0091 "Open for the owner" 2):
without one the SDK falls back to dynamic registration by itself.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import threading
import time
import webbrowser
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from silica.connectors import credentials, mcp_config

logger = logging.getLogger(__name__)

# Fixed, so a server that pins redirect URIs at registration keeps matching
# across logins; outside the ephemeral range Linux hands out (32768-60999).
DEFAULT_CALLBACK_PORT = 18323
LOGIN_TIMEOUT_S = 300.0
# How long the browser tab waits for the login's outcome before it is told to
# look at the app instead: the token exchange is one request, so this is far
# past a working server, and a tab is never left loading.
PAGE_WAIT_S = 30.0
_TOKENS, _CLIENT = mcp_config.OAUTH_FIELDS


class NeedsLogin(RuntimeError):
    """The server wants an authorization the user has not given yet."""


class IssuerMoved(RuntimeError):
    """A hand-registered client ID met an authorization server other than the
    one it was first used with; the message names both."""


@dataclass
class _Login:
    url: str = ""
    error: str = ""
    done: bool = False


_LOGINS: dict[str, _Login] = {}


class KeyringTokenStorage:
    """The SDK's TokenStorage over the server's scope in the Class B store.

    Keyed by the mcpServers key; the issuer half of the key is the stamp on
    the stored client registration, which the SDK compares on every discovery
    and discards when the authorization server changed (SEP-2352), so a
    server that moves re-registers, in the next login (discovery runs in no
    other). A hand-registered ID gets its stamp from _pinning_provider."""

    def __init__(self, key: str, on_tokens: Callable[[], None] | None = None) -> None:
        self.scope = mcp_config.scope(key)
        self.on_tokens = on_tokens  # told once a grant is saved

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = credentials.get(self.scope, _TOKENS)
        return OAuthToken.model_validate_json(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        credentials.put(self.scope, _TOKENS, tokens.model_dump_json())
        if self.on_tokens is not None:
            self.on_tokens()

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = credentials.get(self.scope, _CLIENT)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        credentials.put(self.scope, _CLIENT, client_info.model_dump_json())


def callback_port(spec: dict[str, Any]) -> int:
    oauth = spec.get("oauth") or {}
    try:
        return int(oauth.get("callbackPort") or DEFAULT_CALLBACK_PORT)
    except (TypeError, ValueError):
        # A hand-edited port that is not a number: the fixed one still works,
        # and a login that cannot bind it says so itself.
        return DEFAULT_CALLBACK_PORT


class _Page:
    """The browser tab of one login: answered with how the login ended, not
    with what the redirect carried. Before this the tab read "Silica is
    connected" for a redirect whose `iss` the client then refused (tester,
    2026-10-12)."""

    def __init__(self) -> None:
        self.outcome: asyncio.Future | None = None

    def open(self) -> asyncio.Future:
        self.outcome = asyncio.get_running_loop().create_future()
        return self.outcome

    def answer(self, ok: bool, why: str = "") -> None:
        if self.outcome is not None and not self.outcome.done():
            self.outcome.set_result((ok, why))


async def _page_text(outcome: asyncio.Future | None) -> str:
    if outcome is None:
        return "Silica got the authorization. You can close this tab."
    try:
        ok, why = await asyncio.wait_for(asyncio.shield(outcome), PAGE_WAIT_S)
    except TimeoutError:
        return ("Silica got an answer but has not finished the login: "
                "Settings › Connectors in Silica says how it ended. You can close this tab.")
    if ok:
        return "Silica is connected. You can close this tab."
    return (f"Silica refused this login: {why}. You can close this tab; "
            "Settings › Connectors in Silica shows the server's state.")


async def wait_for_callback(port: int, timeout: float, outcome: asyncio.Future | None = None) -> Any:
    """Serve one GET of the redirect on 127.0.0.1:`port` and return what it
    carried. The tab is answered when `outcome` resolves to (ok, why), after
    the flow checked the redirect and exchanged the code; without one, at once."""
    from mcp.shared.auth import AuthorizationCodeResult

    got: asyncio.Future = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await reader.readline()).decode("latin-1")
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass  # the headers carry nothing the flow needs
            target = line.split(" ")[1] if len(line.split(" ")) > 1 else "/"
            query = {k: v[0] for k, v in parse_qs(urlsplit(target).query).items()}
            ok = "code" in query
            if not got.done() and (ok or "error" in query):
                got.set_result(query)
            body = (await _page_text(outcome) if ok
                    else f"Silica did not get an authorization: {query.get('error', 'no code')}.")
            writer.write(("HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
                          f"Content-Length: {len(body.encode())}\r\nConnection: close\r\n\r\n{body}")
                         .encode())
            with suppress(ConnectionError):
                # A tab closed while the login finished: nobody left to read it.
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    try:
        query = await asyncio.wait_for(got, timeout)
    except asyncio.TimeoutError:
        raise NeedsLogin(f"no authorization arrived within {timeout:g} s") from None
    finally:
        # Stop listening, without `async with server`: its exit waits for the
        # open connection, which waits for the outcome this return leads to.
        server.close()
    if "code" not in query:
        raise NeedsLogin(f"authorization refused: {query.get('error_description') or query.get('error')}")
    return AuthorizationCodeResult(code=query["code"], state=query.get("state"), iss=query.get("iss"))


@functools.cache
def _pinning_provider() -> type:
    """The SDK's provider, with a hand-registered client ID pinned to its issuer.

    The SDK binds a registration it made to the issuer it made it with
    (SEP-2352) and leaves one without a recorded issuer unbound: after the
    authorization server moved, a login sent `client_id=manual-1` to the new
    one (tester, 2026-10-12). The spec says to key client credentials by
    issuer and to surface an error rather than use mismatched ones, so the ID
    is stamped with the issuer at its first discovery, and a later one raises.
    `_expected_issuer` is where the SDK decides that issuer, right before its
    own binding check; private API, under the same `<3` pin as the rest."""
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.auth.utils import issuers_match

    class PinningProvider(OAuthClientProvider):
        pinned_id = ""

        def _expected_issuer(self) -> str:
            issuer = super()._expected_issuer()
            info = self.context.client_info
            if not self.pinned_id or info is None or info.client_id != self.pinned_id:
                return issuer
            if info.issuer is None:
                info.issuer = issuer
                credentials.put(self.context.storage.scope, _CLIENT, info.model_dump_json())
            elif not issuers_match(info.issuer, issuer):
                raise IssuerMoved(
                    f"the authorization server moved from {info.issuer} to {issuer}, and the client "
                    f"ID {info.client_id} was registered with {info.issuer}: register Silica with "
                    f"{issuer} and paste the client ID it gives")
            return issuer

    return PinningProvider


def provider(key: str, spec: dict[str, Any], clients: Any) -> Any:
    """The httpx2 auth for one HTTP server's session."""
    from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata

    oauth = spec.get("oauth") or {}
    port = callback_port(spec)
    redirect = f"http://127.0.0.1:{port}/callback"
    scopes = oauth.get("scopes")
    metadata = OAuthClientMetadata(
        client_name="Silica", redirect_uris=[redirect],
        grant_types=["authorization_code", "refresh_token"], response_types=["code"],
        token_endpoint_auth_method="none",
        scope=" ".join(scopes) if isinstance(scopes, list) else (scopes or None),
    )
    page = _Page()
    storage = KeyringTokenStorage(key, on_tokens=lambda: page.answer(True))
    client_id = str(oauth.get("clientId") or "").strip()
    if client_id and _stored_client_id(storage.scope) != client_id:
        # Pre-registered: first in the spec's order, and it replaces another
        # registration (a pasted ID is the user's word for this server now).
        # Its issuer is recorded at the first discovery (_pinning_provider).
        info = OAuthClientInformationFull(client_id=client_id, redirect_uris=[redirect],
                                          token_endpoint_auth_method="none")
        try:
            credentials.put(storage.scope, _CLIENT, info.model_dump_json())
        except credentials.NoStore as exc:
            # Not stored now is not lost: the SDK then registers in the login,
            # whose own save meets the same refusal and reports it on the row.
            logger.info("mcp %s: client id not stored: %s", key, exc)

    async def redirect_handler(url: str) -> None:
        if key not in clients.interactive_login:
            raise NeedsLogin("needs login")
        state = _LOGINS.setdefault(key, _Login())
        state.url = url
        try:
            webbrowser.open(url)
        except Exception as exc:
            # No browser on this machine (a server, an SSH session): the URL
            # is printed and shown, which is what /connect login is for.
            logger.info("no browser for %s: %s", key, exc)

    async def callback_handler():
        return await wait_for_callback(port, LOGIN_TIMEOUT_S, page.open())

    inner = _pinning_provider()(server_url=spec["url"], client_metadata=metadata, storage=storage,
                                redirect_handler=redirect_handler, callback_handler=callback_handler)
    inner.pinned_id = client_id
    return _gated(inner, key, clients, page)


def _stored_client_id(scope: str) -> str:
    raw = credentials.get(scope, _CLIENT)
    if not raw:
        return ""
    try:
        return str(json.loads(raw).get("client_id") or "")
    except (ValueError, AttributeError):
        return ""  # a torn record: the configured ID replaces it


def _gated(inner: Any, key: str, clients: Any, page: _Page | None = None) -> Any:
    """The provider, with the OAuth flow kept inside a login the user asked for.

    Outside one (the session warm-up, Reload, a call mid-turn) a stored grant
    is presented and refreshed as usual, and a 401 to the request itself is
    "needs login" at once: the SDK's flow would go on to discover, register a
    client with the service and overwrite the stored registration before it
    ever reached the browser it may not open. Seen after the issuer moved
    (tester, 2026-10-12): a `register` with no browser open. The grant stays
    stored; the row offers Log in."""
    import httpx2

    def refused(response: Any) -> bool:
        # The two answers on which the SDK starts its flow: a 401, and a 403
        # asking for more scope (a step-up is a new authorization too).
        if response.status_code == 401:
            return True
        return response.status_code == 403 and "insufficient_scope" in response.headers.get(
            "www-authenticate", "")

    class GatedAuth(httpx2.Auth):
        requires_response_body = True

        async def async_auth_flow(self, request):
            in_login = key in clients.interactive_login
            flow = inner.async_auth_flow(request)
            try:
                outgoing = await flow.__anext__()
                while True:
                    response = yield outgoing
                    if not in_login and outgoing is request and refused(response):
                        raise NeedsLogin("needs login")
                    outgoing = await flow.asend(response)
            except StopAsyncIteration:
                return
            except Exception as exc:
                if in_login and page is not None:
                    page.answer(False, _reason(exc))
                raise
            finally:
                await flow.aclose()

    auth = GatedAuth()
    auth.context = inner.context  # what a test reads (no CIMD URL ships)
    return auth


def _reason(exc: BaseException) -> str:
    from silica.kernel.scrub import scrub_credentials

    return scrub_credentials(str(exc) or type(exc).__name__)


class _QuietNeedsLogin(logging.Filter):
    """The SDK logs every failed OAuth flow with its traceback; "needs login"
    is the expected outcome of a call outside a login, not an error."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, NeedsLogin)


logging.getLogger("mcp.client.auth.oauth2").addFilter(_QuietNeedsLogin())


def has_tokens(key: str) -> bool:
    """A grant is stored; whether the server still takes it is the row's
    state, not this (a background open that meets a 401 leaves it stored)."""
    return credentials.get(mcp_config.scope(key), _TOKENS) is not None


def login(key: str, *, wait_s: float = 15.0) -> dict[str, Any]:
    """Start a login for an HTTP server: open its session with the browser
    allowed. Returns as soon as the authorization URL is known (or the session
    opened on stored tokens); the session finishes in the background when the
    callback lands, and the row turns `connected`."""
    from silica.connectors.mcp_client import CLIENTS, CallFailed

    entry = mcp_config.load().get(key)
    if entry is None:
        raise mcp_config.ConfigError(f"no server named '{key}'")
    if entry.kind != "http":
        raise mcp_config.ConfigError(f"'{key}' is a stdio server; only http servers log in")
    CLIENTS.close(key)
    state = _LOGINS[key] = _Login()
    CLIENTS.interactive_login.add(key)

    def run() -> None:
        try:
            CLIENTS.list_tools(key, timeout=LOGIN_TIMEOUT_S + 30)
        except CallFailed as exc:
            state.error = str(exc)
        finally:
            state.done = True
            CLIENTS.interactive_login.discard(key)

    threading.Thread(target=run, name=f"mcp-login-{key}", daemon=True).start()
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline and not (state.url or state.done):
        time.sleep(0.05)
    if state.error:
        raise CallFailed(state.error)
    return {"url": state.url, "connected": state.done and not state.error}


def login_state(key: str) -> dict[str, Any]:
    state = _LOGINS.get(key)
    return {} if state is None else {"url": state.url, "error": state.error, "done": state.done}


def logout(key: str) -> None:
    """Drop the tokens; the client registration stays, so the next login
    does not register again."""
    from silica.connectors.mcp_client import CLIENTS

    CLIENTS.close(key)
    credentials.delete(mcp_config.scope(key), _TOKENS)
