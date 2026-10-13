# SPDX-License-Identifier: AGPL-3.0-or-later
"""ADR-0102: the 5-hour window, the week and the month, against a fake
`GET /api/v1/key` that answers the shape OpenRouter documents
(https://openrouter.ai/openapi.json, operation getCurrentKey, read 2026-10-13):
`data.limit`, `data.usage` (lifetime USD) and `data.budgets[]` with `scope`,
`limit_usd`, `spend_usd`, `remaining_usd`, `reset_interval` and `resets_at`."""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from silica.agent import usage_limits
from silica.agent.usage_limits import UsageLimited

KEY = "sk-or-v1-tenant-own-key-for-tests"
T0 = 1_760_000_000.0  # 2025-10-09T08:53:20Z
MONTH_RESET = "2026-11-01T00:00:00.000Z"
WEEK_RESET = "2026-10-19T00:00:00.000Z"


def key_record(*, limit=1.5, usage=10.0, month_spend=0.2, week_spend=None, week_limit=0.5):
    """The record OpenRouter returns for a seat's key: its own monthly limit
    (scope key_limit) and, above Free, the tier's weekly guardrail (api_key)."""
    budgets = []
    if week_spend is not None:
        budgets.append({"scope": "api_key", "guardrail_id": "g-seat", "budget_id": None,
                        "limit_usd": week_limit, "spend_usd": week_spend,
                        "remaining_usd": week_limit - week_spend, "reset_interval": "weekly",
                        "resets_at": WEEK_RESET, "include_byok": False})
    if limit is not None:
        budgets.append({"scope": "key_limit", "guardrail_id": None, "budget_id": None,
                        "limit_usd": limit, "spend_usd": month_spend,
                        "remaining_usd": limit - month_spend, "reset_interval": "monthly",
                        "resets_at": MONTH_RESET, "include_byok": False})
    return {"data": {"label": "sk-or-v1-ten...sts", "limit": limit,
                     "limit_remaining": None if limit is None else limit - month_spend,
                     "limit_reset": "monthly", "usage": usage, "usage_monthly": month_spend,
                     "budgets": budgets, "effective_budget": budgets[0] if budgets else None}}


class FakeOpenRouter:
    """A local `/api/v1/key`: what it answers is `self.body` with `self.status`,
    and every Authorization header it saw is kept."""

    def __init__(self):
        self.body: dict = key_record()
        self.status = 200
        self.auth: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.auth.append(self.headers.get("Authorization", ""))
                payload = json.dumps(fake.body).encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/v1/key"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def reads(self) -> int:
        return len(self.auth)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake(monkeypatch, tmp_path):
    server = FakeOpenRouter()
    monkeypatch.setattr(usage_limits, "KEY_URL", server.url)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    monkeypatch.setenv("SILICA_USAGE_WINDOW_SHARE", "0.1")  # $0.15 of $1.50
    monkeypatch.delenv("SILICA_HOSTED", raising=False)
    clock = [T0]
    monkeypatch.setattr(usage_limits, "_now", lambda: clock[0])
    server.clock = clock
    yield server
    server.close()


def window_file(tmp_path):
    return tmp_path / ".silica" / "usage_window.json"


# --- the window (points 4 and 5) ----------------------------------------------

def test_the_window_opens_at_the_first_call_with_the_tenants_own_key(fake, tmp_path):
    assert not window_file(tmp_path).exists()
    usage_limits.check()
    assert json.loads(window_file(tmp_path).read_text()) == {"opened_at": T0, "usage_at_open": 10.0}
    assert fake.auth == [f"Bearer {KEY}"]


def test_the_window_refuses_at_its_share_and_names_its_end(fake, tmp_path):
    usage_limits.check()
    fake.body = key_record(usage=10.149)  # $0.149 of the $0.15 window
    fake.clock[0] = T0 + 3600
    usage_limits.check()
    fake.body = key_record(usage=10.15)
    with pytest.raises(UsageLimited) as hit:
        usage_limits.check()
    assert hit.value.layer == "window"
    assert hit.value.resets_at == usage_limits._iso(T0 + 5 * 3600)
    # Refusing does not move the window: it still ends 5 hours after it opened.
    assert json.loads(window_file(tmp_path).read_text())["opened_at"] == T0


def test_the_window_reopens_after_five_hours(fake, tmp_path):
    usage_limits.check()
    fake.body = key_record(usage=10.5)
    fake.clock[0] = T0 + 5 * 3600 - 1
    with pytest.raises(UsageLimited):
        usage_limits.check()
    fake.clock[0] = T0 + 5 * 3600
    usage_limits.check()
    assert json.loads(window_file(tmp_path).read_text()) == {
        "opened_at": T0 + 5 * 3600, "usage_at_open": 10.5}


