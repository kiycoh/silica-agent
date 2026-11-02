# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0147: a system notification is posted only to a person who left the
tab and allowed it, never carries an answer's text, and the permission is
asked once per page load inside a gesture. The helpers of app-chat.js run
under node with a fake Notification; the calls from the calendar, the jobs
and the composer are checked in place, and the whole was verified by opening
the app."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

PRELUDE = """
globalThis.window = globalThis;
var DEMO = false;
const posted = [];
let asks = 0;
class FakeNotification {
  constructor(title, opts) {
    if (FakeNotification.throws) throw new TypeError("Illegal constructor");
    posted.push({ title, ...opts });
  }
  close() {}
}
FakeNotification.permission = "granted";
FakeNotification.requestPermission = () => { asks++; return Promise.resolve("granted"); };
globalThis.Notification = FakeNotification;
const listeners = {};
globalThis.document = {
  visibilityState: "hidden",
  hasFocus: () => false,
  addEventListener: (name, f) => { listeners[name] = f; },
};
"""


def _run(tmp_path, scenario: str):
    src = (STATIC / "app-chat.js").read_text(encoding="utf-8")
    block = src[src.index("// os-notify:start"):src.index("// os-notify:end")]
    script = tmp_path / "n.js"
    script.write_text(PRELUDE + block + scenario
                      + "\nconsole.log(JSON.stringify({ posted, asks, out: globalThis.out }));\n",
                      encoding="utf-8")
    res = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@needs_node
def test_posted_when_away_and_allowed(tmp_path):
    got = _run(tmp_path, 'globalThis.out = osNotify("Reminder", "Exam · 2026-11-02 09:00", "silica-reminder-exam@x");')
    assert got["out"] is True
    assert got["posted"] == [{"title": "Reminder", "body": "Exam · 2026-11-02 09:00",
                              "tag": "silica-reminder-exam@x", "icon": "/static/favicon.svg"}]


@needs_node
@pytest.mark.parametrize("setup", [
    'document.visibilityState = "visible"; document.hasFocus = () => true;',  # looking at it
    'Notification.permission = "default";',                                    # never allowed
    'Notification.permission = "denied";',
    "Notification.throws = true;",                                             # Chrome on Android
])
def test_not_posted_otherwise_and_the_caller_keeps_its_toast(tmp_path, setup):
    got = _run(tmp_path, setup + '\nglobalThis.out = osNotify("Reminder", "x", "t");')
    assert got["out"] is False and got["posted"] == []


@needs_node
def test_a_visible_window_without_focus_is_away(tmp_path):
    got = _run(tmp_path, 'document.visibilityState = "visible";\nglobalThis.out = osNotify("R", "x", "t");')
    assert got["out"] is True


@needs_node
def test_an_answer_is_announced_without_its_text_and_a_stop_is_not(tmp_path):
    got = _run(tmp_path, """
listeners["silica:turn-end"]({ detail: { outcome: "success" } });
listeners["silica:turn-end"]({ detail: { outcome: "idle" } });
listeners["silica:turn-end"]({ detail: { outcome: "error" } });
""")
    assert [p["title"] for p in got["posted"]] == ["Silica answered", "Silica could not answer"]
    assert {p["tag"] for p in got["posted"]} == {"silica-turn"}
    assert all("ready" in p["body"] or "failed" in p["body"] for p in got["posted"])


@needs_node
def test_the_permission_is_asked_once_and_only_while_undecided(tmp_path):
    got = _run(tmp_path, 'Notification.permission = "default"; askNotify(); askNotify();')
    assert got["asks"] == 1
    assert _run(tmp_path, "askNotify();")["asks"] == 0  # already granted
    assert _run(tmp_path, 'Notification.permission = "default"; DEMO = true; askNotify();')["asks"] == 0


def test_the_three_events_and_the_asks_are_wired():
    cal = (STATIC / "app-calendar.js").read_text(encoding="utf-8")
    jobs = (STATIC / "app-jobs.js").read_text(encoding="utf-8")
    notes = (STATIC / "app-notes.js").read_text(encoding="utf-8")
    chat = (STATIC / "app-chat.js").read_text(encoding="utf-8")
    # A posted reminder is filtered out before the hold, so it is not toasted twice.
    assert cal.index("osNotify(r.late") < cal.index("heldReminders.push(...due)")
    assert "if (v.reminder) askNotify();" in cal
    assert "silica-job-${j.id}" in jobs
    assert notes.count("askNotify()") == 2  # a staged file, an import
    assert "/^\\/nucleate\\b/.test(text.trim())) askNotify();" in chat
    # Never at load: every ask sits inside a function a gesture calls.
    for src in (cal, jobs, notes, chat):
        for line in src.splitlines():
            if "askNotify();" in line:
                assert line.startswith((" ", "\t")), line
