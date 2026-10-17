"""ADR-0121: a file attached to a chat belongs to that chat. The question waits
for it, its text rides the turn, a turn that brings it in is not offered the
injector, it joins the next message once read, it reaches the vault only when
asked, and the last turn can be asked again. Stop leaves within a second."""
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
def chat(tmp_vault, monkeypatch):
    """The GUI with the transcriber stood in for and the agent recorded: each
    turn keeps its user message and the tools it was offered."""
    from silica.ui.web import server

    for key in (adm.MAX_MINUTES_ENV, adm.MONTHLY_MINUTES_ENV, "SILICA_HOSTED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(conv, "_media_to_audio", lambda src, wd, mp3=False: _wav(Path(wd) / f"{src.stem}.wav", 5))
    monkeypatch.setitem(conv.ASR_PROVIDERS, "endpoint", lambda audio: "The lecture said the sky is blue.")
    monkeypatch.setattr(adm, "probe_seconds", lambda src: 5.0)
    server._reset_session()
    turns: list[dict] = []

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, constraints=None, **kw):
        user = [m for m in messages if m["role"] == "user"][-1]
        turns.append({"msg": dict(user), "tools": tuple(constraints.tools) if constraints else ()})
        messages.append({"role": "assistant", "content": f"answer {len(turns)}"})
        return f"answer {len(turns)}"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)
    return TestClient(server.app), server, turns


def _drain():
    from silica.ui.web import jobs

    assert jobs.drain(20), "the worker never finished"


def _folder(server) -> str:
    from silica.kernel.recall.paths import chat_folder

    return chat_folder(server.current_session_id)


def _vault() -> Path:
    from silica.config import CONFIG

    return Path(CONFIG.vault_path)


# --- point 4: a file read after its drop joins the next message ----------------------------

def test_a_file_read_after_a_wordless_drop_joins_the_next_message_once(chat):
    tc, server, turns = chat
    r = tc.post("/nucleate", files=[("files", ("talk.mp3", b"\0" * 64, "audio/mpeg"))])
    assert r.status_code == 202 and r.json()["waits"] is False and not turns
    _drain()
    tc.post("/chat", json={"text": "di cosa parla questo file?"})
    msg = turns[-1]["msg"]
    assert msg["content"].startswith("di cosa parla questo file?")
    assert "the sky is blue" in msg["content"]                    # its text rides the turn
    assert msg["silica_said"] == "di cosa parla questo file?"
    assert [f["name"] for f in msg["silica_files"]] == ["talk.mp3"]
    assert "silica_run_injector" not in turns[-1]["tools"]        # point 3
    tc.post("/chat", json={"text": "e poi?"})
    assert turns[-1]["msg"]["content"] == "e poi?"                 # joined once
    assert "silica_run_injector" in turns[-1]["tools"]            # offered again


def test_the_words_of_a_waiting_drop_ride_the_message_that_took_its_file(chat):
    """A message typed in the moment between the file being read and the page
    asking: the drop's question is not lost, it is named in that turn."""
    tc, server, turns = chat
    r = tc.post("/nucleate", data={"text": "riassumi"}, files=[("files", ("talk.mp3", b"\0" * 64, "audio/mpeg"))])
    _drain()
    tc.post("/chat", json={"text": "allora?"})
    assert 'With these files the user asked earlier: "riassumi"' in turns[-1]["msg"]["content"]
    assert tc.post(f"/jobs/{r.json()['jobs'][0]['id']}/turn", json={"as": "ask"}).status_code == 409


# --- point 2: what fits rides whole, what does not is named --------------------------------

def test_a_file_too_long_to_include_is_named_with_its_outline_and_read_first(tmp_vault, monkeypatch):
    from silica.ui.web import server

    tmp_vault.note("Inbox/long.md", "# Uno\n\n" + "parola " * 5000 + "\n\n## Due\n\nfine\n")
    tmp_vault.note("Inbox/short.md", "# Breve\n\nun verbale corto\n")
    msg = server._compose_nucleate_turn("what does it say?", ["Inbox/short.md", "Inbox/long.md"], [])
    assert '<file path="Inbox/short.md">' in msg and "un verbale corto" in msg
    assert "- Inbox/long.md (~" in msg and "sections: Uno; Due" in msg
    assert 'read it with silica_read(path="Inbox/long.md") before answering' in msg
    assert "parola parola" not in msg
    assert "ready to nucleate" not in msg


# --- point 6: the turns a read file offers ------------------------------------------------

def test_summarize_and_quiz_compose_their_request_and_withhold_the_injector(chat):
    tc, server, turns = chat
    for kind, lead in (("summarize", server.SUMMARIZE_ASK), ("quiz", server.QUIZ_ASK)):
        server._reset_session()
        r = tc.post("/nucleate", files=[("files", (f"{kind}.mp3", kind.encode() * 9, "audio/mpeg"))])
        _drain()
        assert tc.post(f"/jobs/{r.json()['jobs'][0]['id']}/turn", json={"as": kind}).status_code == 200
        assert turns[-1]["msg"]["content"].startswith(lead)
        assert "silica_run_injector" not in turns[-1]["tools"]
    assert tc.post(f"/jobs/{r.json()['jobs'][0]['id']}/turn", json={"as": "dance"}).status_code == 400


