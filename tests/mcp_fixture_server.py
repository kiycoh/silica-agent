"""A synthetic MCP server on the 2.x SDK, for the client's tests (ADR-0091).

No account behind it: every tool answers from its arguments, its own
environment or a counter file. Run as a script it serves stdio (what a
`command` entry in ~/.silica/mcp.json starts); `--http PORT` serves Streamable
HTTP instead, and `--tools N` pads the listing with N inert tools, the shape
of a 15-tool server Gates 1 and 3 ask for.

    python tests/mcp_fixture_server.py [--http PORT] [--tools N] [--calls FILE]

`--calls FILE` appends one JSON line per call, so a test can tell a call that
reached the server from one the client refused before sending; with `--oauth`
the authorization server logs each registration and authorization there too.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server

_STR = {"type": "string"}

_TOOLS: list[dict[str, Any]] = [
    {"name": "search_threads", "description": "Search mail threads by words; returns subjects.",
     "input_schema": {"type": "object", "properties": {"query": _STR}, "required": ["query"]}},
    {"name": "send_message", "description": "Send a mail message to one address.",
     "input_schema": {"type": "object", "properties": {"to": _STR, "body": _STR},
                      "required": ["to", "body"]},
     # An honest hint: a sent mail is an additive update (ADR-0091 Context).
     "annotations": {"destructiveHint": False, "readOnlyHint": False}},
    {"name": "env_probe", "description": "Report which environment variables this server sees.",
     "input_schema": {"type": "object", "properties": {"names": {"type": "array", "items": _STR}}}},
    {"name": "fail", "description": "Always fails, naming a URL with a token in it.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "big", "description": "Returns a very long text with degenerate runs.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "picture", "description": "Returns an image and nothing else.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "structured", "description": "Returns structured content and no text.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "slow", "description": "Sleeps, then answers.",
     "input_schema": {"type": "object", "properties": {"seconds": {"type": "number"}}}},
    {"name": "with_defs", "description": "A schema whose parameter is a local $defs reference.",
     "input_schema": {"type": "object", "properties": {"label": {"$ref": "#/$defs/Label"}},
                      "$defs": {"Label": {"type": "string", "enum": ["a", "b"]}}}},
    {"name": "nested_defs", "description": "A schema whose $defs reference another $def.",
     "input_schema": {"type": "object", "properties": {"x": {"$ref": "#/$defs/Outer"}},
                      "$defs": {"Outer": {"type": "object",
                                          "properties": {"y": {"$ref": "#/$defs/Inner"}}},
                                "Inner": {"type": "string"}}}},
    {"name": "remote_ref", "description": "A schema with a remote $ref.",
     "input_schema": {"type": "object",
                      "properties": {"x": {"$ref": "https://example.org/schema.json"}}}},
    {"name": "bad name!", "description": "A name the OpenAI function grammar refuses.",
     "input_schema": {"type": "object", "properties": {}}},
]


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


# A mailbox of 15 tools, six reads and nine writes: the shape of a real mail
# server, for ADR-0091's Gates 1 and 3 (one server of 15 or more tools).
MAILBOX_READS = ("search_threads", "get_thread", "list_labels", "list_contacts",
                 "get_attachment", "get_profile")
MAILBOX_WRITES = ("send_message", "create_draft", "reply_to_thread", "forward_message",
                  "archive_thread", "delete_thread", "mark_read", "add_label", "remove_label")
_ID = {"type": "string", "description": "A thread id, as search_threads returns it"}
_MAILBOX: list[dict[str, Any]] = [
    {"name": "search_threads", "description": "Search mail threads by words, sender or label; "
     "returns thread ids and subjects.",
     "input_schema": _obj({"query": _STR, "max_results": {"type": "integer"}}, ["query"])},
    {"name": "get_thread", "description": "Read one thread: every message's sender, date and body.",
     "input_schema": _obj({"thread_id": _ID}, ["thread_id"])},
    {"name": "list_labels", "description": "List the mailbox's labels.", "input_schema": _obj({})},
    {"name": "list_contacts", "description": "Find contacts by name; returns names and addresses.",
     "input_schema": _obj({"name": _STR}, ["name"])},
    {"name": "get_attachment", "description": "Download one attachment of a message as text.",
     "input_schema": _obj({"thread_id": _ID, "filename": _STR}, ["thread_id", "filename"])},
    {"name": "get_profile", "description": "The mailbox owner's address and display name.",
     "input_schema": _obj({})},
    {"name": "send_message", "description": "Send a new mail message.",
     "input_schema": _obj({"to": _STR, "subject": _STR, "body": _STR}, ["to", "subject", "body"])},
    {"name": "create_draft", "description": "Save a draft without sending it.",
     "input_schema": _obj({"to": _STR, "subject": _STR, "body": _STR}, ["to", "body"])},
    {"name": "reply_to_thread", "description": "Send a reply in an existing thread.",
     "input_schema": _obj({"thread_id": _ID, "body": _STR}, ["thread_id", "body"])},
    {"name": "forward_message", "description": "Forward a thread's last message to an address.",
     "input_schema": _obj({"thread_id": _ID, "to": _STR, "note": _STR}, ["thread_id", "to"])},
    {"name": "archive_thread", "description": "Archive a thread (remove it from the inbox).",
     "input_schema": _obj({"thread_id": _ID}, ["thread_id"])},
    {"name": "delete_thread", "description": "Move a thread to the trash.",
     "input_schema": _obj({"thread_id": _ID}, ["thread_id"])},
    {"name": "mark_read", "description": "Mark a thread read or unread.",
     "input_schema": _obj({"thread_id": _ID, "read": {"type": "boolean"}}, ["thread_id"])},
    {"name": "add_label", "description": "Add a label to a thread.",
     "input_schema": _obj({"thread_id": _ID, "label": _STR}, ["thread_id", "label"])},
    {"name": "remove_label", "description": "Remove a label from a thread.",
     "input_schema": _obj({"thread_id": _ID, "label": _STR}, ["thread_id", "label"])},
]

_THREADS = {
    "t-101": ("Thesis draft, chapter 2", "prof.rossi@uni.example", "Please send chapter 2 by Friday."),
    "t-102": ("Lab meeting moved to Thursday", "lab@uni.example", "The meeting is now Thursday 10:00."),
    "t-103": ("Invoice October", "billing@host.example", "Your invoice is attached: invoice-oct.pdf."),
}


def _mailbox_answer(name: str, args: dict[str, Any]) -> str:
    if name == "search_threads":
        q = str(args.get("query") or "").lower()
        hits = [f"{tid}: {subj} (from {who})" for tid, (subj, who, _b) in _THREADS.items()
                if not q or any(w in (subj + who).lower() for w in q.split())]
        return "\n".join(hits) or "no thread matches"
    if name == "get_thread":
        subj, who, body = _THREADS.get(str(args.get("thread_id")), ("?", "?", "no such thread"))
        return f"Subject: {subj}\nFrom: {who}\n\n{body}"
    if name == "list_labels":
        return "Inbox, Thesis, Lab, Bills"
    if name == "list_contacts":
        return f"{args.get('name')}: ann@example.org"
    if name == "get_attachment":
        return "invoice-oct.pdf: total 42.00 EUR"
    if name == "get_profile":
        return "me@uni.example (Alex)"
    return f"{name} done"


def _text(s: str, *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=s)], is_error=error)


def build(extra_tools: int = 0, calls_file: str = "", mailbox: bool = False) -> Server:
    tools = list(_MAILBOX if mailbox else _TOOLS) + [
        {"name": f"inert_{i:02d}", "description": f"Inert tool number {i}; returns its number.",
         "input_schema": {"type": "object", "properties": {}}}
        for i in range(extra_tools)
    ]

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[types.Tool.model_validate(t) for t in tools])

    async def on_call_tool(ctx, params) -> types.CallToolResult:
        name, args = params.name, params.arguments or {}
        if calls_file:
            with open(calls_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"name": name, "args": args}) + "\n")
        if mailbox:
            return _text(_mailbox_answer(name, args))
        if name == "search_threads":
            return _text(f"2 threads match {args.get('query')!r}: 'Thesis draft', 'Lab meeting'")
        if name == "send_message":
            return _text(f"sent to {args.get('to')}")
        if name == "env_probe":
            names = args.get("names") or []
            return _text(json.dumps({n: os.environ.get(n) for n in names}))
        if name == "fail":
            return _text("upstream said 401 at https://api.example.org/v1?token=SECRET123", error=True)
        if name == "big":
            return _text("z" * 50 + " head " + "x" * 40_000)
        if name == "picture":
            return types.CallToolResult(content=[types.ImageContent(
                type="image", data="iVBORw0KGgo=", mime_type="image/png")])
        if name == "structured":
            return types.CallToolResult(content=[], structured_content={"count": 3})
        if name == "slow":
            await anyio.sleep(float(args.get("seconds") or 0))
            return _text("slept")
        if name.startswith("inert_"):
            return _text(name.split("_", 1)[1])
        return _text(f"called {name}")

    return Server("fixture", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


class _MemoryAuthServer:
    """An authorization server that approves every request at once: the
    browser step of a login is one redirect straight to the callback. Enough
    to drive the client's discovery, dynamic registration, PKCE, state and
    token exchange; nothing here is a real account."""

    def __init__(self, log: str = "", bad_iss: str = "") -> None:
        # `log` gets one JSON line per registration and authorization, so a
        # test can tell a background open that registered from one that did
        # not; `bad_iss` answers every authorization with that `iss`, the
        # mix-up RFC 9207 makes the client refuse.
        self.log, self.bad_iss = log, bad_iss
        self.clients: dict[str, Any] = {}
        self.codes: dict[str, Any] = {}
        self.tokens: dict[str, Any] = {}
        self.refresh: dict[str, Any] = {}

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    def _note(self, what: str, **kw) -> None:
        if self.log:
            with open(self.log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"as": what, **kw}) + "\n")

    async def register_client(self, client_info) -> None:
        self._note("register", client_id=client_info.client_id)
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client, params) -> str:
        import secrets

        from mcp.server.auth.provider import AuthorizationCode, construct_redirect_uri

        self._note("authorize", client_id=client.client_id)
        code = secrets.token_hex(8)
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=time.time() + 300,
            client_id=client.client_id, code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource)
        extra = {"iss": self.bad_iss} if self.bad_iss else {}
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state, **extra)

    async def load_authorization_code(self, client, authorization_code):
        return self.codes.get(authorization_code)

    async def exchange_authorization_code(self, client, authorization_code):
        import secrets

        from mcp.server.auth.provider import AccessToken, RefreshToken
        from mcp.shared.auth import OAuthToken

        self.codes.pop(authorization_code.code, None)
        access, refresh = secrets.token_hex(16), secrets.token_hex(16)
        self.tokens[access] = AccessToken(token=access, client_id=client.client_id,
                                          scopes=authorization_code.scopes,
                                          expires_at=int(time.time()) + 3600,
                                          resource=authorization_code.resource)
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client.client_id,
                                             scopes=authorization_code.scopes)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=3600,
                          refresh_token=refresh, scope=" ".join(authorization_code.scopes) or None)

    async def load_refresh_token(self, client, refresh_token):
        return self.refresh.get(refresh_token)

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        raise NotImplementedError  # the tests never let a token expire

    async def load_access_token(self, token):
        return self.tokens.get(token)

    async def revoke_token(self, token) -> None:
        self.tokens.pop(getattr(token, "token", ""), None)


def _oauth_app(server: Server, port: int, manual_client: str = "", manual_redirect: str = "",
               issuer: str = "", log: str = "", bad_iss: str = ""):
    """`manual_client` turns dynamic registration off and knows one client
    registered by hand: the spec's last registration step, a pasted client ID.
    `issuer` names the authorization server otherwise than by this server's
    own origin (`http://localhost:PORT` for the same socket): restarted with
    another one, the server has moved to a new issuer."""
    from mcp.server.auth.provider import ProviderTokenVerifier
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
    from mcp.shared.auth import OAuthClientInformationFull

    base = f"http://127.0.0.1:{port}"
    provider = _MemoryAuthServer(log, bad_iss)
    if manual_client:
        provider.clients[manual_client] = OAuthClientInformationFull(
            client_id=manual_client, redirect_uris=[manual_redirect],
            token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"])
    return server.streamable_http_app(
        auth=AuthSettings(issuer_url=issuer or base, resource_server_url=f"{base}/mcp",
                          client_registration_options=ClientRegistrationOptions(enabled=not manual_client),
                          validate_token_resource=False),
        token_verifier=ProviderTokenVerifier(provider), auth_server_provider=provider)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", type=int, default=0)
    ap.add_argument("--oauth", action="store_true")
    ap.add_argument("--mailbox", action="store_true")
    ap.add_argument("--manual-client", default="")
    ap.add_argument("--manual-redirect", default="")
    ap.add_argument("--tools", type=int, default=0)
    ap.add_argument("--calls", default="")
    ap.add_argument("--start-delay", type=float, default=0.0)
    # A server that never answers its handshake, and one that also ignores
    # SIGTERM: the client's close must still leave no process behind.
    ap.add_argument("--hang", action="store_true")
    ap.add_argument("--ignore-term", action="store_true")
    ap.add_argument("--pidfile", default="")
    ap.add_argument("--issuer", default="")
    ap.add_argument("--bad-iss", default="")
    a = ap.parse_args()
    if a.pidfile:
        with open(a.pidfile, "a", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}\n")
    if a.ignore_term:
        import signal

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
    if a.hang:
        time.sleep(3600)
    if a.start_delay:
        time.sleep(a.start_delay)
    server = build(a.tools, a.calls, a.mailbox)
    if a.http:
        import uvicorn
        app = (_oauth_app(server, a.http, a.manual_client, a.manual_redirect, a.issuer, a.calls,
                          a.bad_iss) if a.oauth
               else server.streamable_http_app())
        uvicorn.run(app, host="127.0.0.1", port=a.http, log_level="warning")
        return

    from mcp.server.stdio import stdio_server

    async def serve() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(serve)


if __name__ == "__main__":
    sys.exit(main())
