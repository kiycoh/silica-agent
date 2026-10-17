# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0118 end to end: the GUI served for real on a seeded vault, driven in
headless Chrome by real mouse and key events over the DevTools protocol.

The unit tests pin the payload and the wiring; this proves that what the
browser draws and what lands on disk agree. Opt-in (SILICA_E2E=1) because it
starts a browser per test, about 40 s for the file, and a CI runner's Chrome
is not one this suite pins. No new dependency: `websockets` is the [connect]
extra's, and Chrome is the one on the machine.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

CHROME = next((shutil.which(b) for b in ("google-chrome", "chromium", "chromium-browser") if shutil.which(b)), None)
pytestmark = [
    pytest.mark.skipif(not os.environ.get("SILICA_E2E"), reason="opt-in: SILICA_E2E=1 starts a browser"),
    pytest.mark.skipif(CHROME is None, reason="needs Chrome or Chromium"),
]

HOUR = 48  # px an hour, CAL_HOUR in app-calendar.js
TODAY = dt.date.today()
MON = TODAY - dt.timedelta(days=TODAY.weekday())
DAY = {n: MON + dt.timedelta(days=i) for i, n in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}


def _seed(vault) -> None:
    """One student's week, dated from this week so the test runs any day."""
    def ev(rel: str, fm: str) -> None:
        vault.note(rel, f"---\n{fm}\n---\n")

    m, thu, sat = DAY["mon"], DAY["thu"], DAY["sat"]
    ev("calendar/Lezione.md", f"title: Lezione\nevent_start: {m} 09:00\nevent_end: {m} 11:00\n"
       "event_rrule: FREQ=WEEKLY;BYDAY=MO,TU,TH")
    ev(f"calendar/{thu} Call.md", f"title: Call\nevent_start: {thu} 09:30\nevent_end: {thu} 10:30")
    ev(f"calendar/{m} Iscrizione.md", f"title: Iscrizione\nevent_start: {m}\nevent_end: {DAY['wed']}")
    ev(f"calendar/{sat} Ripasso.md", f"title: Ripasso\nevent_start: {sat} 10:00\nevent_end: {sat} 12:00\n"
       "event_plan: '[[Plans/Exam]]'")
    for i, h in enumerate(("08:00", "12:00", "13:00"), 1):  # enough for a month cell's "+N more"
        ev(f"calendar/{m} Extra {i}.md", f"title: Extra {i}\nevent_start: {m} {h}\nevent_end: {m} {h[:2]}:30")
    ev(f"calendar/{TODAY} Alba.md", f"title: Alba\nevent_start: {TODAY} 00:00\nevent_end: {TODAY} 00:15")
    ev(f"calendar/{TODAY} Sera.md", f"title: Sera\nevent_start: {TODAY} 23:30\nevent_end: {TODAY} 23:45")
    vault.note("Plans/Exam.md", "---\ntype: plan\nscope: folder\ntarget: Bio/\n"
               f"by: '{TODAY + dt.timedelta(days=13)}'\n---\n\n# Exam\n")
    vault.note("Plans/Words.md", "---\ntype: plan\nscope: folder\ntarget: Bio/\nby: Nov 15\n---\n\n# Words\n")
    vault.note(".obsidian/core-plugins.json", json.dumps({"daily-notes": True}))
    vault.note(".obsidian/daily-notes.json", json.dumps({"folder": "Daily", "format": "YYYY-MM-DD"}))
    vault.note(f"Daily/{TODAY}.md", "Finire la relazione entro stasera.\n\n"
               f"- [ ] Consegnare la relazione 📅 {TODAY}\n"
               f"- [ ] Rivedere appunti 📅 {TODAY - dt.timedelta(days=1)}\n")