def test_a_failed_read_lets_the_call_through(fake, tmp_path, caplog):
    fake.status = 500
    with caplog.at_level("WARNING", logger="silica.agent.usage_limits"):
        usage_limits.check()
    assert "the call goes ahead" in caplog.text and KEY not in caplog.text
    assert not window_file(tmp_path).exists()
    fake.close()  # nothing listening: a transport failure, not an HTTP one
    usage_limits.check()


def test_a_window_that_cannot_be_saved_costs_the_window_not_the_call(fake, monkeypatch):
    def full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(usage_limits, "_save_window", full)
    usage_limits.check()
    usage_limits.check()


@pytest.mark.parametrize("record, layer, resets", [
    (key_record(week_spend=0.5), "week", WEEK_RESET),
    (key_record(month_spend=1.5), "month", MONTH_RESET),
    # Both spent: the month, which the student waits longest for.
    (key_record(week_spend=0.6, month_spend=1.6), "month", MONTH_RESET),
])
def test_a_spent_week_or_month_refuses_before_calling(fake, tmp_path, record, layer, resets):
    fake.body = record
    with pytest.raises(UsageLimited) as hit:
        usage_limits.check()
    assert (hit.value.layer, hit.value.resets_at) == (layer, resets)
    assert not window_file(tmp_path).exists(), "a refused call opened a window"


def test_call_llm_refuses_without_calling_the_model(fake, monkeypatch):
    from silica.agent import llm

    made = []
    monkeypatch.setattr(llm, "_call_llm", lambda *a, **k: made.append(a[0]))
    fake.body = key_record(week_spend=0.5)
    with pytest.raises(UsageLimited):
        llm.call_llm("openrouter/qwen/qwen3.7-flash", [{"role": "user", "content": "hi"}])
    assert made == []


def test_nothing_changes_without_a_share(fake, monkeypatch):
    """The single-user GUI (point 4): no share, no read before a call, and a
    spent month is OpenRouter's to refuse, as before."""
    from silica.agent import llm

    monkeypatch.delenv("SILICA_USAGE_WINDOW_SHARE")
    fake.body = key_record(month_spend=1.5)
    made = []
    monkeypatch.setattr(llm, "_call_llm", lambda *a, **k: made.append(a[0]) or llm.LLMResponse(text="ok"))
    llm.call_llm("openrouter/qwen/qwen3.7-flash", [{"role": "user", "content": "hi"}])
    assert made == ["openrouter/qwen/qwen3.7-flash"] and fake.reads == 0


def test_a_model_off_openrouter_is_never_checked(fake, monkeypatch):
    from silica.agent import llm

    monkeypatch.setattr(llm, "_call_llm", lambda *a, **k: llm.LLMResponse(text="ok"))
    llm.call_llm("custom/qwen3.5-9b", [{"role": "user", "content": "hi"}])
    assert fake.reads == 0


def test_a_key_with_no_limit_has_no_window_and_no_bars(fake, tmp_path):
    fake.body = key_record(limit=None)
    usage_limits.check()
    assert not window_file(tmp_path).exists()
    assert usage_limits.status("openrouter/qwen/qwen3.7-flash") == []


def test_the_share_must_be_a_share(fake, monkeypatch):
    for bad in ("0", "1.5", "a tenth", "-0.1"):
        monkeypatch.setenv("SILICA_USAGE_WINDOW_SHARE", bad)
        assert usage_limits.window_share() is None
    monkeypatch.setenv("SILICA_USAGE_WINDOW_SHARE", "1")
    assert usage_limits.window_share() == 1.0


def test_decide_is_refused_like_a_call(fake, monkeypatch):
    from silica.agent import decide
    from silica.config import CONFIG

    monkeypatch.setattr(CONFIG, "decision_model", "typesafe/jev")
    posted = []
    monkeypatch.setattr(decide.httpx, "post", lambda *a, **k: posted.append(a))
    fake.body = key_record(month_spend=1.5)
    assert decide.decide("state", {"q": {"type": "noul"}}) is None
    assert posted == []


# --- what the drawer reads (point 7) -------------------------------------------

