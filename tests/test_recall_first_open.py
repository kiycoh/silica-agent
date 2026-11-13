# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0173: the app builds recall's stores the first time it opens a vault,
and until they are there a recall says so and answers from the passages.

Seen 2026-11-13 on a fresh copy of the demo vault: the app built the passage
index and the page vectors at start, while co-occurrence, lexical and
embeddings came only from /cooccur, /lexical and /embed. silica_recall answered
an empty context with no flag, the chat said the vault had nothing, and
answered Galileo's birth, which a note holds, from general knowledge.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from silica.agent.recall_watch import RecallWatch
from silica.config import CONFIG
from silica.kernel.recall import refresh
from silica.kernel.recall.search import degraded_lines
from silica.obs.events import ToolCompleteEvent
from silica.ui.web.vectors import _builds as real_builds  # before conftest turns it off

# The demo vault's shape: the birth is in a section that does not name him, and
# the sections that do outrank it for the body search's two per note.
GALILEO = ("# Galileo Galilei\n\nGalileo Galilei (1564 to 1642) was an astronomer. Galileo argued "
           "that the earth moves.\n\n## Life\n\nHe was born in Pisa on 15 February 1564.\n\n"
           "## The trial\n\nGalileo was tried in 1633. Galileo recanted. Galileo was held at Arcetri.\n")


class _Embedder:
    """Two numbers per text, and a count of the texts it was asked for."""

    model = "fake-embed"

    def __init__(self):
        self.texts = 0

    def embed(self, texts, query=False):
        self.texts += len(texts)
        return [[float(len(t) % 7 + 1), 1.0] for t in texts]


@pytest.fixture
def vault(tmp_vault):
    tmp_vault.note("Culture/Science/Galileo Galilei.md", GALILEO)
    tmp_vault.note("Culture/Art/Caravaggio.md", "# Caravaggio\n\nHe painted with a hard light from one side.\n")
    return tmp_vault


def _stores() -> tuple[int, bool, int]:
    from silica.kernel.base import home
    from silica.kernel.recall.cooccurrence import get_cooccur_store
    from silica.kernel.recall.embed import get_store

    return (len(get_cooccur_store(lang=CONFIG.cooccurrence_lang).paths()), home.lexical_built(),
            len(get_store()))


class TestTheFirstBuild:
    def test_a_fresh_vault_lacks_every_store_and_gets_the_local_two_without_a_model(self, vault):
        assert refresh.missing(embed=True) == ["cooccurrence", "lexical", "embeddings"]
        done = refresh.build_missing()
        assert set(done) == {"cooccurrence", "lexical"}
        assert _stores() == (2, True, 0)
        assert refresh.missing(embed=True) == ["embeddings"]
        assert refresh.missing(embed=False) == []

    def test_the_embeddings_come_with_an_embedder_and_no_figure_reading(self, vault, monkeypatch):
        """refresh.embed reads every unread figure with the chat model first; a
        build nobody asked for must not spend the person's key on that."""
        monkeypatch.setattr("silica.kernel.recall.image_readings.read_missing",
                            lambda *a, **k: pytest.fail("the first build paid for a figure reading"))
        embedder = _Embedder()
        steps: list[tuple[str, int, int]] = []
        done = refresh.build_missing(embedder=embedder, progress=lambda *s: steps.append(s))
        assert set(done) == {"cooccurrence", "lexical", "embeddings"}
        assert _stores()[2] == 2 and embedder.texts == 4  # a full and a title text per note
        assert steps[-1] == ("vectors", 2, 2)
        assert refresh.missing(embed=True) == []

    def test_a_store_already_there_is_left_as_it_is(self, vault):
        refresh.build_missing()
        from silica.kernel.recall.lexical import _index_path

        before = _index_path().stat().st_mtime_ns
        assert refresh.build_missing() == {}
        assert _index_path().stat().st_mtime_ns == before

    def test_an_empty_vault_builds_nothing(self, tmp_vault):
        assert refresh.build_missing() == {}
        assert not refresh.building()

    def test_it_says_it_is_building_while_it_builds_and_the_sweep_stands_down(self, vault, monkeypatch):
        """A first batch of vectors in the shared store made the read path's
        sweep take the store as seeded and embed the rest inline, twice."""
        from silica.kernel.recall import sync

        seen: dict = {}

        def progress(stage, done, total):
            seen.setdefault("building", refresh.building("embeddings"))
            seen.setdefault("swept", sync.sweep(force=True).skipped)

        refresh.build_missing(embedder=_Embedder(), progress=progress)
        assert seen == {"building": True, "swept": True}
        assert not refresh.building()

    def test_a_vault_switched_mid_build_stops_it(self, vault):
        class Switched(Exception):
            pass

        def progress(stage, done, total):
            raise Switched

        with pytest.raises(Switched):
            refresh.build_missing(embedder=_Embedder(), progress=progress)
        assert not refresh.building()


