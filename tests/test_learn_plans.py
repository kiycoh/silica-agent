# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Plans (learn-suite D17, D23, D24): scope resolution, the plan note read
and written through the gates, the ledger over the checkbox."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import silica.tools.notes  # noqa: F401 — the write gate registers on import
from silica.kernel.learn import plans

DAY = 86400.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@pytest.fixture
def vault(tmp_vault, tmp_path, monkeypatch):
    import silica.kernel.report.quiz as quiz
    from silica.kernel.write import undo_journal

    monkeypatch.setattr(quiz, "log_path", lambda: tmp_path / "quiz.jsonl")
    monkeypatch.setattr(undo_journal, "_DEFAULT_JOURNAL_PATH", tmp_path / "undo.db", raising=False)
    for name in ("Gradient descent", "Chain rule", "Backpropagation", "Softmax"):
        tmp_vault.note(f"ML/{name}.md", f"---\ndate: 2026-01-01\n---\n# {name}\n\nBody of {name}.\n")
    tmp_vault.note("ML/Deep/ResNet.md", "---\ndate: 2026-01-01\n---\n# ResNet\n\nSkip connections.\n")
    tmp_vault.note("Other/Note.md", "# Note\n\nElsewhere.\n")
    return tmp_vault


PLAN = """---
type: plan
scope: steps
target: Plans/ML.md
reach: relational
created: 2026-09-01
---
# Machine Learning

## Tree
- [[Gradient descent]] · [[Backpropagation]]

## Steps
- [x] [[Gradient descent]]: the entry note of the area
- [ ] [[Chain rule]] before [[Backpropagation]]: because the gradient is composed
- [ ] Momentum di Nesterov: not in the vault

## Sessions
- 2026-09-05 · 4 items · ratings: 3 Good/Easy · 1 Hard · next: Chain rule · session: abc12345
"""


def test_plan_note_parses_steps_checkboxes_links_and_sessions(vault):
    vault.note("Plans/ML.md", PLAN)
    p = plans.read_plan("Plans/ML.md")
    assert p["name"] == "Machine Learning" and p["scope"] == "steps" and p["reach_level"] == 3
    assert [s["checked"] for s in p["steps"]] == [True, False, False]
    assert p["steps"][1]["notes"] == ["Chain rule", "Backpropagation"]
    assert p["steps"][1]["because"].startswith("because the gradient")
    assert p["sessions"][0]["date"] == "2026-09-05" and p["sessions"][0]["items"] == 4
    assert p["sessions"][0]["session"] == "abc12345"
    assert plans.find_plan("Machine Learning")["path"] == "Plans/ML.md"
    assert plans.find_plan("ML")["path"] == "Plans/ML.md"


def test_ledger_over_checkbox(vault):
    """A ticked step with no evidence is not passed; an unticked step whose
    notes held the reach after the plan was written is."""
    from silica.kernel.learn import mastery
    from silica.kernel.report import quiz

    vault.note("Plans/ML.md", PLAN)
    p = plans.read_plan("Plans/ML.md")
    quiz.record([
        {"path": "ML/Chain rule.md", "correct": True, "rating": 3, "level": 3, "kind": "explain", "hints": 0},
        {"path": "ML/Backpropagation.md", "correct": True, "rating": 3, "level": 3, "kind": "explain", "hints": 0},
    ])
    assert mastery.steps_passed(p) == [False, True, False]
    report = plans.plan_report(p)
    assert report["steps"][0]["passed"] is False and report["steps"][1]["passed"] is True
    assert report["steps"][2]["gap"] is True
    assert report["frontier"] == ["ML/Gradient descent.md"]


def test_a_prerequisite_cycle_is_reported_not_linearized(vault, monkeypatch):
    from silica.kernel.report import learner

    vault.note("Plans/ML.md", PLAN)
    monkeypatch.setattr(learner, "prerequisites_map", lambda: {
        "ml/chain rule": ["ML/Backpropagation.md"], "ml/backpropagation": ["ML/Chain rule.md"]})
    report = plans.plan_report(plans.read_plan("Plans/ML.md"))
    assert report["cycles"] and {"ML/Chain rule.md", "ML/Backpropagation.md"} <= set(report["cycles"][0])


