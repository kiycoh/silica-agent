# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0115: the chat's picture of the app is the help panel, read as the
surface renders it."""
from __future__ import annotations

import re

import tiktoken

from silica.ui.web.app_help import INDEX, app_message, panel


def _rows(sections):
    return [term for _, rows in sections for term, _ in rows]


def test_every_row_of_the_panel_is_read():
    html = INDEX.read_text(encoding="utf-8")
    block = html[html.index('<div id="help-panel"'):]
    assert len(_rows(panel(hosted=False))) == block.count('class="hp-row"', 0, block.index("</div>\n  <!--"))


def test_the_views_are_read_and_hosted_drops_what_it_hides():
    local, hosted = dict(panel(hosted=False)), dict(panel(hosted=True))
    assert [t for t, _ in local["the views"]] == ["a chat", "Explore", "Learn", "Calendar", "Examine", "a new note"]
    assert "Examine" not in [t for t, _ in hosted["the views"]]
    assert "Examine" not in _rows(panel(hosted=True))


def test_a_row_ends_where_its_sentence_ends():
    # The explore "tabs" row ended on "the panel at the right is" from 0.7.0 to
    # 2026-10-15: a truncation a reader skims past and a model repeats.
    for _, text in (r for _, rows in panel(hosted=False) for r in rows):
        assert not re.search(r"\b(is|the|and|of|to|a)$", text), text


def test_the_message_stays_under_its_ceiling(monkeypatch):
    # Past 1,000 tokens everything after the views moves behind a loadable tool
    # (ADR-0115 point 5). Measured 2026-10-15: 737 local, 607 hosted.
    enc = tiktoken.get_encoding("cl100k_base")
    for flag in ("", "1"):
        monkeypatch.setenv("SILICA_HOSTED", flag)
        assert len(enc.encode(app_message())) < 1000


def test_settings_are_named_never_valued(monkeypatch):
    monkeypatch.setenv("SILICA_HOSTED", "1")
    msg = app_message()
    assert "dictation language" in msg and "api key" not in msg