class TestTheRecallSaysSo:
    def test_no_store_and_notes_in_the_vault_is_a_missing_index(self, vault):
        from silica.kernel.recall import perception

        stats: dict = {}
        assert perception.facade_retrieve("Galileo", k=3, use_memory=False, leg_stats=stats) == (None, None)
        assert stats["degraded"] == {"index": "missing"}

    def test_a_store_of_vectors_whose_embedder_failed_is_not_a_missing_index(self, vault, monkeypatch):
        """A hosted import's shape: embeddings and no co-occurrence. With the
        query embed failing, the line is the embedder's, not "never built"."""
        import openai
        import httpx

        from silica.kernel.recall import perception

        class _Store:
            def __len__(self):
                return 30

        class _Slow:
            def embed(self, texts, query=False):
                raise openai.APITimeoutError(request=httpx.Request("POST", "http://127.0.0.1:9/v1"))

        monkeypatch.setattr("silica.kernel.recall.embed.get_store", lambda: _Store())
        monkeypatch.setattr("silica.model.providers.get_embedder", lambda config: _Slow())
        stats: dict = {}
        assert perception.facade_retrieve("Galileo", k=3, use_memory=False, leg_stats=stats) == (None, None)
        assert stats["degraded"] == {"embed": "slow"}

    def test_while_the_app_builds_it_is_building(self, vault, monkeypatch):
        from silica.kernel.recall import perception

        monkeypatch.setattr(refresh, "building", lambda store=None: True)
        stats: dict = {}
        perception.facade_retrieve("Galileo", k=3, use_memory=False, leg_stats=stats)
        assert stats["degraded"] == {"index": "building"}

    def test_an_empty_vault_has_nothing_to_index_and_is_not_marked(self, tmp_vault):
        """A new hosted tenant's vault: nothing ever builds its stores, and a
        flag there would hold back every general-knowledge block it gets."""
        from silica.kernel.recall import perception

        stats: dict = {}
        perception.facade_retrieve("Galileo", k=3, use_memory=False, leg_stats=stats)
        assert "degraded" not in stats

    def test_embeddings_still_building_are_named_beside_the_co_occurrence_that_answers(self, vault, monkeypatch):
        from silica.kernel.recall import perception

        refresh.build_missing()
        monkeypatch.setattr(refresh, "building", lambda store=None: store == "embeddings")
        stats: dict = {}
        results, _vec = perception.facade_retrieve("Galileo Pisa", k=3, use_memory=False, use_rerank=False,
                                                   leg_stats=stats)
        assert results and stats["degraded"] == {"embed": "building"}

    def test_embeddings_half_built_are_still_building_though_the_query_embeds(self, vault, monkeypatch):
        """The build fills the shared store 32 notes at a time: after the first
        chunk the query embeds, and the store holds a part of the vault."""
        from silica.kernel.recall import perception

        refresh.build_missing()
        embedder = _Embedder()
        refresh._embed_all(embedder, lambda *s: None)  # the store as the build leaves it
        monkeypatch.setattr("silica.model.providers.get_embedder", lambda config: embedder)
        monkeypatch.setattr(refresh, "building", lambda store=None: store == "embeddings")
        stats: dict = {}
        results, vec = perception.facade_retrieve("Galileo Pisa", k=3, use_memory=False, use_rerank=False,
                                                  leg_stats=stats)
        assert vec is not None and results and stats["degraded"] == {"embed": "building"}

    def test_co_occurrence_being_filled_is_no_leg_yet(self, vault, monkeypatch):
        """The same singleton a request reads is the one the build fills."""
        from silica.kernel.recall import perception

        refresh.build_missing()
        monkeypatch.setattr(refresh, "building", lambda store=None: store in (None, "cooccurrence"))
        stats: dict = {}
        assert perception.facade_retrieve("Galileo Pisa", k=3, use_memory=False, leg_stats=stats) == (None, None)
        assert stats["degraded"] == {"index": "building"}

    def test_recall_answers_from_the_passages_with_its_flag_first(self, vault):
        from silica.tools.graph import silica_recall

        out = silica_recall("When was Galileo born?", k=5, memory=False)
        assert list(out)[:4] == ["query", "degraded", "hint", "context"]
        assert out["degraded"] == {"index": "missing"}
        assert "15 February 1564" in out["context"], "the note as recall assembles it, not two sections"
        assert out["notes"][0] == "Culture/Science/Galileo Galilei"
        assert out["partial"] == [], "a short note arrives whole"

    def test_recall_on_an_empty_vault_is_plain_empty(self, tmp_vault):
        from silica.tools.graph import silica_recall

        out = silica_recall("When was Galileo born?", k=5, memory=False)
        assert "degraded" not in out and out["notes"] == []

    def test_a_filtered_recall_does_not_fall_back_to_a_search_that_ignores_the_filter(self, vault):
        from silica.tools.graph import silica_recall

        out = silica_recall("When was Galileo born?", k=5, memory=False, min_trust="human")
        assert out["degraded"] == {"index": "missing"} and out["notes"] == []


