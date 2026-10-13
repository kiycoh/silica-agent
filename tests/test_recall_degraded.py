# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A retrieval call says which leg it ran without (ADR-0094).

History: `degraded: ["rerank"]` on silica_recall dates from 2026-09-03, when
recall answered for a day with :1235 down and nothing in the reply said so. It
keyed on `reranked`, which is also False for an unconfigured reranker, for
gate 2b and for every `notes=` call, and it never named the embedder, whose
loss turns a search by meaning into a search by words. The record is now
filled at the two endpoint failure sites only, as a dict from leg to cause.
"""
from __future__ import annotations

import httpx
import openai
import pytest

from silica.agent.providers import Reranker, why_of
from silica.agent.recall_watch import THIN_COVERAGE_HINT, RecallWatch, degraded_lines

_REQ = httpx.Request("POST", "http://127.0.0.1:9/v1/embeddings")


def _status(code: int) -> openai.APIStatusError:
    return openai.APIStatusError("boom", response=httpx.Response(code, request=_REQ), body=None)


class TestWhyOf:
    def test_a_timeout_is_slow_not_unreachable(self):
        """A slow server read as a dead one and was told to start (review 4)."""
        assert why_of(openai.APITimeoutError(request=_REQ)) == "slow"
        assert why_of(httpx.ReadTimeout("t", request=_REQ)) == "slow"

    def test_a_503_is_loading(self):
        assert why_of(_status(503)) == "loading"
        resp = httpx.Response(503, request=_REQ)
        assert why_of(httpx.HTTPStatusError("x", request=_REQ, response=resp)) == "loading"

    def test_the_rest_follow_the_failure_kind(self):
        assert why_of(openai.APIConnectionError(request=_REQ)) == "unreachable"
        assert why_of(httpx.ConnectError("refused", request=_REQ)) == "unreachable"
        assert why_of(_status(404)) == "rejected"
        assert why_of(_status(500)) == "failed"
        assert why_of(ValueError("not json")) == "failed"


def _rerank_reply(monkeypatch, payload=None, exc=None):
    def post(url, json=None, headers=None, timeout=None):
        if exc is not None:
            raise exc
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr("silica.agent.providers.httpx.post", post)


class TestRerankerFailure:
    def test_a_partial_reply_abstains_instead_of_reranking_on_zeros(self, monkeypatch):
        _rerank_reply(monkeypatch, {"results": [{"index": 0, "relevance_score": 0.9}]})
        r = Reranker("http://127.0.0.1:9/v1", "m")
        assert r.scores("q", ["a", "b"]) is None
        assert r.failure == "failed"

    def test_a_reply_that_is_not_a_list_abstains(self, monkeypatch):
        _rerank_reply(monkeypatch, {"results": "nope"})
        r = Reranker("http://127.0.0.1:9/v1", "m")
        assert r.scores("q", ["a"]) is None
        assert r.failure == "failed"

    def test_a_refused_connection_is_unreachable(self, monkeypatch):
        _rerank_reply(monkeypatch, exc=httpx.ConnectError("refused", request=_REQ))
        r = Reranker("http://127.0.0.1:9/v1", "m")
        assert r.scores("q", ["a"]) is None
        assert r.failure == "unreachable"

    def test_a_full_reply_scores_and_names_no_failure(self, monkeypatch):
        _rerank_reply(monkeypatch, {"results": [{"index": 1, "relevance_score": 0.2},
                                                {"index": 0, "relevance_score": 0.9}]})
        r = Reranker("http://127.0.0.1:9/v1", "m")
        assert r.scores("q", ["a", "b"]) == [0.9, 0.2]
        assert r.failure is None


class _Rec:
    def __init__(self, path):
        self.path, self.score, self.origin = path, 0.03, "vault"


class _Down:
    failure = "unreachable"

    def scores(self, query, docs):
        return None


def _rerank(monkeypatch, reranker, stats, body_len=200):
    from silica.kernel.recall.rerank import rerank_related

    monkeypatch.setattr("silica.kernel.recall.rerank._read_body",
                        lambda p, origin="vault": (p, "word " * (body_len // 5)))
    return rerank_related(reranker, "q", [_Rec("a.md"), _Rec("b.md")], k=2, stats=stats)


class TestRerankRelated:
    def test_an_abstaining_reranker_names_its_cause(self, monkeypatch):
        stats: dict = {}
        out = _rerank(monkeypatch, _Down(), stats)
        assert [r.path for r in out] == ["a.md", "b.md"]  # the fused order stands
        assert stats["degraded"] == {"rerank": "unreachable"}

    def test_the_first_cause_per_leg_wins(self, monkeypatch):
        stats: dict = {"degraded": {"rerank": "slow"}}
        _rerank(monkeypatch, _Down(), stats)
        assert stats["degraded"] == {"rerank": "slow"}

    def test_gate_2b_is_a_designed_abstention_and_is_not_marked(self, monkeypatch):
        stats: dict = {}
        _rerank(monkeypatch, _Down(), stats, body_len=200_000)
        assert stats["gate_fired"] is True
        assert "degraded" not in stats


def test_facade_retrieve_names_the_query_embed_cause(monkeypatch):
    from silica.kernel.recall import perception

    class _Store:
        def __len__(self):
            return 30

    class _Slow:
        def embed(self, texts, query=False):
            raise openai.APITimeoutError(request=_REQ)

    monkeypatch.setattr("silica.kernel.recall.embed.get_store", lambda: _Store())
    monkeypatch.setattr("silica.kernel.recall.cooccurrence.get_cooccur_store",
                        lambda lang=None: [])
    monkeypatch.setattr("silica.agent.providers.get_embedder", lambda config: _Slow())
    stats: dict = {}
    assert perception.facade_retrieve("q", k=3, use_memory=False, leg_stats=stats) == (None, None)
    assert stats["degraded"] == {"embed": "slow"}


def _fake_retrieve(degraded: dict | None = None, reranked: bool = True):
    def fake(query, *, k, leg_stats=None, **kwargs):
        if leg_stats is not None:
            leg_stats["reranked"] = reranked
            if degraded:
                leg_stats.setdefault("degraded", {}).update(degraded)
        return [], None
    return fake


def test_perceive_forwards_the_record(monkeypatch):
    monkeypatch.setattr("silica.kernel.recall.perception.facade_retrieve",
                        _fake_retrieve({"embed": "unreachable"}))
    from silica.kernel.recall.perception import perceive

    rr: dict = {}
    perceive("q", now="2026-09-03", k=3, leg_stats=rr)
    assert rr == {"reranked": True, "degraded": {"embed": "unreachable"}}


class TestTheToolResult:
    def test_recall_puts_degraded_before_the_context(self, monkeypatch):
        """The GUI card holds the first 1,200 characters and the median recall
        result is 22,567: after `context` the flag never reached the screen."""
        monkeypatch.setattr("silica.kernel.recall.perception.facade_retrieve",
                            _fake_retrieve({"embed": "unreachable"}))
        from silica.tools.graph import silica_recall

        out = silica_recall("q", k=3, memory=False)
        assert list(out)[:4] == ["query", "degraded", "hint", "context"]
        assert out["degraded"] == {"embed": "unreachable"}
        assert out["hint"] == ("lexical only: the embedder is unreachable · start it in "
                               "Settings, Endpoints, or run silica doctor")

    def test_an_unscored_pool_alone_is_not_marked(self, monkeypatch):
        """No reranker configured, or gate 2b: `reranked` is False and the
        record holds no cause, so nothing is flagged (the old false alarm)."""
        monkeypatch.setattr("silica.kernel.recall.perception.facade_retrieve",
                            _fake_retrieve(reranked=False))
        from silica.tools.graph import silica_recall

        out = silica_recall("q", k=3, memory=False)
        assert "degraded" not in out and "hint" not in out

    def test_named_notes_retrieve_nothing_and_are_not_marked(self, monkeypatch, tmp_vault):
        tmp_vault.note("A.md", "alpha body")
        called = []
        monkeypatch.setattr("silica.kernel.recall.perception.facade_retrieve",
                            lambda *a, **k: called.append(1))
        from silica.tools.graph import silica_recall

        out = silica_recall("q", notes=["A"], memory=False)
        assert not called and "degraded" not in out

    def test_a_peek_appends_its_coverage_hint(self, monkeypatch, tmp_path):
        monkeypatch.setattr("silica.kernel.recall.perception.facade_retrieve",
                            _fake_retrieve({"rerank": "loading"}))
        monkeypatch.setattr("silica.tools.graph._peek_target", lambda v: str(tmp_path))
        monkeypatch.setattr("silica.kernel.recall.vault_registry.coverage",
                            lambda p: {"level": "cold"})
        from silica.tools.graph import silica_recall

        out = silica_recall("q", k=3, memory=False, vault=str(tmp_path))
        first, second = out["hint"].split(" · ", 1)
        assert first == "not reranked: the reranker is still loading"
        assert second.startswith("no recall index yet")

    def test_semantic_search_puts_degraded_before_the_results(self, monkeypatch):
        """The headline case: with the embedder down and the reranker up, the
        reply read `reranked: true` and looked healthy."""
        monkeypatch.setattr("silica.kernel.recall.perception.facade_retrieve",
                            _fake_retrieve({"embed": "unreachable"}))
        from silica.tools.graph import silica_semantic_search

        out = silica_semantic_search("q", k=3)
        assert list(out)[:3] == ["query", "degraded", "hint"]
        assert out["degraded"] == {"embed": "unreachable"}


class TestTheLine:
    @pytest.fixture(autouse=True)
    def _timeouts(self, monkeypatch):
        from silica.config import CONFIG

        monkeypatch.setattr(CONFIG, "embedding_timeout", 0.2)
        monkeypatch.setattr(CONFIG, "rerank_timeout", 5.0)

    def test_the_adr_examples_word_for_word(self):
        assert degraded_lines({"embed": "unreachable"}) == [
            "lexical only: the embedder is unreachable · start it in Settings, Endpoints"]
        assert degraded_lines({"rerank": "slow"}) == [
            "not reranked: the reranker did not answer in 5 s · raise SILICA_RERANK_TIMEOUT"]

    def test_every_cause(self):
        assert degraded_lines({"embed": "slow"}) == [
            "lexical only: the embedder did not answer in 0.2 s · raise SILICA_EMBEDDING_TIMEOUT"]
        assert degraded_lines({"embed": "loading"}) == ["lexical only: the embedder is still loading"]
        assert degraded_lines({"embed": "rejected"}) == [
            "lexical only: the embedder refused the model name · Settings, Diagnostics names the cause"]
        assert degraded_lines({"rerank": "failed"}) == [
            "not reranked: the reranker failed · Settings, Diagnostics names the cause"]

    def test_the_repl_and_the_tool_name_their_own_fix(self):
        assert degraded_lines({"embed": "unreachable"}, "repl") == [
            "lexical only: the embedder is unreachable · run silica doctor"]
        assert degraded_lines({"rerank": "failed"}, "repl") == [
            "not reranked: the reranker failed · run silica doctor"]
        assert degraded_lines({"embed": "slow"}, "repl") == [
            "lexical only: the embedder did not answer in 0.2 s · raise SILICA_EMBEDDING_TIMEOUT"]
        assert degraded_lines({"rerank": "rejected"}, "tool") == [
            "not reranked: the reranker refused the model name · Settings, Diagnostics "
            "names the cause, or run silica doctor"]

    def test_one_line_per_leg_embed_first(self):
        assert [line.split(":")[0] for line in
                degraded_lines({"rerank": "failed", "embed": "unreachable"})] == [
            "lexical only", "not reranked"]


def _done(name: str, payload: dict):
    import json

    from silica.agent.events import ToolCompleteEvent

    return ToolCompleteEvent(name=name, args={}, call_id="c", result=json.dumps(payload),
                             duration_s=0.0, iteration=1)


class TestTheTurnRollup:
    def test_the_first_cause_per_leg_wins_across_calls(self):
        w = RecallWatch()
        w(_done("silica_recall", {"notes": ["a"], "degraded": {"rerank": "slow"}}))
        w(_done("silica_semantic_search", {"results": [1], "degraded": {"rerank": "failed",
                                                                       "embed": "unreachable"}}))
        assert w.degraded == {"rerank": "slow", "embed": "unreachable"}

    def test_a_turn_that_never_searched_by_meaning_is_not_thin(self):
        """Its misses, silica_search_context's included, say nothing about
        coverage, and the web is the wrong fix."""
        w = RecallWatch()
        w(_done("silica_recall", {"notes": [], "degraded": {"embed": "unreachable"}}))
        w(_done("silica_search_context", {"notes_matched": 0}))
        assert w.thin is False
        assert [n["text"] for n in w.notes()] == [
            "lexical only: the embedder is unreachable · start it in Settings, Endpoints"]

    def test_a_rerank_only_turn_keeps_the_thin_rule_and_both_lines_show(self):
        """A reorder cannot change membership (rerank.py: recall@k is
        rerank-invariant), so a miss is still a miss; the cause comes first."""
        w = RecallWatch(surface="repl")
        w(_done("silica_recall", {"notes": [], "degraded": {"rerank": "unreachable"}}))
        assert w.thin is True
        assert w.notes() == [
            {"kind": "retrieval", "text": "not reranked: the reranker is unreachable · run silica doctor"},
            {"kind": "retrieval", "text": THIN_COVERAGE_HINT}]

    def test_an_error_reply_still_names_its_leg(self):
        """No index at all and the embedder down: the reply is an error, and the
        error alone sends the reader to rebuild an index that is fine."""
        w = RecallWatch()
        w(_done("silica_semantic_search", {"error": "No index", "degraded": {"embed": "slow"}}))
        assert w.degraded == {"embed": "slow"}


def test_the_doctor_compares_the_index_width_with_the_embedder(monkeypatch, tmp_vault):
    from silica.config import CONFIG
    from silica.kernel.recall.embed import get_store
    from silica.onboarding import checks

    store = get_store()
    store.upsert("a", "a", [0.1] * 1024)
    store.upsert("b", "b", [0.2] * 1024)
    monkeypatch.setattr(CONFIG, "embedding_model", "fake-embed")

    def probe(width):
        def post(url, json=None, timeout=None):
            return httpx.Response(200, json={"data": [{"embedding": [0.1] * width}]},
                                  request=httpx.Request("POST", url))
        monkeypatch.setattr(checks.httpx, "post", post)

    probe(768)
    r = checks.check_embeddings(CONFIG)
    assert r.status == "warn"
    assert "1024-dim" in r.detail and "768" in r.detail and "fake-embed" in r.detail
    probe(1024)
    assert checks.check_embeddings(CONFIG).status == "ok"
