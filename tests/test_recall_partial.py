# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""silica_recall must say which notes are worth re-reading.

Measured on a real chat: 3 of 9 tool calls were silica_read_note on notes whose
FULL body recall had already delivered (render(windowed=True) emits the excerpt,
and for a short note the excerpt is the whole body). `partial` names the notes
that really are a slice, so the rest need no second call.
"""
from silica.kernel.recall.perception import NoteBlock, Perception
from silica.tools import graph


def _perception(*blocks):
    return Perception(query="q", blocks=list(blocks))


def _patch(monkeypatch, perception):
    import silica.kernel.recall.perception as perception_mod

    monkeypatch.setattr(perception_mod, "perceive",
                        lambda query, now, k: perception, raising=False)


def test_windowed_note_is_partial_whole_note_is_not(monkeypatch):
    _patch(monkeypatch, _perception(
        NoteBlock(path="A", date="", evidence="", body="line1\nline2\nline3", excerpt="line2"),
        NoteBlock(path="B", date="", evidence="", body="short body", excerpt="short body"),
    ))
    out = graph.silica_recall("q")
    assert out["notes"] == ["A", "B"]
    assert out["partial"] == ["A"]


def test_surrounding_whitespace_is_not_a_difference(monkeypatch):
    _patch(monkeypatch, _perception(
        NoteBlock(path="A", date="", evidence="", body="  body\n", excerpt="body"),
    ))
    assert graph.silica_recall("q")["partial"] == []


def test_no_blocks_is_an_empty_answer(monkeypatch):
    _patch(monkeypatch, _perception())
    out = graph.silica_recall("q")
    assert out["notes"] == [] and out["partial"] == []