def test_nucleate_it_is_the_composed_turn_that_keeps_the_injector(chat):
    tc, server, turns = chat
    r = tc.post("/nucleate", files=[("files", ("talk.mp3", b"\0" * 64, "audio/mpeg"))])
    _drain()
    tc.post(f"/jobs/{r.json()['jobs'][0]['id']}/turn", json={"as": "nucleate"})
    assert "silica_run_injector" in turns[-1]["tools"]
    assert "Inbox/talk.md" in turns[-1]["msg"]["content"]


# --- point 5: attaching is not importing --------------------------------------------------

def test_a_chats_files_stay_out_of_the_vault_until_added(chat):
    from silica.driver import DRIVER

    tc, server, turns = chat
    tc.post("/nucleate", data={"text": "what is it?"}, files=[("files", ("talk.mp3", b"\0" * 64, "audio/mpeg"))])
    _drain()
    folder = _folder(server)
    assert (_vault() / folder / "talk.md").exists()
    listed = [r.path for r in DRIVER.list_files("") if "talk" in r.path]
    assert listed == []   # the converted note is not listed, though DRIVER.upsert wrote it
    r = tc.post("/chat/files/add", json={"source": f"{folder}/talk.mp3"})
    assert r.status_code == 200 and r.json()["notes"] == ["Inbox/talk.md"]
    assert (_vault() / "Inbox/talk.mp3").exists()
    assert "Inbox/talk.md" in [r.path for r in DRIVER.list_files("")]
    # Another chat's file, or a path out of the chat, is refused.
    assert tc.post("/chat/files/add", json={"source": "Inbox/talk.mp3"}).status_code == 400
    assert tc.post("/chat/files/add", json={"source": ".silica-chat/someoneelse/x.md"}).status_code == 400


def test_add_to_vault_refuses_to_overwrite_another_file_of_the_same_name(chat):
    tc, server, _ = chat
    tc.post("/nucleate", data={"text": "x"}, files=[("files", ("talk.mp3", b"\0" * 64, "audio/mpeg"))])
    _drain()
    (_vault() / "Inbox").mkdir(exist_ok=True)
    (_vault() / "Inbox/talk.mp3").write_bytes(b"other bytes")
    r = tc.post("/chat/files/add", json={"source": f"{_folder(server)}/talk.mp3"})
    assert r.status_code == 409 and "different file" in r.json()["detail"]
    assert (_vault() / "Inbox/talk.mp3").read_bytes() == b"other bytes"


def test_deleting_a_chat_deletes_its_files(chat):
    tc, server, _ = chat
    tc.post("/nucleate", data={"text": "leggi"}, files=[("files", ("note.md", b"# A\n\nbody", "text/markdown"))])
    sid, folder = server.current_session_id, _vault() / _folder(server)
    assert (folder / "note.md").exists()
    assert tc.post("/session/delete", json={"id": sid}).json() == {"ok": True}
    assert not folder.exists()


# --- point 4: the bubble shows the words and the files, never the manifest -----------------

def test_messages_serve_the_typed_words_and_the_files(chat):
    tc, server, _ = chat
    tc.post("/nucleate", data={"text": "leggi questo"}, files=[("files", ("note.md", b"# A\n\nbody", "text/markdown"))])
    tc.post("/nucleate", files=[("files", ("bio.md", b"# B\n\nbody", "text/markdown"))])
    users = [m for m in tc.get("/messages").json() if m["role"] == "user"]
    assert users[0]["content"] == "leggi questo"
    assert users[1]["content"] == ""                       # DROP_ASK is the model's, not the student's
    assert [f["name"] for f in users[0]["files"]] == ["note.md"] and users[0]["files"][0]["in_vault"] is False


def test_a_drop_names_its_chat_at_once(chat):
    from silica.agent import narration

    tc, server, _ = chat
    tc.post("/nucleate", data={"text": "riassumi i findings\nper favore"},
            files=[("files", ("talk.mp3", b"\0" * 64, "audio/mpeg"))])
    rows = narration.list_sessions(str(_vault()))
    assert [r["title"] for r in rows if r["id"] == server.current_session_id] == ["riassumi i findings"]


# --- point 7: the last turn asked again -----------------------------------------------------

