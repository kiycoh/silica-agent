# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Two-pass distill (design v2): structure under constrained decode, bodies
as free text in a same-conversation continuation.

Motivation, measured live: bodies inside JSON strings silently corrupt LaTeX
and Windows paths — `"\\top"` decodes to a TAB, `"\\neq"` to a newline — and
the newline class is undetectable after the fact. The two-pass lane removes
JSON from prose entirely, but only where the hazard exists: a mechanical
per-chunk trigger keeps clean chunks on the cheap single-call path.
"""
from unittest import mock

import pytest

from silica.kernel import prep_delegation


def _payload(excerpt: str) -> dict:
    return {
        "schema_version": 1,
        "batches": [
            {
                "inbox_file": "/abs/inbox/x.md",
                "concepts": [
                    {
                        "name": "concept",
                        "action_hint": "create",
                        "inbox_excerpt": excerpt,
                        "vault_collision": None,
                    }
                ],
            }
        ],
    }


class TestHazardTrigger:
    def test_fires_only_on_escapes_the_grammar_cannot_protect(self):
        """Under strict constrained decode, `"\\alpha"` is invalid JSON — the
        grammar forces the double backslash. Only backslash + [bfnrt] is a
        VALID escape that silently decodes to a control character, so only
        those excerpts need the body pass."""
        assert prep_delegation.needs_body_pass(
            _payload(r"the gradient $\frac{a}{b}$")) is True
        assert prep_delegation.needs_body_pass(
            _payload(r"logs land in C:\temp\out.log")) is True
        assert prep_delegation.needs_body_pass(
            _payload("plain prose, no formulas at all")) is False
        assert prep_delegation.needs_body_pass(
            _payload(r"the angle $\alpha$ and the sum $\sigma$")) is False


class TestStructureSchema:
    def test_the_structure_pass_grammar_cannot_carry_a_body(self):
        """The whole point of pass 1: with no snippet/content property and
        additionalProperties forbidden, prose physically cannot enter the
        JSON — no discipline required of the model."""
        import json as _json

        from silica.kernel.write.ops import DistillerStructure

        schema = _json.dumps(DistillerStructure.model_json_schema())
        assert '"snippet"' not in schema
        assert '"content"' not in schema
        assert '"base_content"' not in schema
        # Structure and ephemerals still travel in pass 1 (the drain needs them).
        assert '"heading"' in schema
        assert '"ephemerals"' in schema


STRUCTURE_JSON = (
    '{"main_thematic_axes":["math"],"updates":['
    '{"op":"write","heading":"Gradient","source_basename":"x.md",'
    '"path":"Target/Gradient.md","hub":"Hub"},'
    '{"op":"skip","heading":"noise","source_basename":"x.md","reason":"noise"},'
    '{"op":"patch","heading":"Loss","source_basename":"x.md",'
    '"path":"Existing/Loss.md"}],'
    '"ephemerals":[{"key":"user.course.topic","text":"gradients"}]}'
)

FULL_JSON = (
    '{"main_thematic_axes":["prose"],"updates":['
    '{"op":"write","heading":"Plain","source_basename":"x.md",'
    '"path":"Target/Plain.md","hub":"Hub","snippet":"a plain body"}],'
    '"ephemerals":[]}'
)


class _FakeProvider:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def call_llm(self, **kwargs):
        self.calls.append(kwargs)
        return mock.Mock(text=self.responses.pop(0), tool_calls=[],
                         finish_reason="stop")


@pytest.fixture
def distill_env(monkeypatch):
    """Pin the seams around run_distiller so only the LLM calls vary."""
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW", "32768")
    monkeypatch.setenv("DISTILLER_MAX_TOKENS", "2048")
    monkeypatch.delenv("SILICA_DISTILL_TWO_PASS", raising=False)
    monkeypatch.delenv("SILICA_DISTILL_PROFILE", raising=False)
    monkeypatch.setattr(prep_delegation, "active_distill_profile",
                        lambda: "default")

    def _no_network(*a, **k):
        raise RuntimeError("litellm fallback must never reach the network in tests")

    monkeypatch.setattr("silica.agent.llm.call_llm", _no_network)

    def _run(payload, responses, **kwargs):
        fake = _FakeProvider(responses)
        monkeypatch.setattr("silica.agent.providers.get_provider",
                            lambda *a, **k: fake)
        result = prep_delegation.run_distiller(
            payload=payload, target="Target", session_date="2026-08-02",
            **kwargs)
        return fake, result

    return _run


class TestSingleCallPath:
    def test_a_clean_chunk_stays_on_the_single_call(self, distill_env):
        """No hazard in the excerpts → today's path, zero overhead."""
        fake, result = distill_env(_payload("plain prose"), [FULL_JSON])

        assert len(fake.calls) == 1
        assert fake.calls[0]["response_schema"].__name__ == "DistillerOutput"
        assert result["updates"][0]["snippet"] == "a plain body"