class TestTheLines:
    def test_each_cause_reads_as_the_index_and_not_as_an_endpoint(self):
        assert degraded_lines({"index": "building"}) == [
            "passages only: the recall index is still being built"]
        assert degraded_lines({"index": "missing"}, "repl") == [
            "passages only: the recall index was never built · run /cooccur and /embed"]
        assert degraded_lines({"embed": "building"}) == [
            "partly by meaning: the embedding index is still being built"]
        assert [line.split(":")[0] for line in degraded_lines({"rerank": "slow", "index": "building"})] == [
            "passages only", "not reranked"]


def _done(name: str, payload: dict) -> ToolCompleteEvent:
    return ToolCompleteEvent(name=name, args={}, call_id="c", result=json.dumps(payload),
                             duration_s=0.0, iteration=1)


class TestTheTurn:
    @pytest.fixture(autouse=True)
    def _tools(self):
        import silica.cli  # noqa: F401  registers every tool

    def test_a_degraded_recall_that_found_nothing_is_not_a_search_of_the_notes(self):
        """The owner's rule is general knowledge only once the vault came up
        empty, and a search without its index cannot show that."""
        w = RecallWatch()
        w(_done("silica_recall", {"notes": [], "degraded": {"index": "building"}}))
        assert not w.looked and w.unsure and not w.thin
        w(_done("silica_search_context", {"hits": [], "notes_matched": 0}))
        assert w.looked

    def test_a_lost_reranker_still_searched_the_notes(self):
        """A reorder cannot change membership, as `thin` already holds."""
        w = RecallWatch()
        w(_done("silica_recall", {"notes": [], "degraded": {"rerank": "slow"}}))
        assert w.looked and not w.unsure

    def test_a_degraded_recall_that_found_notes_did_look(self):
        w = RecallWatch()
        w(_done("silica_recall", {"notes": ["Galileo Galilei"], "degraded": {"index": "missing"}}))
        assert w.looked and not w.unsure

    def test_a_write_or_a_memory_saved_is_a_write(self):
        w = RecallWatch()
        w(_done("silica_read_note", {"content": "x"}))
        assert not w.wrote
        w(_done("silica_write_note", {"error": "refused"}))
        assert not w.wrote, "a refused write wrote nothing"
        w(_done("silica_remember", {"saved": "exam on Thursday"}))
        assert w.wrote


