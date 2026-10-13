"""Admission for media before the shared transcriber: the length cap, the
monthly minutes, one file at a time per hosted tenant, and the refusal reaching
the turn a chat drop composes."""
from __future__ import annotations

import threading
import wave
from pathlib import Path

import pytest

from silica.sources import convert as conv
from silica.sources import media_admission as adm


def _write_wav(path: Path, seconds: float) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * int(16000 * seconds))
    return path


@pytest.fixture
def media(tmp_vault, monkeypatch):
    """A media lane with ffmpeg, ffprobe and the transcriber stood in for:
    `lengths[name]` is what the decoded wav holds, `probed[name]` what the
    container claims (the same unless a test makes it lie)."""
    for key in (adm.MAX_MINUTES_ENV, adm.MONTHLY_MINUTES_ENV, "SILICA_HOSTED"):
        monkeypatch.delenv(key, raising=False)
    lengths: dict[str, float] = {}
    probed: dict[str, float] = {}
    calls: list[str] = []

    def to_wav(src, workdir):
        return _write_wav(Path(workdir) / "audio.wav", lengths[src.name])

    def asr(wav):
        calls.append(wav.name)
        return "Some words were spoken."

    monkeypatch.setattr(conv, "_media_to_wav", to_wav)
    monkeypatch.setitem(conv.ASR_PROVIDERS, "endpoint", asr)
    monkeypatch.setattr(adm, "probe_seconds", lambda src: probed.get(src.name, lengths[src.name]))

    def add(name: str, seconds: float) -> str:
        tmp_vault.note(name, "x")
        lengths[name] = seconds
        return name

    return add, probed, calls


def test_no_limit_configured_keeps_the_laptop_lane_as_it_was(media, monkeypatch):
    add, _, calls = media
    monkeypatch.setattr(adm, "probe_seconds", lambda src: pytest.fail("probed with no limit set"))
    conv.convert(add("talk.mp3", 3 * 3600))
    assert calls == ["audio.wav"]
    assert not adm._counter_path().exists()


def test_a_file_over_the_cap_is_refused_before_ffmpeg_and_the_transcriber(media, monkeypatch):
    add, _, calls = media
    monkeypatch.setenv(adm.MAX_MINUTES_ENV, "90")
    monkeypatch.setattr(conv, "_media_to_wav", lambda *a: pytest.fail("decoded a refused file"))

    with pytest.raises(adm.MediaRefused) as refused:
        conv.convert(add("lecture.mp3", 91 * 60))
    assert str(refused.value).startswith("lecture.mp3 is 91 minutes long")
    assert "up to 90 minutes" in str(refused.value)
    assert calls == []


def test_a_header_that_lies_about_the_length_is_caught_on_the_decoded_audio(media, monkeypatch):
    add, probed, calls = media
    monkeypatch.setenv(adm.MAX_MINUTES_ENV, "1")
    probed["liar.mp4"] = 30.0

    with pytest.raises(adm.MediaRefused, match="liar.mp4 is 2 minutes long"):
        conv.convert(add("liar.mp4", 120))
    assert calls == []


def test_the_month_counts_what_was_transcribed_and_names_what_is_left(media, monkeypatch):
    add, _, calls = media
    monkeypatch.setenv(adm.MONTHLY_MINUTES_ENV, "10")
    monkeypatch.setattr(adm, "_month", lambda: "2026-10")

    conv.convert(add("one.mp3", 8 * 60))
    assert adm.used_seconds() == pytest.approx(480)

    with pytest.raises(adm.MediaRefused) as refused:
        conv.convert(add("two.mp3", 3 * 60))
    assert "2 of this month's 10 minutes of transcription are left" in str(refused.value)
    assert "1 November" in str(refused.value)
    assert adm.used_seconds() == pytest.approx(480)  # a refusal costs nothing

    conv.convert(add("three.mp3", 2 * 60))
    assert adm.used_seconds() == pytest.approx(600)
    assert len(calls) == 2

    monkeypatch.setattr(adm, "_month", lambda: "2026-11")
    assert adm.used_seconds() == 0.0  # a new month starts from zero
    conv.convert(add("four.mp3", 5 * 60))
    assert adm._ledger() == {"2026-10": pytest.approx(600), "2026-11": pytest.approx(300)}


def test_a_transcription_that_fails_is_not_counted(media, monkeypatch):
    add, _, _ = media
    monkeypatch.setenv(adm.MONTHLY_MINUTES_ENV, "60")

    def down(wav):
        raise ValueError("no transcription server at http://127.0.0.1:1236/v1")

    monkeypatch.setitem(conv.ASR_PROVIDERS, "endpoint", down)
    with pytest.raises(ValueError, match="no transcription server"):
        conv.convert(add("talk.mp3", 600))
    assert adm.used_seconds() == 0.0