class Page:
    """Just enough of the DevTools protocol: evaluate, mouse, keys, viewport."""

    def __init__(self, ws_url: str):
        from websockets.sync.client import connect

        self.ws = connect(ws_url, max_size=None, open_timeout=10)
        self.n = 0
        self.exceptions: list[str] = []
        self.send("Runtime.enable")
        self.send("Page.enable")

    def send(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv(timeout=30))
            if msg.get("method") == "Runtime.exceptionThrown":
                d = msg["params"]["exceptionDetails"]
                self.exceptions.append(d.get("exception", {}).get("description") or d.get("text", ""))
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def js(self, expr: str):
        r = self.send("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})
        if "exceptionDetails" in r:
            raise AssertionError(f"page threw on {expr[:80]!r}: {r['exceptionDetails']}")
        return r["result"].get("value")

    def wait(self, expr: str, timeout: float = 15, what: str = "") -> object:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            v = self.js(expr)
            if v:
                return v
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for {what or expr}")

    def center(self, el: str) -> tuple[float, float]:
        """The middle of the element a JS expression finds, scrolled into view."""
        r = self.js(f"(() => {{ const e = {el}; if (!e) return null; e.scrollIntoView({{block: 'center'}});"
                    " const r = e.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; })()")
        assert r, f"no element for {el}"
        return r[0], r[1]

    def mouse(self, kind: str, x: float, y: float, pressed: bool = False) -> None:
        self.send("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                               "buttons": 1 if pressed else 0, "clickCount": 1})

    def click(self, el: str) -> None:
        x, y = self.center(el)
        self.mouse("mouseMoved", x, y)
        self.mouse("mousePressed", x, y, True)
        self.mouse("mouseReleased", x, y)

    def drag(self, x: float, y0: float, y1: float) -> None:
        self.mouse("mouseMoved", x, y0)
        self.mouse("mousePressed", x, y0, True)
        for i in range(1, 6):
            self.mouse("mouseMoved", x, y0 + (y1 - y0) * i / 5, True)
        self.mouse("mouseReleased", x, y1)

    def key(self, k: str) -> None:
        code = f"Key{k.upper()}" if len(k) == 1 else k
        self.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": k, "code": code, "text": k if len(k) == 1 else ""})
        self.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": k, "code": code})

    def type(self, text: str) -> None:
        self.send("Input.insertText", {"text": text})

    def open(self, url: str) -> None:
        self.send("Page.navigate", {"url": url})
        self.wait("document.querySelectorAll('.ct-col').length === 7", 30, "the week to draw")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def cal(tmp_vault):
    import uvicorn

    from silica.ui.web import server

    _seed(tmp_vault)
    server._reset_session()
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    end = time.monotonic() + 15
    while not srv.started and time.monotonic() < end:
        time.sleep(0.05)
    assert srv.started, "the GUI did not start"

    profile = tempfile.mkdtemp(prefix="silica-e2e-")
    chrome = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
                               "--window-size=1440,900", "--no-first-run", "--no-default-browser-check",
                               "--disable-extensions", "--mute-audio", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    page = None
    try:
        marker = Path(profile) / "DevToolsActivePort"
        end = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < end:
            time.sleep(0.05)
        devtools = int(marker.read_text().split()[0])
        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{devtools}/json/list", timeout=10))
        page = Page(next(t for t in targets if t["type"] == "page")["webSocketDebuggerUrl"])
        page.send("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        page.base = f"http://127.0.0.1:{port}"
        from silica.config import CONFIG
        page.vault = Path(CONFIG.vault_path)
        page.open(page.base + "/#calendar")
        yield page
        assert not page.exceptions, f"the page threw: {page.exceptions}"
    finally:
        if page:
            page.ws.close()
        chrome.terminate()
        chrome.wait(10)
        shutil.rmtree(profile, ignore_errors=True)
        srv.should_exit = True
        th.join(10)


def _col(date: dt.date) -> str:
    return f"document.querySelector('.ct-col[data-date=\"{date}\"]')"


def _ev(date: dt.date, title: str) -> str:
    return f"[...{_col(date)}.querySelectorAll('.ct-ev')].find(e => e.textContent.includes({title!r}))"


def _btn(text: str, scope: str = "document") -> str:
    return f"[...{scope}.querySelectorAll('button')].find(b => b.textContent.trim() === {text!r})"


def test_the_week_draws_each_event_at_its_hour(cal):
    top = lambda el: cal.js(f"parseFloat({el}.style.top)")  # noqa: E731
    assert top(_ev(DAY["mon"], "Lezione")) == 9 * HOUR
    assert cal.js(f"parseFloat({_ev(DAY['mon'], 'Lezione')}.style.height)") == 2 * HOUR - 2
    # overlapping on Thursday: side by side, each half the column
    lez, call = (cal.js(f"(() => {{ const r = {_ev(DAY['thu'], t)}.getBoundingClientRect(); return [r.left, r.width]; }})()")
                 for t in ("Lezione", "Call"))
    width = cal.js(f"{_col(DAY['thu'])}.getBoundingClientRect().width")
    assert lez[0] != call[0] and max(lez[1], call[1]) < width * 0.6
    # the all-day span covers Monday to Wednesday, after the hours gutter
    assert cal.js("[...document.querySelectorAll('.ct-bar')].find(b => b.textContent.includes('Iscrizione')).style.gridColumn") == "2 / 5"
    # a session wears its plan's diamond; a plan still ahead is a countdown bar, a `by` in words is none
    assert cal.js(f"{_ev(DAY['sat'], 'Ripasso')}.matches('.sess') && !!{_ev(DAY['sat'], 'Ripasso')}.querySelector('.cal-dia')")
    bars = cal.js("[...document.querySelectorAll('.ct-plan')].map(b => b.textContent)")
    assert len(bars) == 1 and "Exam" in bars[0] and "13 days" in bars[0], bars
    # now is a line in today's column, at the current minute
    now = dt.datetime.now()
    assert abs(cal.js(f"parseFloat({_col(TODAY)}.querySelector('.ct-now').style.top)")
               - (now.hour * 60 + now.minute) * HOUR / 60) <= HOUR / 60 * 2


def test_a_drag_writes_the_session_note_and_draws_it_at_once(cal):
    sun = DAY["sun"]
    cal.js(f"document.querySelector('.ct-scroll').scrollTop = {12 * HOUR}")
    x, top = cal.js(f"(() => {{ const r = {_col(sun)}.getBoundingClientRect(); return [r.x + r.width / 2, r.top]; }})()")
    cal.drag(x, top + 14 * HOUR + 5, top + 16 * HOUR)
    cal.wait("!!document.querySelector('.cq-title')", what="the quick card")
    assert cal.js("[...document.querySelectorAll('.cal-card input[type=time]')].map(i => i.value)") == ["14:00", "16:00"]
    cal.type("Esercizi")
    cal.click(_btn("Study session", "document.querySelector('.cal-card')"))
    want = f"calendar/{sun} Esercizi.md"
    cal.wait(f"document.querySelector('.cq-path').textContent === 'Saved as {want}'", what="the path preview")
    cal.click("document.querySelector('.cq-save')")
    note = cal.vault / want
    cal.wait(f"!!{_ev(sun, 'Esercizi')}", 5, "the saved event in Sunday's column")
    assert cal.js(f"parseFloat({_ev(sun, 'Esercizi')}.style.top)") == 14 * HOUR
    text = note.read_text(encoding="utf-8")
    assert f"event_start: {sun} 14:00" in text and f"event_end: {sun} 16:00" in text
    assert "event_plan: '[[Plans/Exam]]'" in text and "AI: true" not in text


def test_the_card_refuses_an_empty_start_and_writes_nothing(cal):
    cal.click("document.querySelector('#cal-new')")
    cal.wait("!!document.querySelector('.cq-title')", what="the quick card")
    cal.type("Vuoto")
    cal.js("(() => { const t = document.querySelector('.cal-card input[type=time]'); t.value = ''; })()")
    cal.click("document.querySelector('.cq-save')")
    cal.wait("!document.querySelector('.cq-err').hidden", what="the error line")
    assert "start time" in cal.js("document.querySelector('.cq-err').textContent")
    assert not list((cal.vault / "calendar").glob("*Vuoto*"))


def test_the_event_card_marks_a_started_event_done_and_cancels_a_future_one(cal):
    cal.click(_ev(TODAY, "Alba"))
    cal.wait("!!document.querySelector('.ce')", what="the event card")
    assert f"calendar/{TODAY} Alba.md" in cal.js("document.querySelector('.ce .cq-path').textContent")
    cal.click(_btn("Done", "document.querySelector('.ce')"))
    alba = cal.vault / f"calendar/{TODAY} Alba.md"
    cal.wait("!document.querySelector('.ce')", what="the card to close")
    end = time.monotonic() + 5
    while "event_status: done" not in alba.read_text(encoding="utf-8") and time.monotonic() < end:
        time.sleep(0.1)
    assert "event_status: done" in alba.read_text(encoding="utf-8")

    future = dt.datetime.now() < dt.datetime.combine(TODAY, dt.time(23, 30))
    cal.click(_ev(TODAY, "Sera"))
    cal.wait("!!document.querySelector('.ce')", what="the event card")
    # Done waits for the start: the kernel hides a closed event's later occurrences
    done = _btn("Done", "document.querySelector('.ce')")
    assert bool(cal.js("!!" + done)) is not future
    cal.click(_btn("Cancel event", "document.querySelector('.ce')"))
    sera = cal.vault / f"calendar/{TODAY} Sera.md"
    end = time.monotonic() + 5
    while "event_status: cancelled" not in sera.read_text(encoding="utf-8") and time.monotonic() < end:
        time.sleep(0.1)
    assert "event_status: cancelled" in sera.read_text(encoding="utf-8")
    if future:
        cal.wait(f"!{_ev(TODAY, 'Sera')}", 10, "the cancelled event to leave the week")


def test_the_keys_walk_the_four_views(cal):
    cal.key("d")
    cal.wait("document.querySelectorAll('.cd-tile').length === 7", what="the day's strip")
    stack = cal.wait("(() => { const t = document.querySelector('.cd-stack').textContent;"
                     " return t.includes('Finire la relazione') && t; })()", what="the daily excerpt")
    assert "Consegnare la relazione" in stack and "late since" in stack
    cal.key("m")
    cal.wait("document.querySelectorAll('.cal-day').length === 42", what="the month")
    cal.click("document.querySelector('button.cal-more')")  # Monday: Lezione and three extras
    cal.wait(f"calMode === 'day' && calFmt(calAnchor) === '{DAY['mon']}'", what="+N more to open its day")
    cal.key("a")
    cal.wait("document.querySelectorAll('.cal-ag-day').length === 7", what="the agenda")
    cal.key("w")
    cal.wait("document.querySelectorAll('.ct-col').length === 7", what="the week")
    cal.key("t")
    first = cal.js("document.querySelector('#cal-sub').textContent")
    cal.key("j")
    cal.key("j")
    later = MON + dt.timedelta(days=14)
    cal.wait(f"document.querySelector('.ct-col').dataset.date === '{later}'", what="two weeks on")
    cal.key("t")
    cal.wait(f"document.querySelector('#cal-sub').textContent === {first!r}", what="back to this week")


def test_a_layer_off_hides_its_kind_and_survives_a_reload(cal):
    assert cal.js("document.querySelectorAll('.ct-ev').length") > 0
    cal.click("[...document.querySelectorAll('#cal-layers button')].find(b => b.textContent.startsWith('Events'))")
    cal.wait("document.querySelectorAll('.ct-ev').length === 0", what="events to go")
    cal.open(cal.base + "/#calendar")
    assert cal.js("document.querySelectorAll('.ct-ev').length") == 0
    cal.click("[...document.querySelectorAll('#cal-layers button')].find(b => b.textContent.startsWith('Events'))")
    cal.wait("document.querySelectorAll('.ct-ev').length > 0", what="events back")


def test_a_month_cell_makes_an_all_day_event(cal):
    cal.key("m")
    cal.wait("document.querySelectorAll('.cal-day').length === 42", what="the month")
    day = DAY["wed"] + dt.timedelta(days=7)
    cell = (f"[...document.querySelectorAll('.cal-day')].find(c => c.querySelector('.cal-n')"
            f".getAttribute('aria-label').endsWith({day.strftime('%B')!r} + ' ' + {str(day.day)!r}))")
    cal.click(cell)
    cal.wait("!!document.querySelector('.cq-title')", what="the quick card")
    assert cal.js("document.querySelector('.cq-ad input').checked")
    cal.type("Gita")
    cal.click("document.querySelector('.cq-save')")
    note = cal.vault / f"calendar/{day} Gita.md"
    end = time.monotonic() + 5
    while not note.exists() and time.monotonic() < end:
        time.sleep(0.1)
    text = note.read_text(encoding="utf-8")
    assert f"event_start: '{day}'" in text or f"event_start: {day}\n" in text, text
    assert "AI: true" not in text  # typed in the card, so the person's


def test_a_narrow_view_folds_the_side_column_and_keeps_new_event(cal):
    cal.send("Emulation.setDeviceMetricsOverride", {"width": 900, "height": 760, "deviceScaleFactor": 1, "mobile": False})
    cal.wait("getComputedStyle(document.querySelector('#cal-side')).display === 'none'", what="the side column to fold")
    assert cal.js("getComputedStyle(document.querySelector('#cal-new-bar')).display") != "none"
    cal.click("document.querySelector('#cal-new-bar')")
    cal.wait("!!document.querySelector('.cq-title')", what="the quick card")


def test_a_reload_under_an_open_card_keeps_what_was_typed(cal):
    cal.click("document.querySelector('#cal-new')")
    cal.wait("!!document.querySelector('.cq-title')", what="the quick card")
    cal.type("bozza")
    cal.js("loadCalendar()")
    assert cal.js("document.querySelector('.cq-title') && document.querySelector('.cq-title').value") == "bozza"
    cal.key("Escape")
    cal.wait("!document.querySelector('.cal-card')", what="Esc to close the card")
