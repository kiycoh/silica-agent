"""A conversion leaves the request (ADR-0099): the record on disk, the one
worker, the restart, retry and dismiss, the drop's new contract, the vault
import, commands typed in the chat, and what the release drain reads."""
from __future__ import annotations

import json
import threading
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from silica.sources import convert as conv
from silica.sources import media_admission as adm


def _wav(path: Path, seconds: float) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * int(16000 * seconds))
    return path


@pytest.fixture
def lane(tmp_vault, monkeypatch):
    """ffmpeg and the transcriber stood in for. `gate` holds the transcriber
    until a test opens it, so the state between record and run can be read."""
    for key in (adm.MAX_MINUTES_ENV, adm.MONTHLY_MINUTES_ENV, "SILICA_HOSTED"):
        monkeypatch.delenv(key, raising=False)
    gate = threading.Event()
    gate.set()
    inside: list[str] = []
    calls = {"max": 0, "now": 0}
    lock = threading.Lock()

    def asr(audio):
        with lock:
            calls["now"] += 1
            calls["max"] = max(calls["max"], calls["now"])
        inside.append(audio.name)
        try:
            assert gate.wait(10), "the test never opened the gate"
            return "Some words were spoken in this lecture."
        finally:
            with lock:
                calls["now"] -= 1

    monkeypatch.setattr(conv, "_media_to_audio", lambda src, wd, mp3=False: _wav(Path(wd) / f"{src.stem}.wav", 5))
    monkeypatch.setitem(conv.ASR_PROVIDERS, "endpoint", asr)
    monkeypatch.setattr(adm, "probe_seconds", lambda src: 5.0)
    return gate, inside, calls


@pytest.fixture
def client(lane, monkeypatch):
    from silica.ui.web import server

    server._reset_session()
    ran: dict = {}

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        ran["text"] = [m for m in messages if m["role"] == "user"][-1]["content"]
        messages.append({"role": "assistant", "content": "ok"})
        return "ok"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)
    monkeypatch.setattr(server, "_embed_imported", lambda folders: None)
    return TestClient(server.app), server, ran


def _jobs_dir() -> Path:
    from silica.ui.web import jobs

    return jobs.jobs_dir()


def _record(job_id: str) -> dict:
    return json.loads((_jobs_dir() / f"{job_id}.json").read_text(encoding="utf-8"))


def _drain():
    from silica.ui.web import jobs

    assert jobs.drain(20), "the worker never finished"


def _inbox() -> Path:
    from silica.config import CONFIG

    return Path(CONFIG.vault_path) / "Inbox"


def _chat(server) -> str:
    """The open chat's folder of attached files, vault-relative (ADR-0121 point 5)."""
    from silica.kernel.recall.paths import chat_folder

    return chat_folder(server.current_session_id)


# --- point 1 and 3: the drop answers at once, behind a record ---------------------------

def test_a_dropped_recording_answers_at_once_with_a_queued_job(client, lane):
    from silica.config import CONFIG

    tc, server, ran = client
    gate, _, _ = lane
    gate.clear()
    try:
        r = tc.post("/nucleate", files=[("files", ("lecture.mp3", b"\0" * 64, "audio/mpeg"))])
        assert r.status_code == 202
        (job,) = r.json()["jobs"]
        # Read before the transcriber is let go: the record is on disk first.
        rec = _record(job["id"])
        assert rec["state"] in ("queued", "running")
        # The chat's file, not the vault's (ADR-0121 point 5).
        assert rec["source"]["path"] == f"{_chat(server)}/lecture.mp3" and rec["source"]["size"] == 64
        assert len(rec["source"]["sha256"]) == 64 and rec["media"] is True
        assert "text" not in ran  # no words and nothing ready: no turn
    finally:
        gate.set()
    _drain()
    rec = _record(job["id"])
    assert rec["state"] == "done" and rec["notes"] == [f"{_chat(server)}/lecture.md"]
    assert rec["attempts"] == 1 and rec["finished"] >= rec["started"] >= rec["created"]
    assert (Path(CONFIG.vault_path) / _chat(server) / "lecture.md").exists()
    assert not (_inbox() / "lecture.md").exists() and not (_inbox() / "lecture.mp3").exists()


