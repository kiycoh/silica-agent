# SPDX-License-Identifier: AGPL-3.0-or-later
"""decide(): one typed decision call (TypeSafe Jev through OpenRouter).

Contract under test: disabled without a model, no retry, and every failure
mode (transport, HTTP, invalid reply) returns None so the caller runs its
existing LLM path. A valid negative answer is still an answer.
"""
import math

import httpx


STATE = {"file_content": "Notes typed during the call. Messy."}
QUESTIONS = {
    "form": {
        "type": "choice",
        "instructions": "Classify the FORM of this file.",
        "criteria": {"study": "learning material", "transcript": "record of an event"},
    }
}
GOOD_REPLY = {
    "model": "typesafe/jev-1.13-20260917",
    "answers": {
        "form": {
            "type": "choice",
            "choice": "transcript",
            "probabilities": {"study": 0.05, "transcript": 0.95},
            "confidence": 0.93,
        }
    },
    "usage": {"input_tokens": 465, "output_tokens": 57, "cost": 0.00002},
}


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def _serve(monkeypatch, body, status=200):
    """Stub httpx.post; returns the list of (url, kwargs) it received."""
    import silica.agent.decide as decide_mod

    calls = []

    def _post(url, **kw):
        calls.append((url, kw))
        return _Resp(status, body)

    monkeypatch.setattr(decide_mod.httpx, "post", _post)
    monkeypatch.setattr("silica.config.CONFIG.decision_model", "typesafe/jev-1.13")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k-test")
    return calls


class TestDecide:
    def test_decide_is_off_without_a_model(self, monkeypatch):
        import silica.agent.decide as decide_mod
        from silica.agent.decide import decide

        monkeypatch.setattr("silica.config.CONFIG.decision_model", "")

        def _boom(*a, **k):
            raise AssertionError("no request when disabled")

        monkeypatch.setattr(decide_mod.httpx, "post", _boom)
        assert decide(STATE, QUESTIONS) is None

    def test_decide_returns_the_validated_answers(self, monkeypatch):
        from silica.agent.decide import decide

        _serve(monkeypatch, GOOD_REPLY)
        answers = decide(STATE, QUESTIONS)
        assert answers["form"]["choice"] == "transcript"
        assert answers["form"]["probabilities"]["transcript"] == 0.95

    def test_decide_sends_model_state_questions_and_the_key(self, monkeypatch):
        from silica.agent.decide import decide

        calls = _serve(monkeypatch, GOOD_REPLY)
        decide(STATE, QUESTIONS)
        url, kw = calls[0]
        assert url == "https://openrouter.ai/api/alpha/decisions"
        assert kw["json"] == {"model": "typesafe/jev-1.13", "state": STATE, "questions": QUESTIONS}
        assert kw["headers"]["Authorization"] == "Bearer k-test"
        assert kw["timeout"] > 0

    def test_decide_rejects_a_choice_outside_the_criteria(self, monkeypatch, caplog):
        from silica.agent.decide import decide

        bad = {**GOOD_REPLY, "answers": {"form": {**GOOD_REPLY["answers"]["form"], "choice": "banana"}}}
        _serve(monkeypatch, bad)
        with caplog.at_level("INFO", logger="silica.agent.decide"):
            assert decide(STATE, QUESTIONS) is None
        assert "invalid" in caplog.text

    def test_decide_rejects_a_choice_that_is_not_a_string(self, monkeypatch, caplog):
        """A list where a string was expected must be "invalid", never a
        TypeError escaping to the caller (which would skip its LLM path)."""
        from silica.agent.decide import decide

        bad = {**GOOD_REPLY, "answers": {"form": {**GOOD_REPLY["answers"]["form"], "choice": ["transcript"]}}}
        _serve(monkeypatch, bad)
        with caplog.at_level("INFO", logger="silica.agent.decide"):
            assert decide(STATE, QUESTIONS) is None
        assert "invalid" in caplog.text

    def test_decide_logs_the_answers_it_returns(self, monkeypatch, caplog):
        from silica.agent.decide import decide

        _serve(monkeypatch, GOOD_REPLY)
        with caplog.at_level("INFO", logger="silica.agent.decide"):
            decide(STATE, QUESTIONS)
        assert "transcript" in caplog.text and "0.95" in caplog.text

    def test_decide_rejects_a_non_finite_probability(self, monkeypatch):
        from silica.agent.decide import decide

        bad = {**GOOD_REPLY, "answers": {"form": {**GOOD_REPLY["answers"]["form"],
                                                   "probabilities": {"study": math.nan, "transcript": 0.95}}}}
        _serve(monkeypatch, bad)
        assert decide(STATE, QUESTIONS) is None

    def test_decide_rejects_a_reply_missing_a_question(self, monkeypatch):
        from silica.agent.decide import decide

        _serve(monkeypatch, {**GOOD_REPLY, "answers": {}})
        assert decide(STATE, QUESTIONS) is None

    def test_decide_returns_none_on_transport_error_without_retry(self, monkeypatch, caplog):
        import silica.agent.decide as decide_mod
        from silica.agent.decide import decide

        monkeypatch.setattr("silica.config.CONFIG.decision_model", "typesafe/jev-1.13")
        monkeypatch.setenv("OPENROUTER_API_KEY", "k-test")
        calls = []

        def _post(url, **kw):
            calls.append(url)
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(decide_mod.httpx, "post", _post)
        with caplog.at_level("INFO", logger="silica.agent.decide"):
            assert decide(STATE, QUESTIONS) is None
        assert len(calls) == 1
        assert "error" in caplog.text

    def test_decide_returns_none_on_http_error(self, monkeypatch):
        from silica.agent.decide import decide

        _serve(monkeypatch, {"error": "rate limited"}, status=429)
        assert decide(STATE, QUESTIONS) is None

    def test_decide_keeps_a_confident_no_as_an_answer(self, monkeypatch):
        """A noul near 0 is a valid negative, not a failure: thresholds are the caller's."""
        from silica.agent.decide import decide

        q = {"urgent": {"type": "noul", "instructions": "Is this urgent?"}}
        _serve(monkeypatch, {**GOOD_REPLY, "answers": {"urgent": {"type": "noul", "noul": 0.02}}})
        assert decide(STATE, q) == {"urgent": {"type": "noul", "noul": 0.02}}