class TestTheApp:
    @pytest.fixture
    def builds(self, vault, monkeypatch):
        from silica.ui.web import vectors

        monkeypatch.setattr(vectors, "_builds", lambda: True)
        monkeypatch.setattr(vectors, "_STATUS", {"state": "idle"})
        return vectors

    def _wait(self, vectors) -> None:
        if (thread := vectors._THREAD) is not None:
            thread.join(10)
        assert vectors._THREAD is None

    def test_opening_a_vault_builds_the_local_stores_with_no_embedder_here(self, builds):
        """An embedder elsewhere (conftest's _eligible is False) still gets
        co-occurrence and lexical: neither calls a model."""
        assert builds.refresh() is True
        self._wait(builds)
        assert _stores() == (2, True, 0)
        assert builds.status()["state"] == "idle"

    def test_with_an_embedder_on_this_machine_the_embeddings_too(self, builds, monkeypatch):
        embedder = _Embedder()
        monkeypatch.setattr(builds, "_eligible", lambda: True)
        monkeypatch.setattr(builds, "_embedder_up", lambda: True)
        monkeypatch.setattr("silica.model.providers.get_embedder", lambda config: embedder)
        monkeypatch.setattr("silica.kernel.recall.engine.engine",
                            lambda: pytest.fail("the page vectors are not this test's"))
        states: list[str] = []
        real_set = builds._set
        monkeypatch.setattr(builds, "_set", lambda **s: states.append(s["state"]) or real_set(**s))
        monkeypatch.setattr(builds, "_page_vectors", lambda vault, progress: None)
        builds.refresh()
        self._wait(builds)
        assert _stores() == (2, True, 2)
        assert states[:3] == ["index", "index", "waiting"] and "vectors" in states

    def test_a_hosted_tenant_is_left_as_it_was(self, vault, monkeypatch):
        """A tenant's stores come from its imports and its runs (ADR-0085)."""
        from silica.ui.web import vectors

        monkeypatch.setattr(vectors, "_builds", real_builds)
        assert real_builds() is True
        monkeypatch.setenv("SILICA_HOSTED", "1")
        assert real_builds() is False and vectors.refresh() is False

    def test_a_note_written_and_moved_after_the_build_is_kept_current(self, builds, tmp_vault, monkeypatch):
        """Through the sweep the job ends with: the moved note's old key goes
        from all three stores and its new one comes in."""
        monkeypatch.setattr(CONFIG, "index_sweep", True)  # conftest turns it off
        builds.refresh()
        self._wait(builds)
        from silica.driver import DRIVER
        from silica.kernel.recall.cooccurrence import get_cooccur_store
        from silica.kernel.recall.lexical import get_lexical_store

        DRIVER.create("Culture/Science/Kepler.md", "# Kepler\n\nKepler worked out the orbits.\n")  # as a chat write lands
        DRIVER.move("Culture/Art/Caravaggio.md", "Culture/Caravaggio.md")
        builds.refresh()
        self._wait(builds)
        keys = set(get_cooccur_store(lang=CONFIG.cooccurrence_lang).paths())
        assert "Culture/Science/Kepler" in keys and "Culture/Caravaggio" in keys
        assert "Culture/Art/Caravaggio" not in keys
        lexical = set(get_lexical_store().paths())
        assert {"Culture/Science/Kepler", "Culture/Caravaggio"} <= lexical and "Culture/Art/Caravaggio" not in lexical

    def test_a_chat_turn_that_wrote_asks_for_the_indexes_and_one_that_read_does_not(self, builds, monkeypatch):
        from silica.ui.web import turn as web_turn

        asked: list[int] = []
        monkeypatch.setattr(builds, "refresh", lambda: asked.append(1) or True)

        class _T:
            read = RecallWatch()

        t = _T()
        web_turn._reindex(t)
        assert asked == []
        t.read.wrote = True
        web_turn._reindex(t)
        assert asked == [1]
        assert web_turn._reindex in web_turn._AFTER

    def test_a_note_moved_in_the_tree_asks_for_them_too(self, builds, monkeypatch):
        from fastapi.testclient import TestClient

        from silica.ui.web import server

        asked: list[int] = []
        monkeypatch.setattr(builds, "refresh", lambda: asked.append(1) or True)
        r = TestClient(server.app).post("/tree/move", json={"paths": ["Culture/Art/Caravaggio.md"], "folder": "Culture"})
        assert r.status_code == 200 and r.json()["moved"]
        assert asked == [1]
        assert (Path(CONFIG.vault_path) / "Culture/Caravaggio.md").is_file()


def test_the_build_flag_is_the_threads_own(vault):
    """`building` is read from request threads while the job's thread builds."""
    gate, inside = threading.Event(), threading.Event()

    def progress(stage, done, total):
        inside.set()
        gate.wait(5)

    t = threading.Thread(target=refresh.build_missing, kwargs={"progress": progress})
    t.start()
    assert inside.wait(5)
    assert refresh.building() and refresh.building("cooccurrence")
    gate.set()
    t.join(5)
    assert not refresh.building()
