"""The vault's note index: what a note states about itself, read once, no body held."""
from __future__ import annotations

import os

import pytest

from silica.kernel.base import note_index


@pytest.fixture
def vault(tmp_path, monkeypatch):
    from silica.config import CONFIG

    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path), raising=False)
    notes = {
        "Plain.md": "---\ndate: 2026-01-02\n---\nbody\n",
        "Plans/Exam.md": "---\ntype: plan\n---\nbody\n",
        "Task.md": "---\nstatus: todo\ntype: Plan\n---\nbody\n",
        "Event.md": "---\nevent_start: 2026-02-02\n---\n",
        "Loser.md": '---\nsuperseded_by: "[[Winner|w]]"\n---\n',
        "Spine.md": '---\nsection: "(outline)"\naliases: a, b\n---\n',
        "Inbox/In.md": "---\ndate: 2020-01-01\n---\npaper\n",
    }
    for rel, text in notes.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_meta_says_what_each_note_is(vault):
    meta = note_index.meta(vault)
    assert {p: m["kind"] for p, m in meta.items()} == {
        "Event.md": "event", "Inbox/In.md": "staged", "Loser.md": "merged", "Plain.md": "",
        "Plans/Exam.md": "plan", "Spine.md": "", "Task.md": "note"}
    assert meta["Loser.md"]["winner"] == "Winner"
    assert meta["Spine.md"]["spine"] is True and meta["Spine.md"]["aliases"] == ["a", "b"]


def test_meta_answers_from_the_memo_until_the_vault_moves(vault):
    first = note_index.meta(vault)
    assert note_index.meta(vault) is first
    (vault / "New.md").write_text("x\n", encoding="utf-8")
    assert "New.md" in note_index.meta(vault)


def test_frontmatters_holds_what_parsed_parsed_and_no_body(vault):
    fms = note_index.frontmatters(vault)
    assert fms["Plans/Exam.md"] == {"type": "plan"} and fms["Event.md"]["event_start"]
    assert fms["Plain.md"] is note_index.parsed(vault, "Plain.md", body=False)[0]
    assert all(len(v) == 2 and "body" not in str(v[1]) for v in note_index._parsed_memo.values())


def test_a_broken_note_is_empty_properties_and_an_unreadable_one_is_gone(vault):
    (vault / "Broken.md").write_text("---\n- a\n- b\n---\nx\n", encoding="utf-8")
    (vault / "Binary.md").write_bytes(b"\xff\xfe\x00")
    fms = note_index.frontmatters(vault)
    assert fms["Broken.md"] == {} and "Binary.md" not in fms


def test_an_edit_inside_one_mtime_tick_is_still_read(vault):
    """The epoch moves on size as well as on mtime, so a memo that keyed on mtime
    alone served the old properties to whoever the epoch had just sent back."""
    note = vault / "Plain.md"
    assert note_index.parsed(vault, "Plain.md", body=False)[0]["date"]
    st = note.stat()
    note.write_text("---\ntype: plan\n---\nlonger body than before\n", encoding="utf-8")
    os.utime(note, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert note_index.parsed(vault, "Plain.md", body=False)[0] == {"type": "plan"}