def test_the_four_scope_kinds_resolve_on_a_synthetic_vault(vault, monkeypatch):
    notes = plans.resolve_scope("notes", ["Softmax", "Chain rule", "Missing"])
    assert notes["paths"] == ["ML/Softmax.md", "ML/Chain rule.md"]
    folder = plans.resolve_scope("folder", "ML/")
    assert "ML/Deep/ResNet.md" in folder["paths"] and "Other/Note.md" not in folder["paths"]
    assert {t["node"] for t in folder["tree"]} == {"ML", "ML/Deep"}

    class FakeStore:
        lang = "en"

        def stem_postings(self):
            return {"softmax": {"ML/Softmax.md": 3, "ML/Backpropagation.md": 1}}

        def neighbors(self, term, k=10, scope=None):
            return [{"concept": "cross-entropy", "weight": 2.0}]

    monkeypatch.setattr("silica.kernel.recall.cooccurrence.get_cooccur_store", lambda lang=None: FakeStore())
    monkeypatch.setattr("silica.kernel.text.text.stem_word", lambda w, lang=None: w.lower())
    topic = plans.resolve_scope("topic", "softmax")
    assert topic["paths"] == ["ML/Softmax.md", "ML/Backpropagation.md"]
    # ADR-0081 §12: a topic's parts are the folders its notes sit in; the
    # neighbouring concepts are chips, not parts
    assert [t["node"] for t in topic["tree"]] == ["ML"] and topic["neighbours"] == ["cross-entropy"]
    steps = plans.resolve_scope("steps", "[[Chain rule]] before [[Backpropagation]]\nSoftmax\nMomentum di Nesterov")
    assert steps["paths"] == ["ML/Chain rule.md", "ML/Backpropagation.md", "ML/Softmax.md"]
    assert steps["gaps"] == ["Momentum di Nesterov"]           # kept, marked, never dropped
    restricted = plans.resolve_scope("steps", "Softmax\nNote", restrict="ML/")
    assert restricted["paths"] == ["ML/Softmax.md"] and restricted["gaps"] == ["Note"]


SCHEDA = """---
source_file: "scheda.pdf"
---
| DIPARTIMENTO | Matematica e Informatica |
| --- | --- |
| CFU | 9 |

PROGRAMMA

| ORE | Lezioni |
| --- | --- |
| 8 | Modelli lineari: regressione lineare multipla, [[Rete neurale multistrato\\|reti neurali multistrato]], analisi discriminante di Fisher, lasso. |
| 4 | Reti neurali profonde: [[Dropout\\|dropout]], apprendimento multi-task. |
"""


def test_a_syllabus_converted_into_tables_plans_one_line_per_block_and_names_what_is_missing(vault):
    """The official syllabus of a course came out of its PDF as tables, the
    programme as `| hours | block: topic, topic |` rows under a bare
    PROGRAMMA line (2026-10-23): no line was read, and a block of eight
    topics could meet two notes at most."""
    for name in ("Regressione lineare multipla", "Rete neurale multistrato", "Discriminante lineare di Fisher"):
        vault.note(f"ML/{name}.md", f"# {name}\n")
    vault.note("Corso/scheda.md", SCHEDA)

    res = plans.resolve_scope("steps", "scheda", restrict="ML/")

    assert res["steps"] == [
        "Modelli lineari: regressione lineare multipla, [[Rete neurale multistrato|reti neurali multistrato]], "
        "analisi discriminante di Fisher, lasso.",
        "Reti neurali profonde: [[Dropout|dropout]], apprendimento multi-task."]  # the alias bar read back
    block, deep = res["tree"]
    assert block["node"] == "Modelli lineari" and block["how"] == "3 of 4 topics"
    assert block["leaves"] == ["ML/Regressione lineare multipla.md", "ML/Rete neurale multistrato.md",
                               "ML/Discriminante lineare di Fisher.md"]
    assert block["missing"] == ["lasso"]
    assert deep["gap"] is True and deep["missing"] == ["Dropout", "apprendimento multi-task"]
    # a hyphenated topic meets its joined title
    vault.note("ML/AdaBoost.md", "# AdaBoost\n")
    from silica.kernel.report.learner import _notes_meta
    from pathlib import Path as _P
    from silica.config import CONFIG as _C
    found, missing = plans._topic_notes(["ada-boost", "random forest"], _notes_meta(_P(_C.vault_path)),
                                        {"ML/AdaBoost.md"})
    assert found == ["ML/AdaBoost.md"] and missing == ["random forest"]

    from pathlib import Path

    from silica.config import CONFIG

    out = plans.write_plan("Esame ML", "steps", "scheda", restrict="ML/", reach="relational", cards=[])
    text = (Path(CONFIG.vault_path) / out["path"]).read_text(encoding="utf-8")
    assert ("- [ ] Modelli lineari: [[Regressione lineare multipla]], [[Rete neurale multistrato]], "
            "[[Discriminante lineare di Fisher]] · not in the vault: lasso") in text
    assert "- [ ] Reti neurali profonde: not in the vault (Dropout, apprendimento multi-task)" in text
    steps = plans.parse_steps(text)
    assert steps[0]["notes"] == ["Regressione lineare multipla", "Rete neurale multistrato",
                                 "Discriminante lineare di Fisher"]


