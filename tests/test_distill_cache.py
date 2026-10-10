# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Distiller replies cached under two fingerprints: the prompt names the
namespace, the call inputs name the entry.

The split is the whole point. Every prompt experiment on record had to
re-distill the corpus to change one lens, which re-rolled every note the lens
never touched and left the two arms incomparable — the runner's own docstring
names the confound. Namespacing on the prompt makes an unchanged input under
an unchanged prompt a replay instead of a re-roll, and makes a changed prompt
unable to read a single entry the old one wrote.
"""
import json
from unittest import mock

import pytest

from silica.kernel import distill_cache, prep_delegation


class TestFingerprints:
    def test_the_prompt_names_the_namespace(self):
        a = distill_cache.prompt_fingerprint("lens A\nrules")
        assert a == distill_cache.prompt_fingerprint("lens A\nrules")
        assert a != distill_cache.prompt_fingerprint("lens A\nrule")
        assert len(a) == 12

    def test_the_entry_key_does_not_depend_on_dict_order(self):
        """Canonical JSON, or the same call would key twice depending on how
        the caller happened to build the dict."""
        assert (distill_cache.entry_key({"a": 1, "b": [2, 3]})
                == distill_cache.entry_key({"b": [2, 3], "a": 1}))
        assert (distill_cache.entry_key({"a": 1})
                != distill_cache.entry_key({"a": 2}))


class TestNamespaceIsolation:
    def test_a_changed_prompt_cannot_read_what_the_old_one_wrote(self):
        """The load-bearing property: an A/B arm never reads the other's work,
        so a prompt delta is measured on replies that prompt actually made."""
        old, new = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
        distill_cache.store(old, "k", {"updates": [{"op": "skip"}]})

        assert distill_cache.load(new, "k") is None
        assert distill_cache.load(old, "k") == {"updates": [{"op": "skip"}]}

    def test_the_old_namespace_survives_the_new_one(self):
        """Entries are not overwritten across namespaces, so switching a lens
        back does not pay for the corpus a second time."""
        distill_cache.store("aaaaaaaaaaaa", "k", {"updates": ["old"]})
        distill_cache.store("bbbbbbbbbbbb", "k", {"updates": ["new"]})

        assert distill_cache.load("aaaaaaaaaaaa", "k") == {"updates": ["old"]}
        assert distill_cache.load("bbbbbbbbbbbb", "k") == {"updates": ["new"]}


class TestDurability:
    def test_a_corrupt_entry_reads_as_a_miss(self):
        """A half-written entry must cost one re-run, never a crashed batch."""
        distill_cache.store("aaaaaaaaaaaa", "k", {"updates": []})
        path = distill_cache.entry_path("aaaaaaaaaaaa", "k")
        path.write_text("{not json", encoding="utf-8")

        assert distill_cache.load("aaaaaaaaaaaa", "k") is None

    def test_an_absent_namespace_is_a_miss_not_an_error(self):
        assert distill_cache.load("cccccccccccc", "k") is None

    def test_an_entry_that_is_not_an_object_reads_as_a_miss(self):
        """A stored list would flow into the caller as a reply shape it never
        validates; refuse it at the boundary."""
        path = distill_cache.entry_path("aaaaaaaaaaaa", "k")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2]", encoding="utf-8")

        assert distill_cache.load("aaaaaaaaaaaa", "k") is None


CLEAN = {
    "schema_version": 1,
    "batches": [{"inbox_file": "/abs/inbox/x.md",
                 "concepts": [{"name": "concept", "action_hint": "create",
                               "inbox_excerpt": "plain prose, no formulas",
                               "vault_collision": None}]}],
}

REPLY = ('{"main_thematic_axes":["prose"],"updates":['
         '{"op":"write","heading":"Plain","source_basename":"x.md",'
         '"path":"Target/Plain.md","hub":"Hub","snippet":"a plain body"}],'
         '"ephemerals":[]}')


class _FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def call_llm(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        text, finish = item if isinstance(item, tuple) else (item, "stop")
        return mock.Mock(text=text, tool_calls=[], finish_reason=finish)


@pytest.fixture
def distill(monkeypatch):
    """Pin every seam around run_distiller so only the cache varies."""
    monkeypatch.setenv("MODEL_CONTEXT_WINDOW", "32768")
    monkeypatch.setenv("DISTILLER_MAX_TOKENS", "2048")
    monkeypatch.delenv("SILICA_DISTILL_TWO_PASS", raising=False)
    monkeypatch.delenv("SILICA_DISTILL_PROFILE", raising=False)
    monkeypatch.setattr(prep_delegation, "active_distill_profile",
                        lambda: "default")

    def _no_network(*a, **k):
        raise RuntimeError("litellm fallback must never reach the network in tests")

    monkeypatch.setattr("silica.agent.llm.call_llm", _no_network)

    def _run(responses, **kwargs):
        fake = _FakeProvider(responses)
        monkeypatch.setattr("silica.agent.providers.get_provider",
                            lambda *a, **k: fake)
        result = prep_delegation.run_distiller(
            payload=CLEAN, target="Target", session_date="2026-08-02", **kwargs)
        return fake, result

    return _run


class TestReplayInsteadOfReRoll:
    def test_the_cache_is_off_unless_asked_for(self, distill):
        """A live vault must not be served a stale reply for an edited note;
        the replay semantics are for a frozen arm, so they are opt-in."""
        first, _ = distill([REPLY])
        second, _ = distill([REPLY])

        assert len(first.calls) == 1
        assert len(second.calls) == 1

    def test_a_second_identical_call_replays(self, distill, monkeypatch):
        monkeypatch.setenv("SILICA_DISTILL_CACHE", "1")
        _, first = distill([REPLY])
        # One reply queued for two calls: a miss would raise IndexError.
        fake, second = distill([])

        assert fake.calls == []
        assert second == first

    def test_a_changed_prompt_forces_a_fresh_call(self, distill, monkeypatch):
        """The namespace follows the lens, so switching profiles cannot serve
        the previous lens's reply."""
        monkeypatch.setenv("SILICA_DISTILL_CACHE", "1")
        distill([REPLY])
        fake, _ = distill([REPLY], profile="extractive")

        assert len(fake.calls) == 1

    def test_a_changed_payload_forces_a_fresh_call(self, distill, monkeypatch):
        monkeypatch.setenv("SILICA_DISTILL_CACHE", "1")
        distill([REPLY])

        other = json.loads(json.dumps(CLEAN))
        other["batches"][0]["concepts"][0]["inbox_excerpt"] = "different source"
        fake = _FakeProvider([REPLY])
        monkeypatch.setattr("silica.agent.providers.get_provider",
                            lambda *a, **k: fake)
        prep_delegation.run_distiller(payload=other, target="Target",
                                      session_date="2026-08-02")

        assert len(fake.calls) == 1

    def test_a_failed_reply_is_never_stored(self, distill, monkeypatch):
        """Caching an error would freeze a transient provider fault into the
        corpus for every later run of that arm."""
        monkeypatch.setenv("SILICA_DISTILL_CACHE", "1")
        _, failed = distill(["not json at all"])
        assert "error" in failed

        fake, recovered = distill([REPLY])
        assert len(fake.calls) == 1
        assert recovered["updates"][0]["snippet"] == "a plain body"


