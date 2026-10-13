# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Admission for a media file before it reaches the transcriber.

On the hosted pilot every tenant transcribes on one shared whisper-server,
which runs one request at a time, so a file is a claim on the whole host's
transcription for as long as it runs. `_ASR_TIMEOUT_S` bounds a file only after
it has held the cores for two hours; the checks here refuse it before it
starts. Three of them, each off unless configured, so a laptop install keeps
the behaviour it had before this module:

  * SILICA_MEDIA_MAX_MINUTES: the longest file accepted.
  * SILICA_MEDIA_MINUTES_MONTHLY: the minutes one account may transcribe in a
    calendar month, counted in the account's own ~/.silica.
  * hosted (`silica.config.hosted()`): one file at a time per process, which on
    the pilot is one tenant.

Every refusal is a `MediaRefused` whose text is the sentence the user reads.
"""
from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import threading
import time
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from silica.config import hosted

logger = logging.getLogger(__name__)

MAX_MINUTES_ENV = "SILICA_MEDIA_MAX_MINUTES"
MONTHLY_MINUTES_ENV = "SILICA_MEDIA_MINUTES_MONTHLY"
# ffprobe reads a container's header, not its samples: seconds on any file a
# user can upload. The bound is for a file built to make it scan.
_PROBE_TIMEOUT_S = 60

_TRANSCRIBING = threading.Lock()


class MediaRefused(ValueError):
    """A media file admission turned away. The text is written for the user,
    so a caller that hides raw errors (hosted) shows this one as it is."""


def _minutes(key: str) -> float:
    """A configured limit in minutes; 0 means none."""
    raw = os.environ.get(key, "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        # Tolerated, not raised: raising here would refuse every media file
        # with a sentence about the operator's typo. The log names the key;
        # until it is fixed the limit is off, which is a laptop's default.
        logger.warning("%s=%r is not a number of minutes; no limit applies", key, raw)
        return 0.0
    return max(value, 0.0)


def _shown(minutes: float) -> str:
    return str(int(minutes)) if minutes == int(minutes) else f"{minutes:g}"


def _counter_path() -> Path:
    # paths._SILICA_HOME and not Path.home() here: the test suite repoints
    # that one name for every store under ~/.silica, so a test that forgets
    # this file still never writes the developer's real counter.
    from silica.kernel.recall import paths

    return paths._SILICA_HOME / "transcription-seconds.json"


def _month() -> str:
    return time.strftime("%Y-%m")


# Spelled out and not calendar.month_name, which follows the process's
# LC_TIME: a host left on it_IT would write "1 novembre" into English copy.
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _renewal() -> str:
    """The first day of next month, as the refusal names it."""
    return f"1 {_MONTHS[time.localtime().tm_mon % 12]}"


def _ledger() -> dict[str, float]:
    path = _counter_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # Tolerated: the file is written atomically, so this is a hand edit or
        # a disk fault. Starting the month from zero gives back at most one
        # month's allowance; refusing every file until an operator looks
        # would take transcription away from a user who did nothing.
        logger.warning("unreadable transcription counter %s; counting from zero", path)
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(v, int | float)} if isinstance(data, dict) else {}


def used_seconds() -> float:
    """Seconds transcribed this calendar month."""
    return _ledger().get(_month(), 0.0)


def probe_seconds(src: Path) -> float | None:
    """The container's own length, or None when ffprobe cannot say. None is
    not a pass: `admit_wav` measures the decoded audio before anything is sent."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S, stdin=subprocess.DEVNULL,
        )
        return float(proc.stdout.strip().splitlines()[0])
    except (subprocess.TimeoutExpired, OSError, ValueError, IndexError):
        # A file ffprobe cannot read is one ffmpeg will refuse next with its
        # own reason, and the decoded length is checked after it either way.
        return None


def _check(name: str, seconds: float) -> None:
    cap = _minutes(MAX_MINUTES_ENV)
    length = math.ceil(seconds / 60)
    if cap and seconds > cap * 60:
        raise MediaRefused(
            f"{name} is {length} minutes long, and files of up to {_shown(cap)} minutes "
            "are transcribed here. Split it into shorter parts and add them one at a time."
        )
    budget = _minutes(MONTHLY_MINUTES_ENV)
    if budget:
        left = max(budget * 60 - used_seconds(), 0.0)
        if seconds > left:
            raise MediaRefused(
                f"{name} is {length} minutes long, and {math.floor(left / 60)} of this month's "
                f"{_shown(budget)} minutes of transcription are left. They renew on {_renewal()}."
            )


def _limited() -> bool:
    return bool(_minutes(MAX_MINUTES_ENV) or _minutes(MONTHLY_MINUTES_ENV))


def admit(src: Path) -> None:
    """Refuse `src` on its container's length, before ffmpeg decodes it."""
    if not _limited():
        return
    seconds = probe_seconds(src)
    if seconds is not None:
        _check(src.name, seconds)


def admit_wav(wav: Path, name: str) -> float:
    """Refuse on the decoded audio's exact length, and return it for `charge`.

    The second check is the one that holds: a container header is the
    uploader's to write, and one that claims a minute over ten hours of
    samples passed `admit`."""
    if not _limited():
        return 0.0
    try:
        with wave.open(str(wav), "rb") as w:
            seconds = w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, ZeroDivisionError):
        # Unmeasured is not admitted: with a limit set, a length nobody read
        # is exactly the case the limit exists for.
        raise ValueError(f"the decoded audio of {name} could not be measured") from None
    _check(name, seconds)
    return seconds


def charge(seconds: float) -> None:
    """Add a transcription that ran to this month's count.

    Called once the transcriber has answered, whatever it heard: an hour of
    silence held the shared server for as long as an hour of speech. A refused
    file, or one the transcriber failed on, is never counted."""
    if not seconds or not _minutes(MONTHLY_MINUTES_ENV):
        return
    from silica.kernel.recall.paths import atomic_write_bytes

    ledger = _ledger()
    month = _month()
    ledger[month] = ledger.get(month, 0.0) + seconds
    atomic_write_bytes(_counter_path(), json.dumps(ledger, sort_keys=True).encode("utf-8"), mode=0o600)


@contextmanager
def one_at_a_time(name: str) -> Iterator[None]:
    """One transcription per process when hosted, which is one per tenant.

    Refused rather than waited on: a second file reaches here only past the
    turn gate, from a turn whose browser left while its conversion ran on (the
    gate is freed then and the worker thread is not), and a turn that waited
    would show a spinner for as long as the first file takes, up to two hours.
    Held around the budget check and the charge too, so two files cannot both
    pass against the same minutes left."""
    if not hosted():
        yield
        return
    if not _TRANSCRIBING.acquire(blocking=False):
        raise MediaRefused(
            f"Another file is still being transcribed for this account. "
            f"Add {name} again once it is done."
        )
    try:
        yield
    finally:
        _TRANSCRIBING.release()