def test_a_plan_note_is_never_material_of_a_scope(vault, monkeypatch):
    # Measured 2026-10-08: the topic "memoria" listed Plans/Prova Learn.md among
    # its 45 notes, because the plan names the topic; a plan is the card, not
    # what it asks about.
    vault.note("ML/Plan of ML.md", "---\ntype: plan\nscope: folder\ntarget: ML/\n---\n# Plan of ML\n\nSoftmax and more.\n")

    class FakeStore:
        lang = "en"

        def stem_postings(self):
            return {"softmax": {"ML/Softmax.md": 3, "ML/Plan of ML.md": 2}}

        def neighbors(self, term, k=10, scope=None):
            return []

    monkeypatch.setattr("silica.kernel.recall.cooccurrence.get_cooccur_store", lambda lang=None: FakeStore())
    monkeypatch.setattr("silica.kernel.text.text.stem_word", lambda w, lang=None: w.lower())
    assert plans.resolve_scope("topic", "softmax")["paths"] == ["ML/Softmax.md"]
    assert "ML/Plan of ML.md" not in plans.resolve_scope("folder", "ML/")["paths"]


def test_legacy_syllabus_reads_as_a_steps_plan(vault):
    vault.note("Plans/Old.md", "---\ntype: syllabus\ntarget: ML/\n---\n# Old\n\n- [ ] [[Softmax]]: goal\n- [x] [[Chain rule]]: goal\n")
    p = plans.read_plan("Plans/Old.md")
    assert p["scope"] == "steps" and p["legacy"] is True
    assert [s["notes"] for s in p["steps"]] == [["Softmax"], ["Chain rule"]]
    assert plans.scope_of(p)["paths"] == ["ML/Softmax.md", "ML/Chain rule.md"]


def test_write_plan_lands_through_the_gate_with_tree_steps_and_gaps(vault):
    out = plans.write_plan("Esame", "steps", "[[Softmax]]\nMomentum di Nesterov", reach="relational", by="2026-10-20")
    assert out["path"].endswith("Esame.md") and out["gaps"] == ["Momentum di Nesterov"]
    text = plans._read(out["path"])
    p = plans.read_plan(out["path"])
    assert p["scope"] == "steps" and p["reach"] == "relational" and p["by"] == "2026-10-20" and p["created"]
    assert "type: plan" in text
    assert "## Tree" in text and "## Steps" in text and "## Sessions" in text
    assert "- [ ] [[Softmax]]" in text and "Momentum di Nesterov: not in the vault" in text
    assert "mastery" not in text.lower()
    folder = plans.write_plan("Area", "folder", "ML/")
    ftext = plans._read(folder["path"])
    assert "## Steps" not in ftext and "[[ResNet]]" in ftext


def test_session_line_lands_through_the_journal_and_reverts(vault):
    from silica.kernel.write.undo_journal import revert_run

    vault.note("Plans/ML.md", PLAN)
    before = plans._read("Plans/ML.md")
    run_id = plans.append_session("Plans/ML.md", "2026-09-07 · 8 items · ratings: 4 Good/Easy · 3 Hard · 1 Again · next: Softmax · session: deadbeef")
    after = plans._read("Plans/ML.md")
    assert after.count("session: deadbeef") == 1 and after.startswith("---")
    assert plans.append_session("Plans/ML.md", "2026-09-07 · 8 items · session: deadbeef") == ""   # a retry never duplicates
    assert plans._read("Plans/ML.md") == after
    res = revert_run(run_id)
    assert res["reverted"] == ["Plans/ML.md"] and plans._read("Plans/ML.md") == before