TRUNCATED = ('{"main_thematic_axes":["prose"],"updates":['
             '{"op":"write","heading":"Plain","source_basename":"x.md",'
             '"path":"Target/Plain.md","hub":"Hub","snippet":"a plain body"},'
             '{"op":"write","heading":"Cut')

STRUCTURE = ('{"main_thematic_axes":["prose"],"updates":['
             '{"op":"write","heading":"Plain","source_basename":"x.md",'
             '"path":"Target/Plain.md","hub":"Hub"}],"ephemerals":[]}')


class TestOnlyCompleteRepliesEnterTheCache:
    """A salvaged, truncated, bodyless or fallback-served reply is a recovery
    input. Freezing one would replay the loss on every later run of the arm."""

    def test_a_salvaged_prefix_is_not_stored(self, distill, monkeypatch):
        monkeypatch.setenv("SILICA_DISTILL_CACHE", "1")
        _, partial = distill([TRUNCATED])
        assert len(partial["updates"]) == 1   # salvage kept the valid prefix

        fake, full = distill([REPLY])
        assert len(fake.calls) == 1           # miss: the partial was not frozen
        assert full["updates"][0]["snippet"] == "a plain body"

    def test_a_length_truncated_reply_is_not_stored(self, distill, monkeypatch):
        """finish_reason == "length" means the tail is lost even when the
        prefix still parses."""
        monkeypatch.setenv("SILICA_DISTILL_CACHE", "1")
        distill([(REPLY, "length")])

        fake, _ = distill([REPLY])
        assert len(fake.calls) == 1

    def test_a_fallback_served_reply_is_not_stored(self, distill, monkeypatch):
        """The cache key names the worker model; a reply served by the litellm
        fallback came from the router model and must not blend the corpora."""
        monkeypatch.setenv("SILICA_DISTILL_CACHE", "1")
        monkeypatch.setattr("silica.agent.providers.get_provider",
                            mock.Mock(side_effect=RuntimeError("worker down")))
        monkeypatch.setattr("silica.agent.llm.call_llm",
                            lambda **k: mock.Mock(text=REPLY, tool_calls=[],
                                                  finish_reason="stop"))
        served = prep_delegation.run_distiller(
            payload=CLEAN, target="Target", session_date="2026-08-02")
        assert served["updates"]

        fake, _ = distill([REPLY])
        assert len(fake.calls) == 1

    def test_a_bodyless_stitch_is_not_stored(self, distill, monkeypatch):
        """A pass 2 that never delivered its ===SILICA-BODY N=== block left the
        op bodyless; replaying it would floor-reject that op forever."""
        monkeypatch.setenv("SILICA_DISTILL_CACHE", "1")
        monkeypatch.setattr(prep_delegation, "needs_body_pass", lambda p: True)
        distill([STRUCTURE, "no body blocks here"])

        fake, _ = distill([STRUCTURE, "===SILICA-BODY 1===\na body"])
        assert len(fake.calls) == 2           # miss: structure + body re-run

    def test_a_complete_stitch_is_stored(self, distill, monkeypatch):
        """The completeness gate must not over-fire: a clean two-pass reply
        replays like any other success."""
        monkeypatch.setenv("SILICA_DISTILL_CACHE", "1")
        monkeypatch.setattr(prep_delegation, "needs_body_pass", lambda p: True)
        _, first = distill([STRUCTURE, "===SILICA-BODY 1===\na body"])
        assert first["updates"][0]["snippet"] == "a body"

        fake, second = distill([])
        assert fake.calls == []
        assert second == first
