# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Every supersede records how far the arrival sat from the fact it buried.

A same-key arrival is either an update of one referent or a different referent
dropped into a reused slot, and only the second is a loss. The distiller does
both and the store cannot tell them apart, so the burial rate was measurable
only by a one-off script over frozen stores — which means it was invisible
whenever a prompt or a model changed, i.e. exactly when it moves.

The mark changes nothing about what supersedes: it is a number written next to
a decision already taken. The cosine is the same TEXT signal the supersede gate
was sized on (updates cluster >= ~0.83, collisions around 0.53, the hand
labelled 0.55-0.70 band shows no internal separation), computed after the batch
embed so it costs no extra request.
"""
import pytest

from silica.kernel.recall.episodic import EpisodicStore


def _store(tmp_path):
    return EpisodicStore(path=tmp_path / "episodic.json")


class _TextEmbedder:
    """Fixed vec per fact text, so the cosine is exact and readable."""

    _TABLE = {
        "the dog is named Tom": [1.0, 0.0, 0.0],
        "the dog is named Rex": [0.9, 0.436, 0.0],          # cos vs Tom ~ 0.9
        "pottery class started yesterday": [0.0, 1.0, 0.0],  # cos vs Tom = 0
    }

    def embed(self, texts):
        return [self._TABLE.get(t, [0.0, 0.0, 1.0]) for t in texts]


def _capture(store, key, text, run, embedder=_TextEmbedder()):
    store.capture([{"key": key, "text": text}], run_id=run, seen="2026-08-02",
                  embedder=embedder)


class TestTheMarkIsRecorded:
    def test_a_reused_slot_records_the_distance_to_what_it_buried(self, tmp_path):
        store = _store(tmp_path)
        _capture(store, "user.event.date", "the dog is named Tom", "r1")
        _capture(store, "user.event.date", "pottery class started yesterday", "r2")

        head = [f for f in store.facts if f.status == "live"][0]
        assert head.supersede_cos == pytest.approx(0.0, abs=1e-6)

    def test_a_genuine_update_records_a_high_distance(self, tmp_path):
        store = _store(tmp_path)
        _capture(store, "user.dog.name", "the dog is named Tom", "r1")
        _capture(store, "user.dog.name", "the dog is named Rex", "r2")

        head = [f for f in store.facts if f.status == "live"][0]
        assert head.supersede_cos == pytest.approx(0.9, abs=1e-3)

    def test_a_new_key_buries_nothing_and_records_nothing(self, tmp_path):
        store = _store(tmp_path)
        _capture(store, "user.dog.name", "the dog is named Tom", "r1")

        assert store.facts[0].supersede_cos is None

    def test_a_reinforcement_is_not_a_burial(self, tmp_path):
        """Same key, same text: the head is touched, nothing is superseded."""
        store = _store(tmp_path)
        _capture(store, "user.dog.name", "the dog is named Tom", "r1")
        _capture(store, "user.dog.name", "the dog is named Tom", "r2")

        assert len(store.facts) == 1
        assert store.facts[0].supersede_cos is None


class TestItChangesNothing:
    def test_the_chain_is_identical_with_and_without_an_embedder(self, tmp_path):
        """The mark is an observation. Without vectors it is simply absent,
        and the store's shape must not move because of it."""
        marked, plain = _store(tmp_path / "a"), _store(tmp_path / "b")
        for store, emb in ((marked, _TextEmbedder()), (plain, None)):
            _capture(store, "user.event.date", "the dog is named Tom", "r1", emb)
            _capture(store, "user.event.date", "pottery class started yesterday",
                     "r2", emb)

        shape = lambda s: [(f.key, f.text, f.status, f.supersedes) for f in s.facts]
        assert shape(marked) == shape(plain)
        assert [f.supersede_cos for f in plain.facts] == [None, None]