def test_the_upload_is_whole_before_it_has_its_name(client, monkeypatch):
    """Point 3: `.<name>.part`, fsynced, renamed. Read at the rename: the final
    name never holds a partial file."""
    import os

    tc, server, _ = client
    seen: list[tuple[str, int]] = []
    real = os.replace

    def spy(src, dst):
        seen.append((Path(src).name, Path(src).stat().st_size))
        return real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    body = b"# Hi\n\n" + b"x" * 3000
    tc.post("/nucleate", files=[("files", ("note.md", body, "text/markdown"))])
    assert (".note.md.part", len(body)) in seen
    folder = _inbox().parent / _chat(server)
    assert (folder / "note.md").read_bytes() == body
    assert not (folder / ".note.md.part").exists()


def test_a_drop_with_words_waits_for_its_files_then_asks_once(client, lane):
    """ADR-0121 point 1: a question never runs before its file can be read.
    The drop answers 202 and starts no turn; the record keeps the words, the
    chat and the files that were ready; the page asks once the drop is read,
    and a second ask (another tab) is refused."""
    tc, server, ran = client
    gate, _, _ = lane
    gate.clear()
    try:
        r = tc.post("/nucleate", data={"text": "what is this lecture about?"},
                    files=[("files", ("lecture.mp3", b"\0" * 64, "audio/mpeg")),
                           ("files", ("note.md", b"# Hi\n\nbody", "text/markdown"))])
        assert r.status_code == 202 and r.json()["waits"] is True
        (job,) = r.json()["jobs"]
        assert "text" not in ran
    finally:
        gate.set()
    _drain()
    rec = _record(job["id"])
    assert rec["text"] == "what is this lecture about?"
    assert rec["session"] and rec["session"] == server.current_session_id == r.json()["session"]
    assert rec["beside"] == [f"{_chat(server)}/note.md"]
    assert tc.post(f"/jobs/{job['id']}/turn", json={"as": "ask"}).status_code == 200
    assert ran["text"].startswith("what is this lecture about?")
    # Both files ride the question, each whole while it fits (point 2).
    assert f'<file path="{_chat(server)}/lecture.md">' in ran["text"]
    assert f'<file path="{_chat(server)}/note.md">' in ran["text"]
    assert tc.post(f"/jobs/{job['id']}/turn", json={"as": "ask"}).status_code == 409


def test_an_over_long_recording_never_enters_the_queue(client, monkeypatch):
    """ADR-0099 Consequences: admission runs when the job is recorded."""
    tc, _, _ = client
    monkeypatch.setenv(adm.MAX_MINUTES_ENV, "90")
    monkeypatch.setattr(adm, "probe_seconds", lambda src: 3 * 3600.0)

    r = tc.post("/nucleate", files=[("files", ("long.mp3", b"\0" * 64, "audio/mpeg")),
                                    ("files", ("note.md", b"# Hi\n\nbody", "text/markdown"))])
    assert r.status_code == 200
    assert "X-Silica-Jobs" not in r.headers
    assert not _jobs_dir().exists() or not list(_jobs_dir().glob("*.json"))


def test_a_drop_of_markdown_alone_records_no_job(client):
    tc, server, ran = client
    r = tc.post("/nucleate", files=[("files", ("note.md", b"# Hi\n\nbody", "text/markdown"))])
    assert r.status_code == 200 and "X-Silica-Jobs" not in r.headers
    assert f"{_chat(server)}/note.md" in ran["text"]


# --- point 2: one worker, in order -----------------------------------------------------------

def test_one_worker_runs_the_jobs_one_at_a_time_in_order(client, lane):
    tc, _, _ = client
    gate, inside, calls = lane
    gate.clear()
    try:
        ids = []
        for name in ("one.mp3", "two.mp3", "three.mp3"):
            r = tc.post("/nucleate", files=[("files", (name, name.encode() * 8, "audio/mpeg"))])
            ids.append(r.json()["jobs"][0]["id"])
        deadline = time.monotonic() + 10
        while not inside and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [_record(i)["state"] for i in ids] == ["running", "queued", "queued"]
    finally:
        gate.set()
    _drain()
    assert inside == ["one.wav", "two.wav", "three.wav"]
    assert calls["max"] == 1
    recs = [_record(i) for i in ids]
    assert recs[1]["started"] >= recs[0]["finished"] and recs[2]["started"] >= recs[1]["finished"]


def test_convert_itself_holds_no_lock_on_a_hosted_tenant(tmp_vault, lane, monkeypatch):
    """The hosted one-at-a-time lock is gone (ADR-0099 Consequences): the
    worker orders the web's conversions, and a lock under it would refuse a
    file the worker had queued while a typed command converted another."""
    monkeypatch.setenv("SILICA_HOSTED", "1")
    gate, _, calls = lane
    gate.clear()
    tmp_vault.note("first.mp3", "x")
    tmp_vault.note("second.mp3", "x")
    threads = [threading.Thread(target=conv.convert, args=(n,)) for n in ("first.mp3", "second.mp3")]
    for t in threads:
        t.start()
    try:
        deadline = time.monotonic() + 10
        while calls["now"] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls["max"] == 2
    finally:
        gate.set()
        for t in threads:
            t.join(10)


