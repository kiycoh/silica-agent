"""OAuth for HTTP MCP servers (ADR-0091 point 3), end to end against the
fixture's in-memory authorization server: discovery, dynamic registration,
PKCE, the state check and the token exchange are the 2.x SDK's; the grant
lands in the Class B store and a browser opens only during a login."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time

import httpx
import pytest

from tests.mcp_helpers import FIXTURE


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(*extra, port: int = 0):
    port = port or _free_port()
    proc = subprocess.Popen([sys.executable, FIXTURE, "--http", str(port), "--oauth", *extra],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/.well-known/oauth-authorization-server", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    return proc, f"http://127.0.0.1:{port}/mcp"


@pytest.fixture
def oauth_server():
    proc, url = _start()
    yield url
    proc.terminate()
    proc.wait(10)


@pytest.fixture(autouse=True)
def _clean():
    yield
    from silica.connectors import mcp_client, mcp_tools

    mcp_tools.unregister()
    mcp_client.CLIENTS.close_all()


def _configure(url: str, cb_port: int, **oauth) -> None:
    from silica.connectors import mcp_config

    mcp_config.path().parent.mkdir(parents=True, exist_ok=True)
    mcp_config.path().write_text(json.dumps({"mcpServers": {"remote": {
        "type": "http", "url": url, "oauth": {"callbackPort": cb_port, **oauth}}}}), encoding="utf-8")


def _approving_browser(monkeypatch, pages: list | None = None):
    """The user, approving: the authorization URL is fetched and its
    redirect followed to the callback listener; `pages` gets the text the
    browser tab ends on."""
    import threading
    import webbrowser

    opened = []

    def visit(url):
        r = httpx.get(url, follow_redirects=True, timeout=60)
        if pages is not None:
            pages.append(r.text)

    def open_(url, *a, **k):
        opened.append(url)
        threading.Thread(target=visit, args=(url,), daemon=True).start()
        return True
    monkeypatch.setattr(webbrowser, "open", open_)
    return opened


def _login_and_wait(key: str = "remote") -> dict:
    from silica.connectors import mcp_oauth

    out = mcp_oauth.login(key)
    deadline = time.monotonic() + 30
    while not mcp_oauth.login_state(key).get("done") and time.monotonic() < deadline:
        time.sleep(0.1)
    return out


def _as_log(path) -> list[str]:
    """What the authorization server was asked: register, authorize."""
    if not path.exists():
        return []
    return [json.loads(line)["as"] for line in path.read_text().splitlines() if line.strip()]


def test_a_call_that_meets_a_401_reads_needs_login_and_opens_no_browser(oauth_server, monkeypatch):
    from silica.connectors import find, mcp_client

    opened = _approving_browser(monkeypatch)
    _configure(oauth_server, _free_port())
    with pytest.raises(mcp_client.CallFailed, match="needs login"):
        mcp_client.CLIENTS.list_tools("remote", timeout=20)
    assert opened == []
    row = find("mcp").status()["rows"][0]
    assert row["state"] == "needs login" and row["oauth"] and not row["logged_in"]


def test_login_stores_the_grant_in_the_keyring_and_the_row_connects(oauth_server, monkeypatch,
                                                                   _no_real_keyring):
    from silica.connectors import find, mcp_client, mcp_oauth

    opened = _approving_browser(monkeypatch)
    _configure(oauth_server, _free_port())
    out = mcp_oauth.login("remote")
    assert out["url"].startswith(oauth_server.rsplit("/mcp", 1)[0] + "/authorize?")
    assert "code_challenge=" in out["url"] and "state=" in out["url"]
    deadline = time.monotonic() + 20
    while not mcp_oauth.login_state("remote").get("done") and time.monotonic() < deadline:
        time.sleep(0.1)
    assert mcp_oauth.login_state("remote")["error"] == ""
    assert len(opened) == 1
    stored = {u for (s, u) in _no_real_keyring.data if s == "silica"}
    assert {"mcp/remote/oauth_tokens", "mcp/remote/oauth_client"} <= stored
    assert mcp_client.CLIENTS.connected("remote")
    row = find("mcp").status()["rows"][0]
    assert row["state"].startswith("connected") and row["logged_in"]
    # A fresh session reuses the stored grant: no second browser.
    mcp_client.CLIENTS.close_all()
    assert mcp_client.CLIENTS.list_tools("remote", timeout=20)
    assert len(opened) == 1


def test_logout_drops_the_tokens_and_keeps_the_registration(oauth_server, monkeypatch, _no_real_keyring):
    from silica.connectors import credentials, mcp_oauth

    _approving_browser(monkeypatch)
    _configure(oauth_server, _free_port())
    mcp_oauth.login("remote")
    deadline = time.monotonic() + 20
    while not mcp_oauth.login_state("remote").get("done") and time.monotonic() < deadline:
        time.sleep(0.1)
    mcp_oauth.logout("remote")
    assert credentials.get("mcp/remote", "oauth_tokens") is None
    assert credentials.get("mcp/remote", "oauth_client") is not None
    assert not mcp_oauth.has_tokens("remote")


def test_the_callback_listener_takes_one_redirect_and_returns_what_it_carried():
    import asyncio

    from silica.connectors import mcp_oauth

    port = _free_port()

    async def go():
        task = asyncio.create_task(mcp_oauth.wait_for_callback(port, 5))
        await asyncio.sleep(0.2)
        r = await asyncio.to_thread(httpx.get, f"http://127.0.0.1:{port}/callback?code=abc&state=xyz&iss=https://as")
        return await task, r

    result, response = asyncio.run(go())
    assert (result.code, result.state, result.iss) == ("abc", "xyz", "https://as")
    assert "close this tab" in response.text


def test_a_refused_authorization_ends_the_wait_with_its_reason():
    import asyncio

    from silica.connectors import mcp_oauth

    port = _free_port()

    async def go():
        task = asyncio.create_task(mcp_oauth.wait_for_callback(port, 5))
        await asyncio.sleep(0.2)
        await asyncio.to_thread(httpx.get, f"http://127.0.0.1:{port}/callback?error=access_denied")
        return await task

    with pytest.raises(mcp_oauth.NeedsLogin, match="access_denied"):
        asyncio.run(go())


def test_a_configured_client_id_comes_first_in_the_registration_order(_no_real_keyring):
    from silica.connectors import credentials, mcp_client, mcp_oauth

    mcp_oauth.provider("remote", {"url": "https://mcp.example.org/mcp",
                                  "oauth": {"clientId": "pre-registered-1"}}, mcp_client.CLIENTS)
    stored = json.loads(credentials.get("mcp/remote", "oauth_client"))
    assert stored["client_id"] == "pre-registered-1"


def test_no_cimd_url_ships():
    """The owner left the permanent CIMD URL open (ADR-0091, Open 2): with
    none, the SDK falls back to dynamic registration by itself."""
    from silica.connectors import mcp_client, mcp_oauth

    p = mcp_oauth.provider("remote", {"url": "https://mcp.example.org/mcp"}, mcp_client.CLIENTS)
    assert p.context.client_metadata_url is None


def test_without_dynamic_registration_the_row_asks_for_a_client_id(monkeypatch, _no_real_keyring):
    """The spec's last registration step: a server that offers neither CIMD nor
    dynamic registration takes a client ID the user registered by hand."""
    from silica.connectors import find, mcp_client, mcp_oauth

    cb = _free_port()
    proc, url = _start("--manual-client", "manual-1", "--manual-redirect", f"http://127.0.0.1:{cb}/callback")
    try:
        _approving_browser(monkeypatch)
        _configure(url, cb)
        with pytest.raises(mcp_client.CallFailed, match="needs a client ID"):
            mcp_oauth.login("remote")
        assert find("mcp").status()["rows"][0]["state"].startswith("needs a client ID")
        assert find("mcp").act("client_id", {"key": "remote", "client_id": "manual-1"})["ok"]
        out = mcp_oauth.login("remote")
        assert "client_id=manual-1" in out["url"]
        deadline = time.monotonic() + 20
        while not mcp_oauth.login_state("remote").get("done") and time.monotonic() < deadline:
            time.sleep(0.1)
        assert mcp_oauth.login_state("remote")["error"] == ""
        assert mcp_client.CLIENTS.connected("remote")
    finally:
        proc.terminate()
        proc.wait(10)


def test_outside_a_login_nothing_is_registered_at_the_authorization_server(oauth_server, _no_real_keyring):
    """A background open (the session warm-up) meets the 401 too: it must not
    register a client with the service, only say the row needs a login."""
    from silica.connectors import credentials, mcp_client

    _configure(oauth_server, _free_port())
    with pytest.raises(mcp_client.CallFailed, match="needs login"):
        mcp_client.CLIENTS.list_tools("remote", timeout=20)
    assert credentials.get("mcp/remote", "oauth_client") is None


def test_without_a_keyring_an_unlogged_server_still_reads_needs_login(oauth_server):
    import keyring
    import keyring.backends.fail

    from silica.connectors import find, mcp_client

    keyring.set_keyring(keyring.backends.fail.Keyring())
    _configure(oauth_server, _free_port())
    with pytest.raises(mcp_client.CallFailed, match="needs login"):
        mcp_client.CLIENTS.list_tools("remote", timeout=20)
    row = find("mcp").status()["rows"][0]
    assert row["state"] == "needs login" and row["store"] == ""


def test_a_login_with_nowhere_to_keep_the_grant_says_so(oauth_server, monkeypatch):
    import keyring
    import keyring.backends.fail

    from silica.connectors import mcp_client, mcp_oauth

    keyring.set_keyring(keyring.backends.fail.Keyring())
    _approving_browser(monkeypatch)
    _configure(oauth_server, _free_port())
    with pytest.raises(mcp_client.CallFailed, match="needs a credentials store"):
        mcp_oauth.login("remote")


# --- the e2e tester's defects (2026-10-12) -----------------------------------

def _stop(proc) -> None:
    proc.terminate()
    proc.wait(10)


def test_a_background_open_with_rejected_tokens_registers_nothing_and_offers_log_in(
        monkeypatch, _no_real_keyring, tmp_path):
    """Log in, move the issuer, reload (tester, 2026-10-12): the authorization
    server logged a `register` with no browser open, the stored registration
    was overwritten, and the row read `needs login` offering only log out."""
    from silica.connectors import credentials, find, mcp_client

    log, port, cb = tmp_path / "as.jsonl", _free_port(), _free_port()
    proc, url = _start("--calls", str(log), port=port)
    opened = _approving_browser(monkeypatch)
    _configure(url, cb)
    try:
        _login_and_wait()
        assert mcp_client.CLIENTS.connected("remote")
        client = credentials.get("mcp/remote", "oauth_client")
        tokens = credentials.get("mcp/remote", "oauth_tokens")
    finally:
        _stop(proc)
    log.write_text("")
    proc, _ = _start("--calls", str(log), "--issuer", f"http://localhost:{port}", port=port)
    try:
        assert find("mcp").act("reload", {})["ok"]
        deadline = time.monotonic() + 30
        row = find("mcp").status()["rows"][0]
        while row["state"].startswith(("connected", "not connected")) and time.monotonic() < deadline:
            time.sleep(0.1)
            row = find("mcp").status()["rows"][0]
        assert row["state"] == "needs login"
        assert not row["logged_in"], "the row must offer Log in, not only Log out"
        assert _as_log(log) == [], "a background open registered or authorized"
        assert credentials.get("mcp/remote", "oauth_client") == client
        assert credentials.get("mcp/remote", "oauth_tokens") == tokens  # left stored
        assert len(opened) == 1
        # Log in is what moves to the new issuer: there, and only there, the
        # client registers again (SEP-2352) and the row connects.
        _login_and_wait()
        assert mcp_client.CLIENTS.connected("remote")
        assert _as_log(log) == ["register", "authorize"]
        assert len(opened) == 2
    finally:
        _stop(proc)


def test_a_pre_registered_client_id_is_pinned_to_its_issuer_and_a_move_is_named(
        monkeypatch, _no_real_keyring, tmp_path):
    """After the issuer moved, a login sent client_id=manual-1 to the new
    server and the stored issuer was None (tester, 2026-10-12); the spec says
    to surface an error rather than use mismatched credentials."""
    from silica.connectors import credentials, find, mcp_client

    log, port, cb = tmp_path / "as.jsonl", _free_port(), _free_port()
    manual = ("--manual-client", "manual-1", "--manual-redirect", f"http://127.0.0.1:{cb}/callback")
    proc, url = _start(*manual, port=port)
    _approving_browser(monkeypatch)
    _configure(url, cb, clientId="manual-1")
    try:
        _login_and_wait()
        assert mcp_client.CLIENTS.connected("remote")
    finally:
        _stop(proc)
    stored = json.loads(credentials.get("mcp/remote", "oauth_client"))
    assert stored["client_id"] == "manual-1"
    assert stored["issuer"].rstrip("/") == f"http://127.0.0.1:{port}"
    proc, _ = _start(*manual, "--calls", str(log), "--issuer", f"http://localhost:{port}", port=port)
    try:
        with pytest.raises(mcp_client.CallFailed) as err:
            _login_and_wait()
        why = str(err.value)
        assert f"http://127.0.0.1:{port}" in why and f"http://localhost:{port}" in why
        assert "authorize" not in _as_log(log), "the client ID was sent to the new issuer"
        row = find("mcp").status()["rows"][0]
        assert f"http://localhost:{port}" in row["state"] and row["client_id_field"]
        # The way on: the user pastes the ID registered with the new issuer
        # (here the same one), the login takes it and pins it there.
        assert find("mcp").act("client_id", {"key": "remote", "client_id": "manual-1"})["ok"]
        assert not find("mcp").status()["rows"][0]["logged_in"]  # the row offers Log in
        _login_and_wait()
        assert mcp_client.CLIENTS.connected("remote")
        stored = json.loads(credentials.get("mcp/remote", "oauth_client"))
        assert stored["issuer"].rstrip("/") == f"http://localhost:{port}"
    finally:
        _stop(proc)


def test_a_refused_iss_tells_the_browser_tab_the_login_failed(monkeypatch, _no_real_keyring):
    """The tab read "Silica is connected" while the row read failed (tester,
    2026-10-12): the page is written once the client has checked the answer."""
    from silica.connectors import find, mcp_client, mcp_oauth

    proc, url = _start("--bad-iss", "https://mixup.example")
    pages: list[str] = []
    _approving_browser(monkeypatch, pages)
    _configure(url, _free_port())
    try:
        try:
            _login_and_wait()
        except mcp_client.CallFailed:
            pass  # the refusal can land before login() returns: the row and the tab are under test
        deadline = time.monotonic() + 30
        while not pages and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        _stop(proc)
    assert "iss" in mcp_oauth.login_state("remote")["error"]
    assert find("mcp").status()["rows"][0]["state"].startswith("failed")
    assert pages and "connected" not in pages[0]
    assert "refused" in pages[0] and "https://mixup.example" in pages[0]


def test_a_good_login_tells_the_browser_tab_it_connected(monkeypatch, oauth_server, _no_real_keyring):
    pages: list[str] = []
    _approving_browser(monkeypatch, pages)
    _configure(oauth_server, _free_port())
    _login_and_wait()
    deadline = time.monotonic() + 30
    while not pages and time.monotonic() < deadline:
        time.sleep(0.1)
    assert pages and "Silica is connected" in pages[0]


def test_the_row_says_a_login_is_under_way_until_it_ends(monkeypatch, oauth_server):
    """What Settings polls on: a login that ends without connecting (refused,
    or no answer) redraws the row with its reason instead of polling on for
    five minutes."""
    import webbrowser

    from silica.connectors import find, mcp_oauth

    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: True)  # nobody approves
    monkeypatch.setattr(mcp_oauth, "LOGIN_TIMEOUT_S", 2.0)
    _configure(oauth_server, _free_port())
    mcp_oauth.login("remote")
    assert find("mcp").status()["rows"][0]["logging_in"]
    deadline = time.monotonic() + 30
    while not mcp_oauth.login_state("remote").get("done") and time.monotonic() < deadline:
        time.sleep(0.1)
    row = find("mcp").status()["rows"][0]
    assert not row["logging_in"] and row["state"] == "needs login"