HAZARD = _payload(r"gradient formula $\frac{\partial L}{\partial w}$ and \top")

BODY_BLOCKS = (
    "===SILICA-BODY 1===\n"
    "Gradient: $\\frac{\\partial \\mathcal{L}}{\\partial w}$ resta \\top.\n"
    "===SILICA-BODY 2===\n"
    "Loss enrichment line."
)


def _texts(messages: list[dict]) -> str:
    """Flatten message content (str or typed parts) for containment asserts."""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            out.append(c)
        else:
            out.extend(p.get("text", "") for p in c or [])
    return "\n".join(out)


class TestTwoPass:
    def test_a_hazard_chunk_runs_structure_then_bodies(self, distill_env):
        fake, result = distill_env(HAZARD, [STRUCTURE_JSON, BODY_BLOCKS])

        assert len(fake.calls) == 2
        assert fake.calls[0]["response_schema"].__name__ == "DistillerStructure"
        assert fake.calls[1]["response_schema"] is None

    def test_pass_two_is_a_continuation_of_pass_one(self, distill_env):
        """Prefix identity is the whole eco argument: same messages, plus the
        assistant echo, plus one short instruction — so prompt caches hit."""
        fake, _ = distill_env(HAZARD, [STRUCTURE_JSON, BODY_BLOCKS])

        first, second = fake.calls[0]["messages"], fake.calls[1]["messages"]
        assert second[: len(first)] == first
        assert len(second) == len(first) + 2
        assert second[-2]["role"] == "assistant"
        assert STRUCTURE_JSON in _texts([second[-2]])
        assert second[-1]["role"] == "user"
        assert "===SILICA-BODY" in _texts([second[-1]])

    def test_pass_one_is_told_not_to_attempt_bodies(self, distill_env):
        """The schema already forbids bodies; the note keeps the prompt honest
        so the model does not fight the grammar (the E2 empty-snippet lesson)."""
        fake, _ = distill_env(HAZARD, [STRUCTURE_JSON, BODY_BLOCKS])

        assert "STRUCTURE PASS" in _texts(fake.calls[0]["messages"])

    def test_bodies_land_verbatim_with_single_backslashes(self, distill_env):
        """The corruption class this whole design kills: `\\top` must survive
        as `\\top`, never decode to TAB + `op`."""
        _, result = distill_env(HAZARD, [STRUCTURE_JSON, BODY_BLOCKS])

        assert result["updates"][0]["snippet"] == (
            "Gradient: $\\frac{\\partial \\mathcal{L}}{\\partial w}$ "
            "resta \\top.")
        assert "\t" not in result["updates"][0]["snippet"]

    def test_skip_ops_consume_no_body_number(self, distill_env):
        """Numbering runs over body-carrying ops only: write=1, patch=2 —
        the skip in between must not shift the mapping."""
        _, result = distill_env(HAZARD, [STRUCTURE_JSON, BODY_BLOCKS])

        assert result["updates"][2]["snippet"] == "Loss enrichment line."
        assert "snippet" not in result["updates"][1]

    def test_a_missing_block_fails_closed(self, distill_env):
        """Body 2 never arrives → the patch op stays bodyless and the validate
        floor rejects it downstream — never a silent half-body."""
        _, result = distill_env(
            HAZARD, [STRUCTURE_JSON,
                     "===SILICA-BODY 1===\nOnly the first body."])

        assert result["updates"][0]["snippet"] == "Only the first body."
        assert "snippet" not in result["updates"][2]

    def test_a_dead_body_pass_still_returns_structure(self, distill_env):
        """Fail-soft: pass 2 blowing up must not kill the batch — ephemerals
        and skips survive, body ops fail the floor downstream."""
        fake, result = distill_env(HAZARD, [STRUCTURE_JSON])  # no 2nd response

        assert len(fake.calls) == 2
        assert result["ephemerals"] == [
            {"key": "user.course.topic", "text": "gradients"}]
        assert "snippet" not in result["updates"][0]


