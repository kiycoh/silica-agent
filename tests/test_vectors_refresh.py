"""The app keeps the engine's page vectors current (ADR-0156, amended
2026-11-08): a build at start and after every write, on a laptop whose
embedder is this machine's, one at a time, waiting for an embedder still
loading, and moving to the new vault when the person switches."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from silica.config import CONFIG
from silica.ui.web import vectors
from silica.ui.web.vectors import _eligible as real_eligible


def _fake_embed(texts):
    return [[float(len(t) % 7 + 1), 1.0, float("watchers" in t)] for t in texts]


@pytest.fixture
def engine(tmp_vault, monkeypatch):
    from silica_core import embeddings
    from silica_core.config import CONFIG as CORE

    monkeypatch.setattr(vectors, "_eligible", real_eligible)
    # This tree's embedder: the engine mirrors it into core's CONFIG at every call.
    monkeypatch.setattr(CONFIG, "embedding_base_url", "http://127.0.0.1:1234/v1")
    monkeypatch.setattr(CONFIG, "embedding_model", "fake")
    monkeypatch.setattr(CORE, "embedding_base_url", "")
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed)
    monkeypatch.setattr(embeddings, "_CACHE", None)
    monkeypatch.setattr("silica.onboarding.serve.ready", lambda base: True)
    monkeypatch.setattr(vectors, "_STATUS", {"state": "idle"})
    vault = Path(CONFIG.vault_path)
    (vault / "enoch.md").write_text("# Enoch\n\n## Watchers\n\nThe watchers descended on Hermon.\n", encoding="utf-8")
    return vault


def _wait() -> None:
    thread = vectors._THREAD
    if thread is not None:
        thread.join(10)
    assert vectors._THREAD is None


def test_a_refresh_builds_the_page_vectors_when_the_embedder_is_this_machines(engine):
    from silica_core import embeddings

    assert vectors.refresh() is True
    _wait()

    assert vectors.status()["state"] == "idle" and vectors.status()["sections"] == 1
    meta, _rows = embeddings.load()
    assert list(meta["docs"]) == ["enoch.md"]


def test_nothing_is_built_on_the_server_or_with_an_embedder_elsewhere(engine, monkeypatch):
    monkeypatch.setattr(CONFIG, "embedding_base_url", "https://api.example.com/v1")
    assert vectors.refresh() is False
    monkeypatch.setattr(CONFIG, "embedding_base_url", "http://127.0.0.1:1234/v1")
    monkeypatch.setenv("SILICA_HOSTED", "1")
    assert vectors.refresh() is False
    assert vectors._THREAD is None


def test_a_request_during_a_build_runs_one_more_after_it(engine, monkeypatch):
    runs, gate = [], threading.Event()

    def build():
        runs.append(1)
        gate.wait(5)

    monkeypatch.setattr(vectors, "_build_once", build)
    vectors.refresh()
    vectors.refresh()
    vectors.refresh()
    gate.set()
    _wait()

    assert len(runs) == 2, "three requests: the build running and one after it"


def test_it_waits_for_an_embedder_still_loading_and_gives_up_on_one_that_never_answers(engine, monkeypatch):
    answers = iter([False, False, True])
    monkeypatch.setattr("silica.onboarding.serve.ready", lambda base: next(answers))
    monkeypatch.setattr(vectors, "_POLL_S", 0.0)
    vectors.refresh()
    _wait()
    assert vectors.status()["state"] == "idle"

    monkeypatch.setattr("silica.onboarding.serve.ready", lambda base: False)
    monkeypatch.setattr(vectors, "_WAIT_S", 0.0)
    vectors.refresh()
    _wait()
    assert vectors.status() == {"state": "failed", "vault": CONFIG.vault_path, "error": "the embedder did not answer"}


def test_a_vault_switch_stops_the_build_and_builds_the_new_vault(engine, tmp_path, monkeypatch):
    from silica_core import embeddings
    from silica_core.kernel.recall import paths

    other = tmp_path / "other"
    other.mkdir()
    (other / "tobit.md").write_text("# Tobit\n\n## Fish\n\nThe fish was caught in the Tigris.\n", encoding="utf-8")
    (engine / "jubilees.md").write_text("# Jubilees\n\n## Weeks\n\nThe weeks of years are counted.\n", encoding="utf-8")
    first = []

    def switching(texts):
        if not first:
            first.append(texts)
            CONFIG.vault_path = str(other)
        return _fake_embed(texts)

    monkeypatch.setattr(embeddings, "_BATCH", 1)
    monkeypatch.setattr(embeddings, "embed_texts", switching)
    vectors.refresh()
    _wait()

    assert vectors.status()["vault"] == str(other) and vectors.status()["state"] == "idle"
    assert list(embeddings.load()[0]["docs"]) == ["tobit.md"]
    with paths.pinned(str(engine)):
        assert set(embeddings.load()[0]["docs"]) <= {"enoch.md", "jubilees.md"}, "the old vault kept its own"


def test_a_written_note_and_a_finished_conversion_each_ask_for_a_build(tmp_vault, monkeypatch):
    from silica.sources import pdf_extract
    from silica.ui.web import jobs, note_ops
    from tests.doc_factory import pdf_bytes

    asked: list[int] = []
    monkeypatch.setattr(vectors, "refresh", lambda: asked.append(1) or False)
    note_ops.index_now("gone")
    assert asked == [1]

    monkeypatch.setattr(CONFIG, "pdf_provider", "pdfium")
    pdf_extract._CACHE.clear()
    (Path(CONFIG.vault_path) / "book.pdf").write_bytes(pdf_bytes(["The Book of Enoch, the watchers"]))
    asked.clear()
    jobs.record("book.pdf", origin="ocr")
    assert jobs.drain()
    assert asked, "the conversion's note asked for its pages"


def test_a_refresh_that_cannot_start_never_fails_its_caller(engine, monkeypatch):
    def broken():
        raise RuntimeError("silica-core no longer reads PDFs through _pdf_text")

    monkeypatch.setattr(vectors, "_eligible", broken)
    assert vectors.refresh() is False


def test_the_status_rides_the_bus_once_per_state_and_at_most_twice_a_second_within_one(monkeypatch):
    from silica.obs.bus import BUS

    seen: list[dict] = []
    BUS.subscribe(vectors.TOPIC, seen.append)
    monkeypatch.setattr(vectors, "_STATUS", {"state": "idle"})
    monkeypatch.setattr(vectors, "_PUBLISHED", 0.0)
    try:
        vectors._set(state="index", vault="v", done=0, total=3)
        vectors._set(state="index", vault="v", done=1, total=3)  # within half a second: kept, not sent
        vectors._set(state="vectors", vault="v", done=0, total=3)
    finally:
        BUS.unsubscribe(vectors.TOPIC, seen.append)

    assert [(s["state"], s["done"]) for s in seen] == [("index", 0), ("vectors", 0)]
    assert vectors.status()["state"] == "vectors"


def test_the_rail_reads_the_status_and_asks_again_on_the_laptop_only(tmp_vault, monkeypatch):
    from fastapi.testclient import TestClient

    from silica.ui.web import server

    monkeypatch.setattr(vectors, "_STATUS", {"state": "failed", "vault": "v", "error": "the embedder did not answer"})
    client = TestClient(server.app)

    assert client.get("/vectors").json()["error"] == "the embedder did not answer"
    started: list[int] = []
    monkeypatch.setattr(vectors, "refresh", lambda: started.append(1) or True)
    assert client.post("/vectors/refresh").json() == {"started": True} and started == [1]