def test_limits_shape_with_a_window_open(fake):
    usage_limits.check()
    fake.body = key_record(usage=10.06, week_spend=0.4, month_spend=1.2)
    got = usage_limits.status("openrouter/qwen/qwen3.7-flash")
    assert got == [
        {"layer": "window", "used": 0.4, "resets_at": usage_limits._iso(T0 + 5 * 3600)},
        {"layer": "week", "used": 0.8, "resets_at": WEEK_RESET},
        {"layer": "month", "used": 0.8, "resets_at": MONTH_RESET},
    ]


def test_limits_before_the_first_message_and_off_openrouter(fake, monkeypatch):
    fake.body = key_record(month_spend=0.3)
    assert usage_limits.status("openrouter/x/y") == [
        {"layer": "window", "used": 0.0, "resets_at": None},
        {"layer": "month", "used": 0.2, "resets_at": MONTH_RESET},
    ]
    # Point 7 stands on its own (amendment 1): no share, the month still shows.
    monkeypatch.delenv("SILICA_USAGE_WINDOW_SHARE")
    assert [d["layer"] for d in usage_limits.status("openrouter/x/y")] == ["month"]
    reads = fake.reads
    assert usage_limits.status("ollama/qwen3.5:9b") == [] and fake.reads == reads


def test_the_limits_route(fake, tmp_vault, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.config import CONFIG
    from silica.ui.web import server

    monkeypatch.setattr(CONFIG, "model", "openrouter/qwen/qwen3.7-flash")
    fake.body = key_record(week_spend=0.25, month_spend=0.75)
    body = TestClient(server.app).get("/limits").json()
    assert body == {"limits": [
        {"layer": "window", "used": 0.0, "resets_at": None},
        {"layer": "week", "used": 0.5, "resets_at": WEEK_RESET},
        {"layer": "month", "used": 0.5, "resets_at": MONTH_RESET},
    ]}
    fake.status = 401
    assert TestClient(server.app).get("/limits").json() == {"limits": []}


# --- the sentences (point 6) ----------------------------------------------------

def test_each_layer_has_its_sentence_with_the_reset():
    from silica.ui.web.failures import explain

    out = explain(UsageLimited("window", "2026-10-13T12:20:00Z"))
    assert out["error"] == "You have used this 5-hour window. It resets at 12:20 UTC."
    assert (out["limit"], out["resets_at"], out["when"]) == ("window", "2026-10-13T12:20:00Z", "at 12:20 UTC")
    assert explain(UsageLimited("week", WEEK_RESET))["error"] == (
        "You have used this week's allowance. It resets on Monday at 00:00 UTC.")
    assert explain(UsageLimited("month", MONTH_RESET))["error"] == (
        "You have used this month's allowance. It resets on 1 November at 00:00 UTC.")


def test_a_sub_agents_refusal_keeps_its_sentence_in_a_tool_row():
    from silica.ui.web.failures import sentence

    raw = f"subagent failed: {UsageLimited('week', WEEK_RESET)}"
    assert sentence(raw) == "You have used this week's allowance. It resets on Monday at 00:00 UTC."


class _Forbidden(Exception):
    status_code = 403


class _Unauthorized(Exception):
    status_code = 401


def test_hosted_a_403_at_a_limit_names_the_spent_layer(fake, monkeypatch):
    from silica.ui.web.failures import explain

    monkeypatch.setenv("SILICA_HOSTED", "1")
    fake.body = key_record(week_spend=0.52)
    out = explain(_Forbidden('OpenrouterException - {"error":{"message":"Key limit exceeded","code":403}}'))
    assert out["error"] == "You have used this week's allowance. It resets on Monday at 00:00 UTC."
    assert out["limit"] == "week" and out["detail"] == ""
    # Transcription reaches OpenRouter outside call_llm and fails as text alone.
    out = explain(ValueError("transcription server returned 403: limit"))
    assert out["limit"] == "week"


def test_a_403_with_nothing_spent_and_a_bad_key_keep_the_credentials_sentence(fake, monkeypatch):
    from silica.ui.web.failures import explain

    monkeypatch.setenv("SILICA_HOSTED", "1")
    out = explain(_Forbidden("403 Forbidden"))
    assert out["error"].startswith("The provider rejected the credentials") and "limit" not in out
    fake.body = key_record(week_spend=0.6)
    reads = fake.reads
    # A website's 403 under /fetch is not a spent allowance, and reads nothing.
    out = explain(RuntimeError("fetch failed: HTTP 403 Forbidden for https://example.org"))
    assert "limit" not in out and fake.reads == reads
    fake.body = key_record(month_spend=1.5)
    out = explain(_Unauthorized("401 Unauthorized: User not found"))
    assert out["error"].startswith("The provider rejected the credentials") and "limit" not in out
    assert fake.reads == reads, "a bad key's 401 read the key"


def test_a_laptops_403_reads_nothing(fake):
    from silica.ui.web.failures import explain

    fake.body = key_record(month_spend=1.5)
    out = explain(_Forbidden("403 Forbidden"))
    assert "limit" not in out and fake.reads == 0


# --- the turn (points 8 and 10) -------------------------------------------------

@pytest.fixture
def journal(monkeypatch):
    import logging

    from silica.ui.logging import HostedJsonFormatter

    class Lines(logging.Handler):
        def __init__(self):
            super().__init__(logging.DEBUG)
            self.setFormatter(HostedJsonFormatter())
            self.lines: list[str] = []

        def emit(self, record):
            self.lines.append(self.format(record))

        def turns(self):
            return [d for d in map(json.loads, self.lines) if d.get("event") == "turn"]

    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.delenv("SILICA_METRICS_OPTOUT", raising=False)
    log = logging.getLogger("silica.metrics")
    saved = log.level
    log.setLevel(logging.INFO)
    lines = Lines()
    log.addHandler(lines)
    yield lines
    log.removeHandler(lines)
    log.setLevel(saved)


def test_a_refused_turn_is_counted_limited_and_says_when(fake, journal, tmp_vault, monkeypatch):
    pytest.importorskip("fastapi")
    from silica.agent import llm
    from silica.ui.web import server

    server._reset_session()
    made = []
    monkeypatch.setattr(llm, "_call_llm", lambda *a, **k: made.append(a[0]))

    def run_agent(messages, model, *a, **k):  # the loop's first call, through the real gate
        return llm.call_llm("openrouter/qwen/qwen3.7-flash", messages).text

    monkeypatch.setattr(server, "run_agent", run_agent)
    usage_limits.check()  # opens the window
    fake.body = key_record(usage=10.2)

    async def collect():
        return [item async for item in server.run_turn("what is entropy?")]

    frames = asyncio.run(collect())
    error = frames[-1]
    assert error["type"] == "error" and error["limit"] == "window"
    assert error["error"].startswith("You have used this 5-hour window. It resets at ")
    assert error["when"] in error["error"] and error["resets_at"]
    assert made == []
    (turn,) = journal.turns()
    assert (turn["outcome"], turn["limit"]) == ("limited", "window")


def test_what_needs_no_model_stays_at_the_limit(fake, tmp_vault, monkeypatch):
    """Point 8: reading a note, the tree and the vault's + for a text file call
    no model, so a spent month leaves them working."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.agent import llm
    from silica.ui.web import server

    def no_model(*a, **k):
        raise AssertionError("a model was called")

    monkeypatch.setattr(llm, "_call_llm", no_model)
    monkeypatch.setattr(server, "_embed_imported", lambda folders: None)
    fake.body = key_record(month_spend=1.5)
    path = tmp_vault.note("Entropy.md", "# Entropy\n\nA measure of disorder.\n")
    server._reset_session()
    tc = TestClient(server.app)
    assert tc.get("/note", params={"path": path}).status_code == 200
    resp = tc.post("/vault/import", files=[("files", ("Lecture.md", b"# Lecture\n\nnotes", "text/markdown"))],
                   data={"paths": [""], "last": "1"})
    assert resp.status_code == 200
    from silica.config import CONFIG

    assert (Path(CONFIG.vault_path) / "Inbox" / "Lecture.md").exists()
    assert tc.get("/limits").status_code == 200


def test_hosted_dictation_at_a_limit_says_which_and_when(fake, monkeypatch, tmp_vault):
    """Dictation reaches OpenRouter outside call_llm: its 403 was shown as
    "transcription failed (403): <OpenRouter's body>"."""
    from fastapi.testclient import TestClient

    from silica.agent import stt
    from silica.ui.web import server

    monkeypatch.setenv("SILICA_HOSTED", "1")
    fake.body = key_record(month_spend=1.5)

    async def endpoint(url, clip):
        return "", 'transcription failed (403): {"error":{"message":"Key limit exceeded","code":403}}'

    monkeypatch.setattr(server, "_stt_via_endpoint", endpoint)
    monkeypatch.setattr(stt, "dictation_endpoint", lambda cfg: ("https://openrouter.ai/api/v1", "SILICA_STT_BASE_URL"))
    monkeypatch.setattr(stt, "has_local_stt", lambda: False)
    resp = TestClient(server.app).post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    assert resp.status_code == 502
    assert resp.json()["detail"].startswith("You have used this month's allowance.")
    assert "Key limit" not in resp.text