def test_revisit_and_completion_write_only_frontmatter_keys(vault):
    vault.note("Plans/ML.md", PLAN)
    plans.set_revisit("Plans/ML.md", ["ML/Softmax.md", "ML/Chain rule.md"])
    p = plans.read_plan("Plans/ML.md")
    assert p["revisit"] == ["ML/Softmax.md", "ML/Chain rule.md"]
    assert "## Steps" in plans._read("Plans/ML.md")
    # completion is evidence-backed: a plan with an open gap never completes
    assert plans.close_plan("Plans/ML.md")["status"] == "active"
    assert plans.close_plan("Plans/ML.md", reopen=True)["status"] == "active"
    vault.note("Plans/Done.md", PLAN.replace("- [ ] Momentum di Nesterov: not in the vault\n", "").replace("scope: steps", "scope: steps"))
    from silica.kernel.report import quiz

    quiz.record([{"path": p, "correct": True, "rating": 3, "level": 3, "kind": "explain", "hints": 0}
                 for p in ("ML/Gradient descent.md", "ML/Chain rule.md", "ML/Backpropagation.md")])
    out = plans.close_plan("Plans/Done.md")
    assert out["status"] == "completed" and out["completed"]
    text = plans._read("Plans/Done.md")
    assert "status: completed" in text and "completed:" in text
    assert plans.close_plan("Plans/Done.md", reopen=True)["status"] == "active"
    assert "status: completed" not in plans._read("Plans/Done.md")


def test_pace_is_workload_arithmetic_and_never_divides_by_zero(vault):
    p = plans.read_plan("Plans/ML.md", PLAN.replace("created: 2026-09-01", "created: 2026-09-01\nby: 2000-01-01"))
    report = plans.plan_report(p)
    assert report["pace"]["days_left"] == 0 and "remaining" in report["pace"]["say"].lower()
    p2 = plans.read_plan("Plans/ML.md", PLAN.replace("created: 2026-09-01", "created: 2026-09-01\nby: 2999-01-01"))
    r2 = plans.plan_report(p2)
    assert r2["pace"]["per_day"] >= 0 and "session" in r2["pace"]["say"]


# ------------------------------------------------------------------ ADR-0081: the sentence of Create plan

def test_the_date_reader_takes_day_and_month_names_numbers_and_iso_and_never_a_past_date():
    from datetime import date

    today = date(2026, 10, 8)
    read = lambda s: plans.read_date(s, today)  # noqa: E731
    assert read("Esame di Psicologia generale il 30 novembre: percezione") == ("2026-11-30", "")
    assert read("exam by Nov 30, 2026") == ("2026-11-30", "")
    assert read("deadline 2026-12-01") == ("2026-12-01", "")
    assert read("scritto il 1° dicembre") == ("2026-12-01", "")
    assert read("entro il 15/01") == ("2027-01-15", "")          # no year: the next such day
    assert read("orale il 12/31") == ("2026-12-31", "")          # 31 cannot be a month
    assert read("la versione 3.5 del modello") == (None, "")     # a dot needs its year
    assert read("30 febbraio") == (None, "")
    day, why = read("March 3rd 2025 was the old exam")
    assert day is None and "has passed" in why


def test_the_verb_reader_finds_intentions_in_both_languages_and_skips_nouns():
    assert plans.read_intentions("Devo saperle spiegare e applicare.") == ["explain", "apply"]
    assert plans.read_intentions("I need to compare and evaluate the theories") == ["compare", "evaluate"]
    assert plans.read_intentions("ricordare le date") == ["recall"]
    assert plans.read_intentions("applicazioni web e valutazione") == []    # nouns name subjects