class TestTheRate:
    def test_it_separates_collisions_from_updates(self, tmp_path):
        store = _store(tmp_path)
        _capture(store, "user.dog.name", "the dog is named Tom", "r1")
        _capture(store, "user.dog.name", "the dog is named Rex", "r2")    # update
        _capture(store, "user.event.date", "the dog is named Tom", "r3")
        _capture(store, "user.event.date", "pottery class started yesterday", "r4")

        stats = store.burial_stats()
        assert stats["supersedes"] == 2
        assert stats["collisions"] == 1
        assert stats["updates"] == 1
        assert stats["unmeasured"] == 0
        assert stats["collision_rate"] == pytest.approx(0.5)

    def test_unmeasured_supersedes_are_reported_not_guessed(self, tmp_path):
        """No embedder means no signal. Counting those as updates would report
        a clean store precisely where nothing was checked."""
        store = _store(tmp_path)
        _capture(store, "user.event.date", "a", "r1", None)
        _capture(store, "user.event.date", "b", "r2", None)

        stats = store.burial_stats()
        assert stats["supersedes"] == 1
        assert stats["unmeasured"] == 1
        assert stats["collision_rate"] is None

    def test_an_empty_store_has_no_rate(self, tmp_path):
        assert _store(tmp_path).burial_stats()["collision_rate"] is None

    def test_the_threshold_is_a_parameter_not_a_verdict(self, tmp_path):
        """The band was measured on one corpus; a caller comparing two prompts
        must be able to move the line without editing the store."""
        store = _store(tmp_path)
        _capture(store, "user.dog.name", "the dog is named Tom", "r1")
        _capture(store, "user.dog.name", "the dog is named Rex", "r2")  # cos 0.9

        assert store.burial_stats(tau=0.95)["collisions"] == 1
        assert store.burial_stats(tau=0.70)["collisions"] == 0


class TestDurability:
    def test_the_mark_survives_a_reload(self, tmp_path):
        store = _store(tmp_path)
        _capture(store, "user.event.date", "the dog is named Tom", "r1")
        _capture(store, "user.event.date", "pottery class started yesterday", "r2")

        assert _store(tmp_path).burial_stats()["collisions"] == 1

    def test_a_store_written_before_the_field_loads_unchanged(self, tmp_path):
        """Additive: every frozen store and replay baseline still opens."""
        store = _store(tmp_path)
        _capture(store, "user.dog.name", "the dog is named Tom", "r1")
        # A store from before the field is by definition also from before the
        # npz format, so the fixture writes the legacy JSON text directly.
        import json as _json

        doc = {"schema_version": 1, "next_id": store.next_id,
               "facts": [{k: v for k, v in f.model_dump().items()
                          if k != "supersede_cos"} for f in store.facts]}
        (tmp_path / "episodic.json").write_text(
            _json.dumps(doc, ensure_ascii=False), encoding="utf-8")

        reloaded = _store(tmp_path)
        assert len(reloaded.facts) == 1
        assert reloaded.facts[0].supersede_cos is None


class TestTheRateHasAReader:
    """A count without a reader is not a signal. The digest is where the other
    episodic worklists already land, so the burial rate lands there too —
    but only when non-zero, so the routine case adds no line."""

    def _digest(self, tmp_path, monkeypatch):
        from silica.kernel.recall import episodic
        from silica.kernel.progress import ProgressLedger

        monkeypatch.setattr(episodic, "store_path",
                            lambda: tmp_path / "episodic.json")
        return ProgressLedger.new(mode="test").digest()

    def test_a_collision_reaches_the_digest(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        _capture(store, "user.event.date", "the dog is named Tom", "r1")
        _capture(store, "user.event.date", "pottery class started yesterday", "r2")

        text = self._digest(tmp_path, monkeypatch)
        assert "EPISODIC BURIAL: 1/1" in text
        assert "reused keys" in text

    def test_a_clean_store_adds_no_line(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        _capture(store, "user.dog.name", "the dog is named Tom", "r1")
        _capture(store, "user.dog.name", "the dog is named Rex", "r2")

        assert "EPISODIC BURIAL" not in self._digest(tmp_path, monkeypatch)
