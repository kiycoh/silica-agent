"""Admission for media before the shared transcriber: the length cap, the
monthly minutes, and the refusal reaching the turn a chat drop composes. One
file at a time is the job worker's now (ADR-0099), tested in test_jobs.py."""
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

    def to_wav(src, workdir, mp3=False):
        return _write_wav(Path(workdir) / "audio.wav", lengths[src.name])

    def asr(wav):
        calls.append(wav.name)
        return "Some words were spoken."

    monkeypatch.setattr(conv, "_media_to_audio", to_wav)
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
    monkeypatch.setattr(conv, "_media_to_audio", lambda *a, **k: pytest.fail("decoded a refused file"))

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


def test_an_empty_answer_is_retried_once_and_never_counted(media, monkeypatch):
    add, _, _ = media
    monkeypatch.setenv(adm.MONTHLY_MINUTES_ENV, "60")
    answers = ["", "Words on the second call."]
    monkeypatch.setitem(conv.ASR_PROVIDERS, "endpoint", lambda wav: answers.pop(0))
    conv.convert(add("flaky.mp3", 600))
    assert answers == [] and adm.used_seconds() == pytest.approx(600)

    calls = []
    monkeypatch.setitem(conv.ASR_PROVIDERS, "endpoint", lambda wav: calls.append(wav) or "")
    with pytest.raises(ValueError, match="no speech transcribed"):
        conv.convert(add("silence.mp3", 600))
    assert len(calls) == 2 and adm.used_seconds() == pytest.approx(600)


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


def test_a_concat_list_named_mp3_reads_nothing_beside_it(tmp_path):
    """ffmpeg picks a demuxer from the bytes: without convert._FFMPEG_FORMATS
    this upload was read as a list naming another file, and transcribed it."""
    if not conv.shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    _write_wav(tmp_path / "other.wav", 2.0)
    (tmp_path / "evil.mp3").write_text("ffconcat version 1.0\nfile 'other.wav'\n")
    with pytest.raises(ValueError, match="ffmpeg could not read evil.mp3"):
        conv._media_to_audio(tmp_path / "evil.mp3", tmp_path, mp3=True)


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
    monkeypatch.setattr(conv, "_media_to_audio", lambda src, wd, mp3=False: _write_wav(Path(wd) / "a.wav", lengths[src.name]))
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


def test_hosted_a_drop_takes_audio(drop, monkeypatch):
    monkeypatch.setenv("SILICA_HOSTED", "1")
    assert "Still converting" in drop("talk.m4a", 60)


def test_hosted_a_drop_names_what_the_picker_does_not_offer(drop, monkeypatch):
    monkeypatch.setenv("SILICA_HOSTED", "1")
    text = drop("clip.mp4", 60)
    assert "Inbox/note.md" in text
    assert "- clip.mp4: .mp4: not a type Silica reads here" in text


def _failure(name: str) -> str:
    """The sentence a failed conversion's job holds: the transcriber runs
    after the turn now (ADR-0099), so its failure reaches the job, not the turn."""
    from silica.ui.web import jobs

    assert jobs.drain(20)
    (rec,) = [r for r in jobs.listing() if r["source"]["name"] == name]
    assert rec["state"] == "failed"
    return rec["error"]


def test_hosted_a_converter_failure_reaches_the_job_without_its_raw_text(drop, monkeypatch):
    monkeypatch.setenv("SILICA_HOSTED", "1")

    def down(wav):
        raise ValueError("no transcription server at http://127.0.0.1:18236/v1/audio/transcriptions")

    monkeypatch.setitem(conv.ASR_PROVIDERS, "endpoint", down)
    text = drop("talk.mp3", 60)
    assert "- talk.mp3" in text and "Still converting" in text
    assert _failure("talk.mp3") == "it could not be converted"


def test_on_a_laptop_the_job_carries_the_converter_reason(drop, monkeypatch):
    def down(wav):
        raise ValueError("no transcription server at http://localhost:1236/v1")

    monkeypatch.setitem(conv.ASR_PROVIDERS, "endpoint", down)
    drop("talk.mp3", 60)
    assert _failure("talk.mp3") == "no transcription server at http://localhost:1236/v1"


def test_the_endpoint_lanes_mp3_is_measured_by_its_size(tmp_path, monkeypatch):
    """The endpoint lane sends ffmpeg's constant-bitrate mp3, which `wave`
    cannot open: an unmeasured file would be refused on every hosted tenant."""
    monkeypatch.setenv(adm.MAX_MINUTES_ENV, "1")
    ok = tmp_path / "audio.mp3"
    ok.write_bytes(b"\0" * 3000 * 59)  # 24 kbps = 3000 bytes a second
    assert adm.admit_audio(ok, "a.mp3") == pytest.approx(59.0)
    ok.write_bytes(b"\0" * 3000 * 61)
    with pytest.raises(adm.MediaRefused):
        adm.admit_audio(ok, "a.mp3")