def test_a_hosted_tenant_transcribes_one_file_at_a_time(media, monkeypatch):
    """The turn gate is freed when a browser leaves mid-/convert while the
    conversion thread runs on, so the gate alone let a second file in."""
    add, _, _ = media
    monkeypatch.setenv("SILICA_HOSTED", "1")
    inside, release = threading.Event(), threading.Event()

    def slow(wav):
        inside.set()
        release.wait(10)
        return "Some words were spoken."

    monkeypatch.setitem(conv.ASR_PROVIDERS, "endpoint", slow)
    first = threading.Thread(target=conv.convert, args=(add("first.mp3", 60),))
    first.start()
    try:
        assert inside.wait(10)
        with pytest.raises(adm.MediaRefused, match="Another file is still being transcribed"):
            conv.convert(add("second.mp3", 60))
    finally:
        release.set()
        first.join(10)
    conv.convert("second.mp3")  # admitted once the first is done


def test_a_laptop_is_not_held_to_one_file_at_a_time(media, monkeypatch):
    add, _, _ = media
    inside, release = threading.Event(), threading.Event()

    def slow(wav):
        inside.set()
        release.wait(10)
        return "Some words were spoken."

    monkeypatch.setitem(conv.ASR_PROVIDERS, "endpoint", slow)
    first = threading.Thread(target=conv.convert, args=(add("first.mp3", 60),))
    first.start()
    try:
        assert inside.wait(10)
        monkeypatch.setitem(conv.ASR_PROVIDERS, "endpoint", lambda wav: "Other words.")
        assert conv.convert(add("second.mp3", 60))
    finally:
        release.set()
        first.join(10)


def test_a_typo_in_a_limit_is_logged_and_turns_that_limit_off(monkeypatch, caplog):
    monkeypatch.setenv(adm.MAX_MINUTES_ENV, "ninety")
    assert adm._minutes(adm.MAX_MINUTES_ENV) == 0.0
    assert "SILICA_MEDIA_MAX_MINUTES" in caplog.text


def test_ffprobe_reads_a_real_length(tmp_path):
    if not conv.shutil.which("ffprobe"):
        pytest.skip("ffprobe not installed")
    assert adm.probe_seconds(_write_wav(tmp_path / "a.wav", 3.0)) == pytest.approx(3.0, abs=0.05)
    (tmp_path / "junk.mp3").write_bytes(b"not audio")
    assert adm.probe_seconds(tmp_path / "junk.mp3") is None


# --- the refusal reaches the user ----------------------------------------------------------

@pytest.fixture
def drop(media, monkeypatch):
    from fastapi.testclient import TestClient

    from silica.ui.web import server

    server._reset_session()
    ran: dict = {}

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        ran["text"] = next(m["content"] for m in messages if m["role"] == "user")
        messages.append({"role": "assistant", "content": "ok"})
        return "ok"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)
    tc = TestClient(server.app)
    lengths = {}
    monkeypatch.setattr(conv, "_media_to_wav", lambda src, wd: _write_wav(Path(wd) / "a.wav", lengths[src.name]))
    monkeypatch.setattr(adm, "probe_seconds", lambda src: lengths[src.name])

    def post(name: str, seconds: float):
        lengths[name] = seconds
        r = tc.post("/nucleate", files=[("files", (name, b"\0" * 64, "audio/mpeg")),
                                        ("files", ("note.md", b"# Hi\n\nbody", "text/markdown"))])
        assert r.status_code == 200
        return ran["text"]

    return post


def test_a_refused_file_is_named_in_the_turn_with_its_sentence(drop, monkeypatch):
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv(adm.MAX_MINUTES_ENV, "90")

    text = drop("lecture.mp3", 3 * 3600)
    assert "Inbox/note.md" in text  # the other file still went through
    assert "Not staged" in text
    assert "- lecture.mp3: lecture.mp3 is 180 minutes long, and files of up to 90 minutes" in text


def test_hosted_a_converter_failure_reaches_the_turn_without_its_raw_text(drop, monkeypatch):
    monkeypatch.setenv("SILICA_HOSTED", "1")

    def down(wav):
        raise ValueError("no transcription server at http://127.0.0.1:18236/v1/audio/transcriptions")

    monkeypatch.setitem(conv.ASR_PROVIDERS, "endpoint", down)
    text = drop("talk.mp3", 60)
    assert "- talk.mp3: it could not be converted" in text
    assert "127.0.0.1" not in text


def test_on_a_laptop_the_turn_carries_the_converter_reason(drop, monkeypatch):
    def down(wav):
        raise ValueError("no transcription server at http://localhost:1236/v1")

    monkeypatch.setitem(conv.ASR_PROVIDERS, "endpoint", down)
    assert "- talk.mp3: no transcription server at http://localhost:1236/v1" in drop("talk.mp3", 60)