def test_the_reading_is_checked_against_the_vault_and_rescued_only_where_code_can_verify(vault, monkeypatch):
    from silica.kernel.learn import items

    def reply(text):
        monkeypatch.setattr(items, "_llm", lambda *a, **k: (text, "stop"))

    # no model: the date and the verbs are still read, Study stays empty and says why
    from silica.config import CONFIG

    monkeypatch.setattr(CONFIG, "model", "", raising=False)
    monkeypatch.setattr(items, "_llm", lambda *a, **k: ("", None))
    r = plans.read_sentence("Exam on 30 November 2099: I must explain softmax", today=None)
    assert r["by"] == "2099-11-30" and r["objectives"] == ["explain"] and r["scope"] == "" and r["model"] is False
    assert "No model" in r["said"][-1]
    # a listed subject that is no note: the list becomes a syllabus, whose resolver keeps gaps
    reply("KIND: notes\nTARGET: Softmax | Momentum di Nesterov\nONLY_IN: ML/\nNAME: Esame ML")
    r = plans.read_sentence("Esame di ML: softmax e momentum di Nesterov")
    assert r["scope"] == "steps" and r["target"] == "Softmax\nMomentum di Nesterov" and r["restrict"] == "ML/"
    assert r["name"] == "Esame ML" and "syllabus" in r["said"][0]
    # every name a note: notes, and a restriction is dropped as redundant
    reply("KIND: steps\nTARGET: Softmax | Chain rule\nONLY_IN: ML/\nNAME: Ripasso")
    assert plans.read_sentence("Ripasso di ML: Softmax e Chain rule")["scope"] == "notes"
    # a folder named by its last segment; a restriction the sentence does not name is a guess, dropped
    reply("KIND: notes\nTARGET: Deep\nONLY_IN: none\nNAME: Deep")
    r = plans.read_sentence("tutta la cartella Deep")
    assert (r["scope"], r["target"]) == ("folder", "ML/Deep/")
    reply("KIND: steps\nTARGET: softmax | transformers\nONLY_IN: Other/\nNAME: x")
    r = plans.read_sentence("softmax e transformers")
    assert r["restrict"] == "" and any("does not name Other/" in s for s in r["said"])
    # a folder that does not exist is dropped and said, never passed on
    reply("KIND: folder\nTARGET: Biology/\nONLY_IN: none\nNAME: Bio")
    r = plans.read_sentence("all of biology")
    assert r["scope"] == "" and "not a folder" in r["said"][-1]


def test_a_syllabus_line_says_how_it_matched_and_a_refused_line_is_a_gap(vault):
    vault.note("Other/Teoria della probabilita.md", "# Teoria della probabilita\n\nEventi.\n")
    res = plans.resolve_scope("steps", "[[Softmax]]\nChain rule\nTeoria della mente\nGradient")
    how = [(n["how"], n.get("doubt")) for n in res["tree"]]
    assert how[0] == ("by link", False) and how[1] == ("by title", False)
    # one shared content word of two ("della" is not counted): amber
    assert how[2] == ("1 of 2 words", True)
    assert how[3] == ("1 of 1 word", False)
    refused = plans.resolve_scope("steps", "[[Softmax]]\nChain rule\nTeoria della mente", refused=[2])
    assert refused["tree"][2]["gap"] and refused["tree"][2]["how"] == "refused" and refused["gaps"] == ["Teoria della mente"]
    assert "Other/Teoria della probabilita.md" not in refused["paths"]


def test_edit_plan_rewrites_the_fields_in_one_revertible_write_and_keeps_the_sessions(vault):
    vault.note("Plans/ML.md", PLAN)
    out = plans.edit_plan("Plans/ML.md", "Machine Learning, again", "folder", "ML/", by="2099-01-01",
                          objectives=["recall"], cards=[])
    assert out["run"]
    p = plans.read_plan("Plans/ML.md")
    assert p["name"] == "Machine Learning, again" and p["scope"] == "folder" and p["target"] == "ML/"
    assert p["objectives"] == ["recall"] and p["by"] == "2099-01-01" and p["created"] == "2026-09-01"
    assert p["sessions"][0]["session"] == "abc12345"            # the ledger's reading of the plan does not move


# ------------------------------------------------------------------ ADR-0081 review, 2026-10-08

