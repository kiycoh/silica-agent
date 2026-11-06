# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared shapes of a model call: the deadline, the failure words, the keys a
request carries, and the one-call helpers every lane goes through."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from silica.model import keys
from silica.model.errors import context_overflow, server_fault, shared_pool, unreadable_reply
from silica.model.llm import run_with_deadline
from silica.model.reply import parse_reply


class _Http500(Exception):
    status_code = 500


def test_a_deadline_marks_the_call_abandoned():
    abandoned = threading.Event()
    with pytest.raises(TimeoutError, match="wall-clock deadline"):
        run_with_deadline(lambda: time.sleep(5), 0.05, abandon=abandoned)
    assert abandoned.is_set()


def test_a_stop_flag_ends_the_wait_without_the_call():
    stop, abandoned = threading.Event(), threading.Event()
    stop.set()

    class Stopped(Exception):
        pass

    started = time.monotonic()
    with pytest.raises(Stopped):
        run_with_deadline(lambda: time.sleep(5), None, abandon=abandoned, stop=stop, on_stop=Stopped)
    assert time.monotonic() - started < 1 and abandoned.is_set()


def test_a_stop_flag_leaves_a_finished_call_its_value():
    assert run_with_deadline(lambda: 42, None, stop=threading.Event()) == 42


def test_a_base_exception_crosses_back_when_asked_to():
    def die():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_with_deadline(die, 1.0, catch=BaseException)


def test_failures_are_read_in_one_place():
    over = Exception("request (40000 tokens) exceeds the available context size (32768 tokens)")
    assert context_overflow(over) == (40000, 32768)
    assert context_overflow(Exception("nope")) is None
    assert shared_pool(Exception('{"limit_source": "upstream_provider_shared_pool"}'))
    assert server_fault(_Http500("x")) and not server_fault(Exception("x"))
    assert unreadable_reply(_Http500("does not match the expected peg-native format"))
    assert not unreadable_reply(_Http500("Context size has been exceeded"))
    assert not unreadable_reply(Exception("does not match the expected"))


def test_a_benched_key_sits_out_of_every_request(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k1")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "k2")
    monkeypatch.setattr(keys, "_benched", {})
    assert keys.ready_key("openrouter") == "k1"
    keys._benched["k1"] = time.monotonic() + 60
    assert keys.ready_key("openrouter") == "k2"


def test_a_provider_without_keys_has_no_ready_key():
    assert keys.ready_key("lmstudio") == ""


def test_a_decision_follows_the_pool(monkeypatch):
    from silica.config import CONFIG
    from silica.model import decide

    monkeypatch.setenv("OPENROUTER_API_KEY", "k1")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "k2")
    monkeypatch.setattr(keys, "_benched", {"k1": time.monotonic() + 60})
    monkeypatch.setattr(CONFIG, "decision_model", "typesafe/jev")
    monkeypatch.setattr("silica.model.usage_limits.check", lambda: None)
    sent = {}

    def post(url, json, headers, timeout):
        sent.update(headers)
        return SimpleNamespace(status_code=500, text="no")

    monkeypatch.setattr("silica.model.decide.httpx.post", post)
    decide.decide({}, {})
    assert sent["Authorization"] == "Bearer k2"


def test_the_usage_read_follows_the_pool(monkeypatch):
    from silica.model import usage_limits

    monkeypatch.setenv("OPENROUTER_API_KEY", "k1")
    monkeypatch.setenv("OPENROUTER_API_KEY_2", "k2")
    monkeypatch.setattr(keys, "_benched", {"k1": time.monotonic() + 60})
    sent = {}

    def get(url, headers, timeout):
        sent.update(headers)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": {}})

    monkeypatch.setattr("silica.model.usage_limits.httpx.get", get)
    usage_limits.read_key()
    assert sent["Authorization"] == "Bearer k2"


@pytest.mark.parametrize("raw, value", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('Here it is: {"a": 1} done', {"a": 1}),
    ('{"a": 1}\n}', {"a": 1}),
    ("[1, 2]", [1, 2]),
])
def test_a_reply_parses_as_models_write_it(raw, value):
    assert parse_reply(raw) == value


@pytest.mark.parametrize("raw", ["", None, "not json", '[{"a": 1}, {"a"'])
def test_a_reply_that_is_not_json_is_the_callers_default(raw):
    assert parse_reply(raw, {"fell": "back"}) == {"fell": "back"}


def test_the_body_appendix_stays_the_distillers(monkeypatch):
    from silica.kernel.text.sanitize import parse_json

    raw = '{"updates": [{"op": "write", "snippet_ref": 1}]}\n===SILICA-BODY 1===\nbody text\n'
    parsed, _ = parse_json(raw)
    assert parsed["updates"][0]["snippet"] == "body text"


def _fake_lane(seen, text):
    def get_provider(config, role="router"):
        seen["role"] = role

        def call_llm(**kw):
            seen.update(kw)
            return SimpleNamespace(text=text)
        return SimpleNamespace(call_llm=call_llm)
    return get_provider


def test_ask_json_reaches_a_patched_lane_and_defaults_on_a_bad_reply(monkeypatch):
    from silica.model.ask import ask_json

    seen: dict = {}
    monkeypatch.setattr("silica.model.providers.get_provider", _fake_lane(seen, "nonsense"))
    out = ask_json([{"role": "user", "content": "q"}], config=object(), default={"d": 1}, max_tokens=7)
    assert out == {"d": 1}
    assert seen["role"] == "worker" and seen["max_tokens"] == 7 and seen["tools"] is None


def test_ask_once_turns_thinking_off_on_the_roles_model(monkeypatch):
    from silica.config import CONFIG
    from silica.model.ask import ask_once

    seen: dict = {}

    def call_llm(model, messages, **kw):
        seen.update(kw, model=model, messages=messages)
        return SimpleNamespace(text="ok", finish_reason="stop")

    monkeypatch.setattr("silica.model.llm.call_llm", call_llm)
    monkeypatch.setattr(CONFIG, "judge_model", "openrouter/judge")
    assert ask_once("p", max_tokens=9, role="judge").text == "ok"
    assert seen == {"model": "openrouter/judge", "messages": [{"role": "user", "content": "p"}],
                    "max_tokens": 9, "temperature": 0, "reasoning": False}


def test_a_new_openai_compatible_row_needs_no_branch(monkeypatch):
    """H-model-5: a local server on litellm's generic route is a table row."""
    from dataclasses import replace

    from silica.model import llm
    from silica.provider_table import PROVIDERS
    from tests.llm_mocks import litellm_mock_response

    row = replace(PROVIDERS["lmstudio"], name="newbox", base_url="http://box:9/v1", fixed_key="nb")
    monkeypatch.setitem(PROVIDERS, "newbox", row)
    seen: dict = {}

    def completion(**kw):
        seen.update(kw)
        return litellm_mock_response(text="ok")

    monkeypatch.setattr(llm._litellm(), "completion", completion)
    llm.call_llm("newbox/m", [{"role": "user", "content": "hi"}])
    assert (seen["model"], seen["api_key"]) == ("openai/m", "nb")
    assert seen["api_base"] == "http://box:9/v1"


def test_limits_reads_the_environment_after_config_loaded_dotenv():
    """OLLAMA_HOST in `.env` is read at import here, so a process entering through
    this module must have run config first."""
    import subprocess
    import sys

    probe = "import sys, silica.model.limits; assert 'silica.config' in sys.modules"
    subprocess.run([sys.executable, "-c", probe], check=True)
