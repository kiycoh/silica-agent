"""ADR-0172 point 1: the chat's setup card, the first run without a terminal.

The card writes through POST /settings, so what is checked here is that those
writes land the keys `silica init` lands, that a local runtime's embedder is
never taken for its chat model, when the card shows at all, and that Test reads
the probe's failure as a sentence. Every write goes to a throwaway .env."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("fastapi")
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from silica.config import CONFIG  # noqa: E402
from silica.onboarding.checks import CheckResult  # noqa: E402

_CARD_KEYS = ("SILICA_MODEL", "SILICA_PROVIDER", "SILICA_PROVIDER_BASE_URL", "OPENROUTER_API_KEY")


@pytest.fixture
def card(tmp_vault, tmp_path, monkeypatch):
    """A client whose Settings writes land in tmp_path/card.env, with the live
    values the writes touch restored after the test."""
    from silica.ui.web import server
    from silica.ui.web.session import SESSION

    env = tmp_path / "card.env"
    monkeypatch.setattr("silica.kernel.base.user_env.resolve_env_path", lambda: env)
    monkeypatch.setattr(SESSION, "busy", False)
    monkeypatch.setattr(CONFIG, "model", "")
    monkeypatch.setattr(CONFIG, "_provider", "lmstudio")
    for key in _CARD_KEYS:  # apply() makes a key live in os.environ
        monkeypatch.setenv(key, "")
    return TestClient(server.app), env


def _env(path) -> dict[str, str]:
    from dotenv import dotenv_values

    return {k: v or "" for k, v in dotenv_values(path).items()}


def test_an_online_key_lands_what_silica_init_lands(card, tmp_path, monkeypatch):
    import silica.onboarding.wizard as wizard

    c, card_env = card
    # silica init, scripted: OpenRouter, a key, Enter on the model.
    vault = tmp_path / "init-vault"
    vault.mkdir()
    init_env = tmp_path / "init.env"
    monkeypatch.setattr(wizard, "endpoint_model_ids", lambda base, api_key="": [])
    monkeypatch.setattr(wizard, "run_checks", lambda cfg: [])
    monkeypatch.setattr(wizard.gitstate, "find_repo_root", lambda p: None)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    answers = iter(["", str(vault), "", "openrouter", "sk-or-test", "", "n", ""])
    assert wizard.run_wizard(input_fn=lambda _p: next(answers), env_path=init_env) == 0

    # The card's two writes, in its order.
    for key, value in (("OPENROUTER_API_KEY", "sk-or-test"), ("SILICA_PROVIDER", "openrouter")):
        assert c.post("/settings", json={"key": key, "value": value}).status_code == 200

    init, wrote = _env(init_env), _env(card_env)
    for key in ("SILICA_PROVIDER", "SILICA_MODEL", "OPENROUTER_API_KEY"):
        assert wrote[key] == init[key], key
    assert c.get("/config").json()["setup"] is False


def test_a_local_runtime_gives_its_chat_model_and_never_its_embedder(card, monkeypatch):
    from silica.onboarding import serve

    c, card_env = card
    monkeypatch.setattr(serve, "ready", lambda url: True)
    monkeypatch.setattr(serve, "endpoint_model_ids",
                        lambda url, api_key="": ["text-embedding-qwen3-embedding-0.6b", "qwen/qwen3.5-9b"])
    offered = c.get("/settings/setup").json()
    assert offered["needed"] is True
    assert {r["runtime"]: r["models"] for r in offered["local"]} == {
        "lmstudio": ["qwen/qwen3.5-9b"], "ollama": ["qwen/qwen3.5-9b"]}

    assert c.post("/settings", json={"key": "SILICA_PROVIDER", "value": "lmstudio"}).status_code == 200
    assert _env(card_env)["SILICA_MODEL"] == "qwen/qwen3.5-9b"
    assert c.get("/config").json()["setup"] is False


def test_a_runtime_serving_only_an_embedder_offers_nothing_to_chat_with(card, monkeypatch):
    from silica.onboarding import serve

    c, _env_path = card
    monkeypatch.setattr(serve, "ready", lambda url: url.endswith(":1234/v1"))
    monkeypatch.setattr(serve, "endpoint_model_ids", lambda url, api_key="": ["embeddinggemma-2"])
    rows = {r["runtime"]: r for r in c.get("/settings/setup").json()["local"]}
    assert rows["lmstudio"]["up"] and rows["lmstudio"]["models"] == []
    assert not rows["ollama"]["up"]


def test_the_card_shows_without_a_model_or_its_key_and_never_on_the_server(card, monkeypatch):
    from silica.ui.web import settings_routes

    c, _env_path = card
    assert c.get("/config").json()["setup"] is True
    # A hosted provider's model with no key is check_chat_model's fail too.
    monkeypatch.setattr(CONFIG, "model", "openrouter/deepseek/deepseek-v4-flash")
    monkeypatch.setattr(CONFIG, "_provider", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert settings_routes.setup_needed() is True
    monkeypatch.setattr("silica.config.hosted", lambda: True)
    assert settings_routes.setup_needed() is False
    with pytest.raises(HTTPException):
        settings_routes.setup_options()


@pytest.mark.parametrize(("detail", "said"), [
    ("failed: litellm.AuthenticationError: 401 No auth credentials found", "The provider did not accept the API key"),
    ("failed: OpenrouterException 402: This request requires more credits, or fewer max_tokens",
     "The provider says this account has no credit left"),
    ("skipped — no model configured", "No model is set up yet"),
])
def test_test_says_what_failed_in_one_sentence_and_keeps_the_probe_as_detail(card, monkeypatch, detail, said):
    from silica.onboarding import checks

    c, _env_path = card
    sent = []
    monkeypatch.setattr(checks, "live_probe", lambda cfg: sent.append(1) or CheckResult("live probe", "fail", detail))
    r = c.post("/settings/setup/test").json()
    assert r["ok"] is False and r["error"].startswith(said) and r["detail"] == detail
    assert sent == [1]
