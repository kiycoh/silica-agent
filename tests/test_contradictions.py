"""ADR-0166: a contradiction's record, the proposals a check leaves, and the
four ways a person settles one, every one of them text in the note."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from silica.kernel.base import frontmatter
from silica.kernel.check import contradictions as cx
from silica.kernel.write.contested import contested_callout, mark_contested

TODAY = "2026-11-12"

IMPORTED = mark_contested(
    "---\ntitle: Dose\n---\n\nThe dose is 5 mg a day.\n\n"
    + contested_callout("The dose is 10 mg a day.", "notes-2026.md") + "\n",
    "source: notes-2026.md")

MINE = "The meeting is on Tuesday at noon."
NOTE_A = f"---\ntitle: A\n---\n\nIntro line here.\n\n{MINE}\n\nAnother closing line.\n"


def _refs(text: str) -> list:
    data, _raw, _body = frontmatter.split(text)
    return list((data or {}).get("contradictions") or [])


def test_an_imported_record_reads_with_no_date_and_its_callout_line():
    (r,) = cx.records(IMPORTED)
    assert r["other"] == "notes-2026.md" and r["note"] == "" and r["their"] == "The dose is 10 mg a day."
    assert r["found"] == "" and cx.age(r["found"]) is None
    assert IMPORTED.split("\n")[r["line"] - 1].startswith("> [!warning] Contradiction")


def test_a_new_record_carries_both_quotes_and_the_day_without_an_em_dash():
    text = cx.add_record(NOTE_A, "[[B]]", "The meeting is on Wednesday.", MINE, TODAY)
    (r,) = cx.records(text)
    assert (r["other"], r["note"], r["their"], r["mine"], r["found"]) == (
        "[[B]]", "B", "The meeting is on Wednesday.", MINE, TODAY)
    assert r["how"] == cx.FOUND_BY and "—" not in cx.callout("[[B]]", "x", "y", TODAY)
    assert _refs(text) == ["source: [[B]]"]


def test_this_holds_files_the_other_side_and_says_who_won():
    text = cx.add_record(NOTE_A, "[[B]]", "The meeting is on Wednesday.", MINE, TODAY)
    out = cx.settle(text, "source: [[B]]", "this", TODAY)
    live, filed = out.split("## Superseded")
    assert MINE in live and "Wednesday" not in live and "Wednesday" in filed
    assert f"Resolved {TODAY}: this note holds." in filed
    assert "contested" not in frontmatter.split(out)[0] and cx.records(out) == []


def test_the_other_holds_files_this_notes_words_and_keeps_the_winner_in_place():
    text = cx.add_record(NOTE_A, "[[B]]", "The meeting is on Wednesday.", MINE, TODAY)
    out = cx.settle(text, "source: [[B]]", "other", TODAY)
    live, filed = out.split("## Superseded")
    assert "> [!note] Contradiction from [[B]]" in live and "Wednesday" in live
    assert f"Resolved {TODAY}: [[B]] holds." in live
    assert f"\n{MINE}\n" not in live and f"> [!quote] Superseded {TODAY} by [[B]]\n> {MINE}" in filed
    assert "Intro line here.\n\nAnother closing line." in live  # one blank line where the paragraph stood
    assert cx.records(out) == []


def test_the_other_holds_leaves_an_edited_sentence_to_the_person():
    text = cx.add_record(NOTE_A, "[[B]]", "Wednesday.", MINE, TODAY).replace(
        f"\n{MINE}\n", "\nThe meeting is on Tuesday, maybe.\n")
    out = cx.settle(text, "source: [[B]]", "other", TODAY)
    assert "The meeting is on Tuesday, maybe." in out and "## Superseded" not in out


def test_both_hold_needs_its_condition_and_keeps_both():
    text = cx.add_record(NOTE_A, "[[B]]", "Wednesday.", MINE, TODAY)
    with pytest.raises(ValueError):
        cx.settle(text, "source: [[B]]", "both", TODAY)
    out = cx.settle(text, "source: [[B]]", "both", TODAY, condition="the B meeting is the rehearsal")
    assert f"Resolved {TODAY}: both hold when the B meeting is the rehearsal." in out
    assert MINE in out and "## Superseded" not in out and cx.records(out) == []


def test_kept_open_dates_the_look_and_stays_open():
    out = cx.settle(IMPORTED, "source: notes-2026.md", "open", TODAY)
    (r,) = cx.records(out)
    assert r["kept"] == [TODAY] and r["their"] == "The dose is 10 mg a day."


def test_a_flag_settles_this_or_other_and_refuses_both():
    flagged = mark_contested(NOTE_A, "flagged: wrong day (by user, 2026-11-01)")
    (r,) = cx.records(flagged)
    assert (r["their"], r["found"], r["line"]) == ("wrong day", "2026-11-01", 0)
    with pytest.raises(ValueError):
        cx.settle(flagged, r["ref"], "both", TODAY, condition="x")
    assert cx.settle(flagged, r["ref"], "open", TODAY) == flagged
    out = cx.settle(flagged, r["ref"], "this", TODAY)
    assert f"Resolved {TODAY}: this note holds." in out.split("## Superseded")[1] and cx.records(out) == []


def test_a_note_whose_only_properties_were_the_contest_keeps_no_empty_block():
    flagged = mark_contested("# Founding\n\nFounded in 1987.\n", "flagged: wrong year (by user, 2026-11-02)")
    out = cx.settle(flagged, cx.records(flagged)[0]["ref"], "this", TODAY)
    assert out.startswith("# Founding") and "{}" not in out
    imported = mark_contested("Body.\n\n" + contested_callout("Other.", "s.md") + "\n", "source: s.md")
    assert cx.settle(imported, "source: s.md", "this", TODAY).startswith("Body.")


def test_an_imported_record_settles_this_through_the_old_resolver():
    out = cx.settle(IMPORTED, "source: notes-2026.md", "this", TODAY)
    assert f"Resolved {TODAY}: this note holds." in out and cx.records(out) == []


def test_has_record_reads_a_settled_pair_and_the_other_sides_line():
    text = cx.add_record(NOTE_A, "[[B]]", "x", MINE, TODAY)
    assert cx.has_record(text, "B") and not cx.has_record(text, "Bee")
    assert cx.has_record("..\n" + cx.side_line("A", "this", TODAY) + "\n", "A")
    assert cx.side_line("A", "both", TODAY, "in winter") == (
        f"> [!note] Contradiction with [[A]] resolved {TODAY}: both hold when in winter.")


def test_a_whole_note_passage_is_focused_on_the_block_that_answers():
    theirs = "# Launch plan\n\nThe public launch is set for March 3.\n\nThe pricing page goes live early.\n"
    passage = "Launch plan The public launch is set for March 3. The pricing page goes live early."
    assert cx._focus(theirs, passage, "The board moved the public launch to April 14.") == (
        3, "The public launch is set for March 3.")
    assert cx._focus(theirs, "words found nowhere", "x") == (0, "words found nowhere")


def _cache(vault: Path, path: str, line: int, at: str, passage: str) -> None:
    from silica.kernel.check.run import _cache_path

    p = _cache_path(str(vault), path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"verdicts": {}, "report": {"findings": [
        {"kind": "contradicted", "grade": "error", "line": line, "end": line, "at": at, "passage": passage}]}}))


@pytest.fixture
def pair(tmp_vault):
    from silica.config import CONFIG

    tmp_vault.note("Work/A.md", NOTE_A)
    tmp_vault.note("Work/B.md", "---\ntitle: B\n---\n\nThe meeting is on Wednesday at noon.\n")
    _cache(Path(CONFIG.vault_path), "Work/A.md", 7, "Work/B.md:5", "The meeting is on Wednesday at noon.")
    return Path(CONFIG.vault_path)


def test_a_check_proposes_once_and_a_resolution_writes_both_notes_in_one_run(pair):
    from silica.kernel.write.undo_journal import get_undo_journal

    (p,) = cx.proposals(str(pair))
    assert (p["path"], p["line"], p["mine"], p["other"], p["their_line"]) == ("Work/A.md", 7, MINE, "[[B]]", 5)
    view = cx.note_view(str(pair), "Work/A.md")
    assert view["records"] == [] and len(view["proposals"]) == 1
    res = cx.resolve(str(pair), "Work/A.md", "this", at=p["at"], line=p["line"])
    assert res["paths"] == ["Work/A.md", "Work/B.md"] and res["contested"] is False
    a, b = (pair / "Work/A.md").read_text(), (pair / "Work/B.md").read_text()
    assert MINE in a and f"Resolved {TODAY[:4]}" in a.split("## Superseded")[1]
    assert "Contradiction with [[A]] resolved" in b
    assert cx.proposals(str(pair)) == []  # the pair is history now
    from silica.kernel.link.sweep import revert_and_sweep

    assert get_undo_journal().run_info(res["run"])["source"] == "user:/write"
    revert_and_sweep(res["run"])
    # the driver stamps a derived `type:` on the way back; the words are the person's again
    assert frontmatter.split((pair / "Work/A.md").read_text())[2] == frontmatter.split(NOTE_A)[2]
    assert "Contradiction" not in (pair / "Work/B.md").read_text()


def test_keeping_a_proposal_open_writes_the_record_and_lists_it(pair):
    (p,) = cx.proposals(str(pair))
    res = cx.resolve(str(pair), "Work/A.md", "open", at=p["at"], line=p["line"])
    assert res["contested"] is True and res["paths"] == ["Work/A.md"]
    (row,) = cx.open_records()
    assert row["path"] == "Work/A.md" and row["mine"] == MINE and row["age"] == 0
    (r,) = cx.note_view(str(pair), "Work/A.md")["records"]
    lines = (pair / "Work/A.md").read_text().split("\n")
    assert lines[r["mine_line"] - 1] == MINE and (r["other_path"], r["their_line"]) == ("Work/B.md", 5)
    with pytest.raises(LookupError):
        cx.resolve(str(pair), "Work/A.md", "this", at=p["at"], line=p["line"])