def test_a_refused_match_survives_edit_and_reword_and_the_readers_own_text_stays(vault):
    vault.note("Other/Teoria della probabilita.md", "# Teoria della probabilita\n\nEventi.\n")
    out = plans.write_plan("Esame", "steps", "Softmax\nTeoria della mente", objectives=["explain"], cards=[], refused=[1])
    path = out["path"]
    assert plans.read_plan(path)["refused"] == ["Teoria della mente"]
    # the reader writes under the plan
    full = str(plans._vault() / path)
    text = vault.read(full).replace("## Objectives", "Ripassare prima la parte sulle reti.\n\n## Objectives")
    vault.write(full, text + "\n## Mie note\n- chiedere al prof\n")
    # Reword sends no refusals: the stored ones hold
    plans.edit_plan(path, "Esame", "steps", "Softmax\nTeoria della mente", objectives=["explain"], cards=[])
    p = plans.read_plan(path)
    assert p["refused"] == ["Teoria della mente"]
    assert plans.scope_of(p)["paths"] == ["ML/Softmax.md"]
    body = vault.read(full)
    assert "Ripassare prima la parte sulle reti." in body and "## Mie note\n- chiedere al prof" in body
    assert body.count("## Objectives") == 1 and body.count("## Steps") == 1
    # the edit form sends its own list, by line text or index: an undo clears it
    plans.edit_plan(path, "Esame", "steps", "Softmax\nTeoria della mente", objectives=["explain"], cards=[], refused=[])
    assert plans.read_plan(path)["refused"] == []


def test_a_note_whose_title_has_a_comma_is_studied_as_one_note(vault):
    vault.note("ML/Sensazione, percezione.md", "# Sensazione, percezione\n\nUno.\n")
    out = plans.write_plan("Senso", "notes", ["Sensazione, percezione", "Softmax"], objectives=["recall"], cards=[])
    p = plans.read_plan(out["path"])
    assert plans.scope_of(p)["paths"] == ["ML/Sensazione, percezione.md", "ML/Softmax.md"]
    # a plan written before, as a comma string, still reads
    vault.note("Plans/Old notes.md", "---\ntype: plan\nscope: notes\ntarget: Softmax, Chain rule\n---\n# Old notes\n")
    assert plans.scope_of(plans.read_plan("Plans/Old notes.md"))["paths"] == ["ML/Softmax.md", "ML/Chain rule.md"]


def test_the_date_reader_survives_a_leap_day_and_leaves_problem_set_alone():
    from datetime import date

    assert plans.read_date("esame il 29 febbraio", date(2028, 3, 10)) == (None, "")
    assert plans.read_date("esame il 29 febbraio", date(2027, 10, 1)) == ("2028-02-29", "")
    assert plans.read_date("problem set 3 is due", date(2026, 10, 8)) == (None, "")
    assert plans.read_date("on May 3", date(2026, 10, 8)) == ("2027-05-03", "")
    assert plans.read_date("entro il 3 set", date(2026, 8, 1)) == ("2026-09-03", "")


def test_an_empty_reply_is_not_reported_as_no_model(vault, monkeypatch):
    from silica.config import CONFIG
    from silica.kernel.learn import items

    monkeypatch.setattr(CONFIG, "model", "local/qwen", raising=False)
    monkeypatch.setattr(items, "_llm", lambda *a, **k: ("", "stop"))
    r = plans.read_sentence("softmax e chain rule")
    assert r["model"] is False and "returned nothing" in r["said"][-1]


def test_a_path_outside_the_vault_reads_as_no_note(vault, tmp_path):
    from silica.kernel.learn import items

    secret = tmp_path / "outside.md"
    secret.write_text("# secret\n", encoding="utf-8")
    assert items.read_note(str(secret)) == "" and items.read_note("../outside.md") == ""
    assert items.read_note("ML/Softmax.md").startswith("---")


def test_planned_paths_is_what_the_active_plans_cover(vault):
    """The line Learn's Next draws once a plan exists (ADR-0111 point 4)."""
    assert plans.planned_paths() is None   # no plan: no line
    vault.note("Bio/cell.md", "# Cell\n\nThe membrane is a lipid bilayer.\n")
    vault.note("Bio/Lab.md", "---\nevent_start: 2026-10-20T10:00\n---\n\n# Lab\n")
    vault.note("Ref/memo.md", "# Memo\n\nKept for reference.\n")
    vault.note("Plans/Exam.md", "---\ntype: plan\nscope: folder\ntarget: Bio/\n---\n\n# Exam\n")
    assert plans.planned_paths() == {"Bio/cell.md"}
    vault.note("Plans/Exam.md", "---\ntype: plan\nscope: folder\ntarget: Bio/\nstatus: completed\n---\n\n# Exam\n")
    assert plans.planned_paths() is None   # a completed plan draws no line