def test_redo_regenerates_and_edit_replaces_the_words_keeping_the_files(chat):
    from silica.agent import narration

    tc, server, turns = chat
    tc.post("/chat", json={"text": "prima domanda"})
    tc.post("/nucleate", data={"text": "cosa dice?"}, files=[("files", ("note.md", b"# A\n\nil cielo", "text/markdown"))])
    assert tc.post("/turn/redo", json={}).status_code == 200
    assert turns[-1]["msg"]["content"] == turns[-2]["msg"]["content"]
    assert "silica_run_injector" not in turns[-1]["tools"]          # its files still withhold it
    assert tc.post("/turn/redo", json={"text": "e il mare?"}).status_code == 200
    content = turns[-1]["msg"]["content"]
    assert content.startswith("e il mare?") and "il cielo" in content and "cosa dice?" not in content
    users = [m for m in server.messages if m["role"] == "user"]
    assert [u.get("silica_said", u["content"]) for u in users] == ["prima domanda", "e il mare?"]
    # The narration folds the rewinds the same way, so a reload agrees.
    beats = narration.read_beats(narration.narration_dir() / f"{server.current_session_id}.jsonl")
    replayed = [m for m in narration.messages_from_beats(beats) if m["role"] == "user"]
    assert [u.get("silica_said", u["content"]) for u in replayed] == ["prima domanda", "e il mare?"]
    assert tc.post("/turn/redo", json={"text": "  "}).status_code == 400


def test_redo_refuses_a_direct_command(chat):
    tc, server, _ = chat
    server.messages.append({"role": "user", "content": "/vault", "origin": "cli"})
    assert tc.post("/turn/redo", json={}).status_code == 409


# --- point 7: Stop within a second ---------------------------------------------------------

def test_stop_leaves_a_model_call_within_a_second(monkeypatch):
    from silica.agent import loop

    def slow_llm(model, messages, **kw):
        time.sleep(5)
        raise AssertionError("the loop should have left before this answer")

    monkeypatch.setattr(loop, "call_llm", slow_llm)
    token = threading.Event()
    threading.Timer(0.3, token.set).start()
    t0 = time.monotonic()
    out = loop.run_agent([{"role": "user", "content": "hi"}], "m", cancel_token=token)
    assert out == "(silica: cancelled)" and time.monotonic() - t0 < 1.2


def test_stop_runs_no_further_tool_of_the_batch(monkeypatch):
    from types import SimpleNamespace

    import silica.cli  # noqa: F401  (fills the TOOLS registry)
    from silica.agent import loop
    from silica.tools import TOOLS

    token = threading.Event()
    ran: list[str] = []
    calls = [SimpleNamespace(id=f"c{i}", name="silica_exists", args={"path": f"n{i}.md"}) for i in range(3)]

    def one_batch(model, messages, **kw):
        return SimpleNamespace(tool_calls=calls, text="", reasoning="", usage={},
                               assistant_message={"role": "assistant", "content": "", "tool_calls": [
                                   {"id": c.id, "type": "function",
                                    "function": {"name": c.name, "arguments": json.dumps(c.args)}} for c in calls]})

    real = TOOLS["silica_exists"].fn

    def fn(**kw):
        ran.append(kw.get("path"))
        token.set()      # Stop pressed while the first tool ran
        return real(**kw)

    monkeypatch.setattr(loop, "call_llm", one_batch)
    monkeypatch.setattr(TOOLS["silica_exists"], "fn", fn)
    out = loop.run_agent([{"role": "user", "content": "hi"}], "m", cancel_token=token)
    assert out == "(silica: cancelled)" and ran == ["n0.md"]


# --- point 7: sources from search, and the page progress ----------------------------------

def test_search_results_name_their_notes():
    from silica.ui.web.callback import _found

    assert _found("silica_search", json.dumps({"paths": ["a.md", "b.md", "c.md", "d.md"]})) == ["a.md", "b.md", "c.md"]
    assert _found("silica_search_context", json.dumps({"hits": [{"path": "x.md"}, {"path": "x.md"}]})) == ["x.md"]
    assert _found("silica_recall", json.dumps({"notes": ["Folder/Note"]})) == ["Folder/Note.md"]
    assert _found("silica_read", json.dumps({"path": "a.md"})) == []
    assert _found("silica_search", "not json") == []


def test_a_chat_file_converts_beside_itself_and_reports_its_pages(tmp_vault):
    from silica.kernel.recall.paths import chat_folder

    src = Path(tmp_vault.note(f"{chat_folder('abc123')}/paper.pdf", ""))
    assert conv._note_dir(src, "Inbox") == chat_folder("abc123")
    assert conv._note_dir(Path(tmp_vault.note("Inbox/x/paper.pdf", "")), "Inbox") == "Inbox/x"

    seen: list[tuple[int, int]] = []
    token = conv.PAGE_PROGRESS.set(lambda done, total: seen.append((done, total)))
    try:
        data = {"images": {0: b"png0", 1: b"png1", 2: b"png2"}, "pages": [[("t", 0)]], "no_text": [0, 1, 2]}
        import silica.agent.vision as vision
        from silica.kernel.recall import image_readings

        real = image_readings.read_image
        image_readings.read_image = lambda png, lang, prompt=None, ask=None: {"text": "read"}
        vision_ask = vision.ask_chat_model
        vision.ask_chat_model = lambda messages, max_tokens=0: "read"
        try:
            conv._read_pages(data, Path(tmp_vault.note("work/.keep", "")).parent)
        finally:
            image_readings.read_image = real
            vision.ask_chat_model = vision_ask
    finally:
        conv.PAGE_PROGRESS.reset(token)
    assert seen[0] == (0, 3) and seen[-1] == (3, 3)
