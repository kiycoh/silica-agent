# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0105: the first-visit tour's steps hang together, and every control
and announcement it relies on exists. Read off the static files; where the
bubble lands, what it dims and the real turns are verified by opening the app."""
from __future__ import annotations

import re

from tests.webassets import WEB, app_js

TOUR = (WEB / "app-tour.js").read_text(encoding="utf-8")
INDEX = (WEB / "index.html").read_text(encoding="utf-8")


def _steps() -> dict[str, str]:
    """Each step's name and the text of its literal, in the order written."""
    block = TOUR[TOUR.index("const TOUR_STEPS = {"):TOUR.index("const TOUR_RESUME")]
    names = re.findall(r"^  (\w+): \{", block, re.M)
    return dict(zip(names, re.split(r"^  \w+: \{", block, flags=re.M)[1:]))


def _resume() -> dict[str, str]:
    start = TOUR.index("const TOUR_RESUME")
    return dict(re.findall(r'(\w+): "(\w+)"', TOUR[start:TOUR.index(";", start)]))


def test_every_step_the_tour_goes_to_or_resumes_on_exists():
    steps = set(_steps())
    assert set(re.findall(r'tourGo\("(\w+)"\)', TOUR)) <= steps
    resume = _resume()
    assert set(resume) <= steps and set(resume.values()) <= steps


def test_every_step_moves_on_by_its_button_or_by_an_event_the_tour_listens_to():
    for name, body in _steps().items():
        wait = re.search(r'wait: "(\w+)"', body)
        assert wait or "primary:" in body, f"{name} has no way on"
        if wait:
            assert f'tourWaiting("{wait.group(1)}")' in TOUR, f"nothing ends {name}'s wait"


def test_the_numbers_run_one_to_four_in_order_after_the_welcome():
    ns = [int(re.search(r"\bn: (\d)", body).group(1)) for body in _steps().values()]
    assert ns == sorted(ns) and set(ns) == {0, 1, 2, 3, 4}


def test_the_controls_the_tour_lights_exist():
    for mark in ('id="vault-add"', 'id="composer"', 'class="composer-shell"',
                 'class="tab" data-tab="graph"', 'class="tab" data-tab="learn"',
                 'id="tree"', 'id="log"', 'id="tour-start"', 'id="vault-files"'):
        assert mark in INDEX, mark


def test_its_listeners_exist_before_the_first_lists_are_asked_for():
    scripts = re.findall(r'<script src="/static/([\w-]+\.js)">', INDEX)
    at = scripts.index("app-tour.js")
    assert at < scripts.index("app-settings.js")  # starts /vault_info and /sessions
    for needed in ("app-chat.js", "app-shell.js", "app-notes.js", "app-boot.js"):
        assert scripts.index(needed) < at, needed


def test_the_page_announces_what_the_tour_waits_on():
    page = app_js() + (WEB / "work.js").read_text(encoding="utf-8")
    for event in ("silica:vault-info", "silica:sessions", "silica:turn-end", "silica:view", "silica:job"):
        assert f'new CustomEvent("{event}"' in page, event
        assert f'"{event}"' in TOUR, event