# --- point 4: a restart is visible, nothing resumes ---------------------------------------

def _write(rec: dict) -> None:
    d = _jobs_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rec['id']}.json").write_text(json.dumps(rec), encoding="utf-8")


def _rec(job_id: str, state: str, **kw) -> dict:
    from silica.config import CONFIG

    now = time.time()
    rec = {"id": job_id, "kind": "convert", "origin": "drop",
           "source": {"path": "Inbox/a.mp3", "name": "a.mp3", "size": 1, "sha256": "0" * 64},
           "dest_dir": "", "vault": CONFIG.vault_path, "text": "", "session": None,
           "media": True, "seconds": None, "state": state, "created": now, "updated": now,
           "started": None, "finished": None, "attempts": 1, "notes": [], "hidden": [],
           "error": "", "dismissed": False}
    rec.update(kw)
    return rec


def test_a_start_marks_the_jobs_it_did_not_run_as_interrupted(client):
    from silica.ui.web import jobs

    old = time.time() - 31 * 86400
    _write(_rec("a" * 12, "running"))
    _write(_rec("b" * 12, "queued"))
    _write(_rec("c" * 12, "done", finished=old))
    _write(_rec("d" * 12, "done", finished=time.time() - 86400))
    _write(_rec("e" * 12, "failed", finished=old, error="it could not be converted"))

    with TestClient(client[1].app):  # the lifespan runs before the first request
        pass

    assert _record("a" * 12)["state"] == "interrupted"
    assert _record("a" * 12)["error"] == jobs.INTERRUPTED == "the server restarted during this conversion"
    assert _record("b" * 12)["state"] == "interrupted"
    assert not (_jobs_dir() / f"{'c' * 12}.json").exists()   # done, past 30 days
    assert _record("d" * 12)["state"] == "done"              # done, inside 30 days
    assert _record("e" * 12)["state"] == "failed"            # kept until dismissed
    assert jobs.recover() == 0  # nothing resumes, and a second look changes nothing


def test_a_running_job_of_this_process_survives_a_second_startup(client, lane):
    from silica.ui.web import jobs

    tc, _, _ = client
    gate, inside, _ = lane
    gate.clear()
    try:
        job = tc.post("/nucleate", files=[("files", ("a.mp3", b"\0" * 9, "audio/mpeg"))]).json()["jobs"][0]
        deadline = time.monotonic() + 10
        while not inside and time.monotonic() < deadline:
            time.sleep(0.01)
        assert jobs.recover() == 0
        assert _record(job["id"])["state"] == "running"
    finally:
        gate.set()
    _drain()
    assert _record(job["id"])["state"] == "done"


def test_retry_reruns_from_the_inbox_copy(client):
    tc, _, _ = client
    (_inbox()).mkdir(parents=True, exist_ok=True)
    (_inbox() / "a.mp3").write_bytes(b"\0" * 32)
    import hashlib

    _write(_rec("a" * 12, "interrupted", error="the server restarted during this conversion",
                source={"path": "Inbox/a.mp3", "name": "a.mp3", "size": 32,
                        "sha256": hashlib.sha256(b"\0" * 32).hexdigest()}))
    listed = tc.get("/jobs").json()["jobs"]
    assert listed[0]["retry"] is True and "vault" not in listed[0]

    r = tc.post(f"/jobs/{'a' * 12}/retry")
    assert r.status_code == 200 and r.json()["state"] == "queued"
    _drain()
    rec = _record("a" * 12)
    assert rec["state"] == "done" and rec["notes"] == ["Inbox/a.md"] and rec["attempts"] == 2


def test_retry_refuses_a_file_that_is_gone_or_changed(client):
    tc, _, _ = client
    _inbox().mkdir(parents=True, exist_ok=True)
    _write(_rec("a" * 12, "failed"))
    r = tc.post(f"/jobs/{'a' * 12}/retry")
    assert r.status_code == 409 and "no longer in the Inbox" in r.json()["detail"]
    assert tc.get("/jobs").json()["jobs"][0]["retry"] is False

    (_inbox() / "a.mp3").write_bytes(b"other bytes")
    r = tc.post(f"/jobs/{'a' * 12}/retry")
    assert r.status_code == 409 and "changed since it was added" in r.json()["detail"]
    assert _record("a" * 12)["state"] == "failed"