class TestGates:
    def test_extractive_class_profiles_split_on_hazard_too(self, distill_env):
        """They used to stay single-call on the claim that a corrupted body
        breaks the verbatim substring match and the validator rejects it.
        Measured false for the newline class: the expansion splits the body
        line in two and BOTH halves are still substrings of the source, so the
        floor passes it. No fail-closed, no exception."""
        fake, _ = distill_env(HAZARD, [STRUCTURE_JSON, BODY_BLOCKS],
                              profile="promotion")

        assert len(fake.calls) == 2
        assert fake.calls[0]["response_schema"].__name__ == "DistillerStructure"

    def test_the_kill_switch_forces_single_call(self, distill_env, monkeypatch):
        monkeypatch.setenv("SILICA_DISTILL_TWO_PASS", "0")

        fake, _ = distill_env(HAZARD, [FULL_JSON])

        assert len(fake.calls) == 1
        assert fake.calls[0]["response_schema"].__name__ == "DistillerOutput"

    def test_structure_only_never_pays_for_bodies(self, distill_env):
        """The drain discards note bodies (capture_from_distill keeps only
        ephemerals) — structure_only makes it stop paying for them."""
        fake, result = distill_env(HAZARD, [STRUCTURE_JSON],
                                   structure_only=True)

        assert len(fake.calls) == 1
        assert fake.calls[0]["response_schema"].__name__ == "DistillerStructure"
        assert result["ephemerals"] == [
            {"key": "user.course.topic", "text": "gradients"}]


class TestBodiesSurviveSanitize:
    """The body pass hands prose to SANITIZE, which was built for JSON prose."""

    def test_a_bare_literal_newline_survives_normalize_ops(self, distill_env):
        """`\\n` as the subject of a sentence: the body never went through JSON,
        so there is no double-escaping for the prose expansion to undo — and
        expanding anyway is silent, unrecoverable corruption."""
        from silica.kernel.text.sanitize import normalize_ops

        body = "Il tokenizer divide il buffer sul letterale \\n, e si ferma."
        _, result = distill_env(
            HAZARD, [STRUCTURE_JSON, f"===SILICA-BODY 1===\n{body}"])

        assert normalize_ops(result["updates"])[0]["snippet"] == body

    def test_an_extractive_body_keeps_its_escape_and_stays_verbatim(
            self, distill_env):
        """What the split buys the extractive lane: the selected span reaches
        the note exactly as selected, so the verbatim floor compares it against
        the source it was taken from — not against an expanded rewrite of it."""
        from silica.kernel.text.sanitize import normalize_ops
        from silica.kernel.write.provenance import nonextractive_lines

        excerpt = ("Il tokenizer divide il buffer sul letterale \\n "
                   "e si ferma esattamente lì, senza guardare oltre.")
        _, result = distill_env(
            _payload(excerpt),
            [STRUCTURE_JSON, f"===SILICA-BODY 1===\n{excerpt}"],
            profile="promotion")

        body = normalize_ops(result["updates"])[0]["snippet"]
        assert body == excerpt
        assert nonextractive_lines(body, excerpt) == []
