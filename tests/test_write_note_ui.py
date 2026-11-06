# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0155 and ADR-0110's amendment of 2026-11-06, as the page ships them. What a
press does is tests/test_write_note_e2e.py's; this holds what must stay in place."""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "silica" / "ui" / "web" / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
NOTES = (STATIC / "app-notes.js").read_text(encoding="utf-8")
WRITE = (STATIC / "app-write.js").read_text(encoding="utf-8")
SHELL = (STATIC / "app-shell.js").read_text(encoding="utf-8")


def test_the_note_view_is_a_view_with_no_row_among_the_views():
    assert 'id="view-note"' in INDEX
    views = INDEX[INDEX.index('<nav id="views"'):INDEX.index("</nav>", INDEX.index('<nav id="views"'))]
    assert "note" not in re.findall(r'data-tab="(\w+)"', views)
    assert 'tab === "chat" || tab === "note"' in SHELL, "a view with no row is served to everyone, as a chat is"


def test_the_doors_are_all_called_write_a_note():
    for door in ('id="write-note"', 'id="rail-write"', 'id="vam-write"'):
        assert door in INDEX, door
    assert 'item("Write a note"' in SHELL
    assert "write a note</button>" in SHELL, "the empty vault's line"
    assert 'e.code === "KeyN"' in WRITE and "e.altKey" in WRITE, "Alt+N, since Ctrl+N is the browser's"


def test_app_write_comes_after_the_sessions_it_extends():
    assert INDEX.index("/static/app-notes.js") < INDEX.index("/static/app-write.js") < INDEX.index("/static/app-boot.js")


def test_a_conflict_is_a_choice_and_typing_is_not_one():
    assert "keep typing to overwrite" not in NOTES
    for act in ("Keep mine", "Take the disk's", "Compare"):
        assert f'act("{act}"' in NOTES, act
    assert "if (s.conflict) { paintFoot(s); return; }" in NOTES


def test_the_foot_says_saved_only_on_the_servers_answer():
    typed = NOTES[NOTES.index("function typed(s)"):NOTES.index("function setPhase")]
    assert 'setPhase(s, "saving")' in typed and '"saved"' not in typed


def test_the_bundle_exports_what_the_note_view_reads():
    script = (STATIC.parent.parent.parent.parent / "scripts" / "vendor_codemirror.sh").read_text(encoding="utf-8")
    for name in ("autocompletion", "startCompletion", "search", "searchKeymap", "SearchQuery", "setSearchQuery",
                 "getSearchQuery", "findNext", "findPrevious", "replaceNext", "replaceAll", "openSearchPanel",
                 "closeSearchPanel", "showTooltip", "tooltips"):
        assert re.search(rf"\b{name}\b", script), name
    built = (STATIC / "codemirror.min.js").read_text(encoding="utf-8")
    assert "@codemirror/autocomplete@6.20.4" in built and "@codemirror/search@6.7.2" in built


def test_a_selection_has_both_checks_and_verify_stays_the_persons():
    assert 'item("check against the vault"' in NOTES and 'if (checkWebOpen) item("check against the web"' in NOTES
    assert 'id="sel-check"' in INDEX and "data-check" in INDEX
    assert "WEB_CLAIM_MAX = 600" in NOTES
    assert '"verify' not in WRITE


def test_a_demo_shows_no_check():
    css = (STATIC / "app-shell.css").read_text(encoding="utf-8")
    rule = css[css.index("html[data-demo] :is("):]
    rule = rule[:rule.index("{")]
    for control in ("#note-check-open", "#sel-check", "#nv-selbar [data-check]", '#nv-menu [data-act="check"]'):
        assert control in rule, control


def test_the_bar_cannot_leave_the_narrowest_drawer():
    css = (STATIC / "app-write.css").read_text(encoding="utf-8")
    assert "flex-wrap: wrap; max-width: calc(100% - 16px)" in css