def test_dismiss_removes_a_stopped_job_hides_a_done_one_and_refuses_a_live_one(client):
    tc, _, _ = client
    _write(_rec("a" * 12, "failed"))
    _write(_rec("b" * 12, "done", notes=["Inbox/b.md"]))
    _write(_rec("c" * 12, "running"))
    assert tc.post(f"/jobs/{'a' * 12}/dismiss").status_code == 200
    assert not (_jobs_dir() / f"{'a' * 12}.json").exists()
    assert tc.post(f"/jobs/{'b' * 12}/dismiss").status_code == 200
    assert _record("b" * 12)["dismissed"] is True
    r = tc.post(f"/jobs/{'c' * 12}/dismiss")
    assert r.status_code == 409 and "still converting" in r.json()["detail"]
    assert [j["id"] for j in tc.get("/jobs").json()["jobs"]] == ["c" * 12]


def test_an_id_that_is_not_one_reaches_no_file(client):
    tc, _, _ = client
    assert tc.post("/jobs/..%2F..%2Fx/retry").status_code in (404, 409)
    assert tc.post("/jobs/ZZZZZZZZZZZZ/dismiss").status_code == 409


# --- point 5: transitions on the stream the browser holds --------------------------------

def test_a_transition_reaches_the_narration_stream_as_a_job_event(client, monkeypatch):
    from types import SimpleNamespace

    from silica.agent.bus import BUS
    from silica.ui.web import jobs

    tc, server, _ = client
    stop = SimpleNamespace(should_exit=False)
    monkeypatch.setattr(server, "_SERVER", stop)
    monkeypatch.setattr(server, "_SSE_POLL_S", 0.01)
    body: list[str] = []
    t = threading.Thread(target=lambda: body.append(tc.get("/narration/sse").text), daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not BUS._subscribers.get(jobs.TOPIC) and time.monotonic() < deadline:
        time.sleep(0.01)
    jobs._publish(_rec("a" * 12, "running"))
    time.sleep(0.2)
    stop.should_exit = True
    t.join(10)
    assert not t.is_alive()
    frame = next(f for f in body[0].split("\n\n") if f.startswith("event: job"))
    assert "id:" not in frame  # the beats' cursor is untouched
    data = json.loads(frame.split("data: ", 1)[1])
    assert data["id"] == "a" * 12 and data["state"] == "running" and "vault" not in data
    # The stream's record carries what the list's does: a failed job's Retry
    # was missing from the row the event redrew (seen in the browser 2026-10-13).
    assert "retry" in data


# --- point 6: a finished job offers the turn -----------------------------------------------

def test_a_finished_job_offers_the_turn_and_never_starts_one(client):
    tc, server, ran = client
    r = tc.post("/nucleate", files=[("files", ("talk.mp3", b"\0" * 64, "audio/mpeg"))])
    job = r.json()["jobs"][0]
    _drain()
    assert "text" not in ran  # done, and still no turn of its own
    r = tc.post(f"/jobs/{job['id']}/turn", json={})
    assert r.status_code == 200
    # The button is the student's choice, so it nucleates; a wordless drop no
    # longer does. Nucleating adds the file to the vault first (ADR-0121 point 5).
    assert ran["text"].startswith(server.NUCLEATE_ASK) and "Inbox/talk.md" in ran["text"]
    assert (_inbox() / "talk.md").exists() and (_inbox() / "talk.mp3").exists()
    assert _record(job["id"])["dismissed"] is True
    # A drop's turn is asked once (ADR-0121 point 1): two tabs, one answer.
    assert tc.post(f"/jobs/{job['id']}/turn", json={}).status_code == 409


def test_ask_again_reruns_the_words_typed_with_the_drop(client):
    tc, server, ran = client
    r = tc.post("/nucleate", data={"text": "summarize it"},
                files=[("files", ("talk.mp3", b"\0" * 64, "audio/mpeg"))])
    job_id = r.json()["jobs"][0]["id"]
    _drain()
    tc.post(f"/jobs/{job_id}/turn", json={"ask": True})   # the older spelling of "as": "ask"
    assert ran["text"].startswith("summarize it") and f"{_chat(server)}/talk.md" in ran["text"]


# --- the vault import (ADR-0098) and commands typed in the chat ------------------------------

def test_an_imported_pdf_converts_as_a_job_and_the_server_drops_it_after(client, monkeypatch):
    from silica.config import CONFIG
    from tests.doc_factory import pdf_bytes

    monkeypatch.setattr(CONFIG, "pdf_provider", "pdfium")
    monkeypatch.setenv("SILICA_HOSTED", "1")
    tc, _, _ = client
    r = tc.post("/vault/import", files=[("files", ("paper.pdf", pdf_bytes(["The full text of the paper, long enough to be a page."]), "application/pdf"))],
                data={"paths": [""], "last": "1"})
    (row,) = r.json()["files"]
    assert row["status"] == "queued" and row["path"] == "Inbox/paper.pdf"
    _drain()
    rec = _record(row["job"])
    assert rec["state"] == "done" and rec["notes"] == ["Inbox/paper.md"]
    assert not (_inbox() / "paper.pdf").exists() and (_inbox() / "paper.md").exists()


def test_convert_typed_in_the_chat_is_a_job(client):
    tc, _, ran = client
    _inbox().mkdir(parents=True, exist_ok=True)
    (_inbox() / "talk.mp3").write_bytes(b"\0" * 16)
    r = tc.post("/chat", json={"text": "/convert Inbox/talk.mp3 Inbox/missing.pdf notes.xyz"})
    assert r.status_code == 202
    body = r.json()
    assert [j["source"]["name"] for j in body["jobs"]] == ["talk.mp3"]
    assert {s["name"]: s["reason"] for s in body["skipped"]} == {
        "missing.pdf": "it was not found", "notes.xyz": "Silica has no converter for .xyz"}
    assert "text" not in ran
    _drain()
    assert _record(body["jobs"][0]["id"])["state"] == "done"


def test_nucleate_typed_with_a_file_to_convert_records_it_and_waits(client):
    from silica.ui.web import jobs

    tc, _, ran = client
    _inbox().mkdir(parents=True, exist_ok=True)
    (_inbox() / "talk.mp3").write_bytes(b"\0" * 16)
    r = tc.post("/chat", json={"text": "/nucleate Inbox/talk.mp3 --target=Lectures"})
    assert r.status_code == 202 and r.json()["command"] == "/nucleate"
    assert "text" not in ran  # the command did not run: the job offers it
    _drain()
    assert _record(r.json()["jobs"][0]["id"])["dest_dir"] == "Lectures"
    # Converted once: typed again, the command finds the conversion and runs
    # as it always did, with nothing left to queue.
    assert jobs.from_command("/nucleate Inbox/talk.mp3", None) is None
    assert jobs.from_command("/nucleate Inbox/", None) is None  # a folder runs as before


def test_a_command_with_nothing_to_convert_runs_as_before(client):
    tc, _, _ = client
    r = tc.post("/chat", json={"text": "hello"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")


# --- point 8 and the edge cap -----------------------------------------------------------

def test_the_release_drain_reads_jobs_and_the_turn_on_the_socket(client):
    """Host: localhost, no Origin, nothing remote: what bin/release sends."""
    from silica.ui.web import server

    tc, _, _ = client
    _write(_rec("a" * 12, "running"))
    _write(_rec("b" * 12, "queued"))
    _write(_rec("c" * 12, "failed"))
    r = tc.get("/jobs/running", headers={"host": "localhost"})
    assert r.status_code == 200
    assert r.json() == {"turn": False, "jobs": [
        {"id": "a" * 12, "state": "running", "kind": "convert"},
        {"id": "b" * 12, "state": "queued", "kind": "convert"}]}
    assert server._begin_turn()
    try:
        assert tc.get("/jobs/running", headers={"host": "localhost"}).json()["turn"] is True
    finally:
        server._end_turn()


def test_on_the_server_the_page_learns_the_edge_cap(client, monkeypatch):
    tc, _, _ = client
    assert tc.get("/supported_types").json()["upload_max_mb"] is None
    monkeypatch.setenv("SILICA_HOSTED", "1")
    assert tc.get("/supported_types").json()["upload_max_mb"] == 100


# --- the page's side (driven in a browser 2026-10-13; these pin the wiring) -----------

def test_the_page_reads_a_reply_that_is_not_a_stream_and_rereads_jobs_on_reconnect():
    from tests.webassets import WEB, app_js

    js = app_js()
    run = js[js.index("async function runTurn("):js.index("function handle(ev)")]
    # point 5: a body that is not an event stream never ends without a word
    assert 'startsWith("text/event-stream")' in run and "drawJobAnswer(flow, d)" in run
    work = (WEB / "work.js").read_text(encoding="utf-8")
    assert 'es.addEventListener("job"' in work and '"silica:stream-open"' in work
    assert 'document.addEventListener("silica:stream-open", jobsFetch)' in js
    # the edge's cap is checked before a byte is sent, on the drop and the import
    assert js.count("withinUploadCap(") >= 3
