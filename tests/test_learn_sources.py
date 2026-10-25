# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A plan read against the sources that teach it (ADR-0131): collections and
their roles, steps made of outline rows, the reference's hours, the
assessment, the depth, the guide."""
from __future__ import annotations

from datetime import date

import pytest

import silica.tools.notes  # noqa: F401 — the write gate registers on import
from silica.kernel.learn import plans, sources

BOOK = "/x/Pattern Learning Methods (Ann Smith) (Z-Library).pdf"

SYLLABUS = """# Corso di apprendimento

| TESTI CONSIGLIATI | Ann Smith, Pattern Learning Methods, Springer |
| --- | --- |
| VALUTAZIONE | Progetto individuale e prova orale. Il progetto viene assegnato con almeno una settimana di anticipo rispetto all'orale. |

PROGRAMMA

| ORE | Lezioni |
| --- | --- |
| 4 | Modelli lineari: regressione lineare, ridge regression, lasso regression |
| 6 | Metodi kernel: kernel trick, teorema di Mercer |
| 4 | Macchine a vettori di supporto: variabili slack, classificazione one class |
| 2 | Regolarizzazione: dropout, early stopping |
| 4 | Applicazione dei modelli kernel a problemi di classificazione su dataset reali |
| 4 | Applicazione dei modelli lineari a problemi di classificazione su dataset reali |
"""

LECTURES = {
    "L1": ["Regressione lineare", "Minimi quadrati", "Ridge regression", "Lasso regression"],
    "L2": ["Definizione di kernel", "Kernel trick", "Teorema di Mercer", "Forma duale"],
    "L3": ["Iperpiano ottimo", "Variabili slack", "Classificazione one-class", "Dualità SVDD"],
    "L4": ["Esercitazione con scikit"],
}
BOOK_SEGMENTS = {
    "Book-01-basics": ["Algebra di base", "Regressione lineare", "Dropout"],
    "Book-02-nets": ["Reti convolutive", "Pooling"],
}
SUMMARY = {
    "Regressione lineare": "la regressione lineare stima i pesi con i minimi quadrati",
    "Minimi quadrati": "la soluzione chiusa del gradiente nullo",
    "Ridge regression": "la ridge regression aggiunge una penalità L2",
    "Lasso regression": "la lasso regression aggiunge una penalità L1",
    "Definizione di kernel": "una funzione simmetrica semidefinita positiva",
    "Kernel trick": "il kernel trick sostituisce il prodotto interno",
    "Teorema di Mercer": "il teorema di Mercer caratterizza i kernel validi",
    "Forma duale": "la forma duale esprime il modello con i soli prodotti interni",
    "Iperpiano ottimo": "l'iperpiano di margine massimo",
    "Variabili slack": "le variabili slack misurano la violazione del margine",
    "Classificazione one-class": "la classificazione one class riconosce una sola classe",
    "Dualità SVDD": "il duale della sfera minima",
    "Esercitazione con scikit": "un notebook di esempio",
    "Algebra di base": "vettori e matrici",
    "Dropout": "il dropout spegne unità a caso durante l'addestramento",
    "Reti convolutive": "convoluzione e condivisione dei pesi",
    "Pooling": "riassume una regione dell'uscita",
}


@pytest.fixture
def course(tmp_vault, tmp_path, monkeypatch):
    import silica.kernel.report.quiz as quiz
    from silica.kernel.write import undo_journal

    monkeypatch.setattr(quiz, "log_path", lambda: tmp_path / "quiz.jsonl")
    monkeypatch.setattr(undo_journal, "_DEFAULT_JOURNAL_PATH", tmp_path / "undo.db", raising=False)
    sources._MEMO.clear()
    sources._VOCAB.clear()
    made: dict[str, list[str]] = {}

    def spine(stem: str, rows: list[str], leaf: str, source_file: str, text: str) -> None:
        tmp_vault.note(f"{leaf}.md", f'---\nsource_file: "{source_file}"\ntype: Source\n---\n\n{text}\n')
        body = "\n".join(f"{i}. [[{r}]]: {SUMMARY[r]}" for i, r in enumerate(rows, 1))
        tmp_vault.note(f"Course/{stem}.md", f"---\nsection: (outline)\ntype: Note\n---\n\n# {stem}\n\n{body}\n\n"
                                            f"## Sources\n[[{leaf}]]\n")
        for r in rows:
            made.setdefault(r, []).append(leaf)

    for stem, rows in LECTURES.items():
        spine(stem, rows, f"sources/course/Slides/{stem}", f"/x/Slides/{stem}.pdf",
              " ".join(SUMMARY[r] for r in rows) + (" early stopping" if stem == "L3" else ""))
    for stem, rows in BOOK_SEGMENTS.items():
        spine(stem, rows, f"sources/course/Book/{stem}", BOOK, " ".join(SUMMARY[r] for r in rows))
    for title, leaves in made.items():
        links = "\n".join(f"[[{leaf}]]" for leaf in leaves)
        tmp_vault.note(f"Course/{title}.md", f"---\ntype: Note\n---\n\n# {title}\n\n{SUMMARY[title]}.\n\n## Sources\n{links}\n")
    tmp_vault.note("Course/lab_kernel.md", "---\ntype: Note\n---\n\n```python\nclf = SVC(kernel='rbf')\n```\n\n"
                                           "## Concepts practised\n- [[Kernel trick]]: usa un kernel rbf\n")
    tmp_vault.note("Course/lab_linear.md", "---\ntype: Note\n---\n\n```python\nreg = LinearRegression()\n```\n\n"
                                           "## Concepts practised\n- [[Regressione lineare]]: modelli lineari\n")
    tmp_vault.note("Course/Syllabus.md", "---\ntype: Note\n---\n\n" + SYLLABUS)
    return tmp_vault


def _notes():
    from silica.kernel.report.learner import _notes_meta

    return _notes_meta(plans._vault())


def _stems(paths) -> list[str]:
    from pathlib import Path

    return [Path(p).stem for p in paths]


def test_spines_group_into_a_book_by_its_file_and_a_series_by_its_folder(course):
    notes = _notes()
    colls = {c["label"]: c for c in sources.collections(notes, set(notes))}
    assert set(colls) == {"Pattern Learning Methods (Ann Smith)", "Slides"}
    assert colls["Slides"]["kind"] == "series" and _stems(colls["Slides"]["spines"]) == ["L1", "L2", "L3", "L4"]
    assert colls["Pattern Learning Methods (Ann Smith)"]["kind"] == "book"
    assert [r["title"] for r in colls["Slides"]["rows"]][:4] == LECTURES["L1"]


def test_a_role_is_read_for_the_reference_and_the_readers_choice_wins(course):
    notes = _notes()
    colls = sources.collections(notes, set(notes))
    steps = [[sources.roots(t) for t in plans._topics(line) or [line]]
             for line in plans.reference_lines(SYLLABUS)]
    by_label = lambda roles: {c["label"]: roles[c["key"]]["role"] for c in colls}  # noqa: E731
    assert by_label(sources.roles(colls, SYLLABUS, "Syllabus", steps)) == {
        "Pattern Learning Methods (Ann Smith)": "reference", "Slides": "material"}
    flipped = sources.roles(colls, SYLLABUS, "Syllabus", steps,
                            {"Pattern Learning Methods (Ann Smith)": "material", "Slides": "reference"})
    assert by_label(flipped) == {"Pattern Learning Methods (Ann Smith)": "material", "Slides": "reference"}
    assert all(v["why"] == "you set it" for v in flipped.values())


def test_a_book_named_in_the_references_title_is_the_material(course):
    notes = _notes()
    colls = sources.collections(notes, set(notes))
    roles = sources.roles(colls, "", "Studying Pattern Learning Methods", [[{"dropout"}]])
    book = next(c for c in colls if c["kind"] == "book")
    assert roles[book["key"]] == {"role": "material", "why": "named in the reference's title", "confirm": False}


def test_order_picks_the_collection_that_follows_the_reference():
    a = {"key": "a", "label": "A", "spines": [f"a{i}" for i in range(5)], "rows": []}
    b = {"key": "b", "label": "B", "spines": [f"b{i}" for i in range(5)], "rows": []}
    for c, order in ((a, [0, 1, 2, 3, 4]), (b, [4, 0, 3, 1, 2])):
        for pos, step in zip(c["spines"], order):
            c["rows"].append({"spine": pos, "roots": {f"t{step}"}})
    steps = [[{f"t{i}"}] for i in range(5)]
    roles = sources.roles([a, b], "", "", steps)
    assert roles["a"]["role"] == "material" and roles["a"]["why"].startswith("follows the reference's order")
    assert roles["b"]["role"] == "reference"


def test_steps_take_the_materials_rows_and_a_book_note_only_where_the_material_is_silent(course):
    res = plans.resolve_scope("steps", "Syllabus")
    tree = {n["node"]: n for n in res["tree"]}
    assert _stems(tree["Modelli lineari"]["leaves"]) == LECTURES["L1"]
    # the whole lecture: "Definizione di kernel" and "Forma duale" are named by no topic
    assert _stems(tree["Metodi kernel"]["leaves"]) == LECTURES["L2"]
    assert _stems(tree["Macchine a vettori di supporto"]["leaves"]) == LECTURES["L3"]
    reg = tree["Regolarizzazione"]
    # dropout only the book teaches: kept and marked; early stopping only a
    # deck's text holds: the step links that deck's outline
    assert "Dropout" in _stems(reg["leaves"]) and _stems(reg["ref"]) == ["Dropout"]
    assert "L3" in _stems(reg["leaves"]) and not reg.get("missing")
    assert {s["label"]: s["role"] for s in res["sources"]} == {
        "Pattern Learning Methods (Ann Smith)": "reference", "Slides": "material"}


def test_an_activity_line_meets_the_practice_its_distinctive_words_weigh_in(course):
    res = plans.resolve_scope("steps", "Syllabus")
    labs = {("kernel" if "kernel" in n["node"] else "lineari"): _stems(n.get("practice") or [])
            for n in res["tree"] if n["node"].startswith("Applicazione")}
    assert labs == {"kernel": ["lab_kernel"], "lineari": ["lab_linear"]}


def test_no_note_sits_in_two_steps_but_practice(course):
    from collections import Counter

    res = plans.resolve_scope("steps", "Syllabus")
    practice = {"lab_kernel", "lab_linear"}
    seen = Counter(s for n in res["tree"] for s in _stems(n.get("leaves") or []) if s not in practice)
    assert max(seen.values()) == 1


def test_a_glued_line_is_split_into_the_vaults_words():
    vocab = {"richiami", "algebra", "lineare", "introduzione", "corso", "di", "al"}
    assert sources.split_glued("richiamidialgebralineare", vocab) == "richiami di algebra lineare"
    assert sources.split_glued("Introduzionealcorso", vocab) == "introduzione al corso"
    assert sources.split_glued("xyzzyplughwordless", vocab) is None


def test_reference_rows_keep_the_hours_and_a_guide_is_read_from_its_plan_table():
    rows = plans.reference_rows(SYLLABUS)
    assert [(r["hours"], r.get("unit")) for r in rows][:2] == [(4.0, "h"), (6.0, "h")]
    guide = ("# Guida\n\n- una nota qualunque\n\n| # | lezioni | cosa | profondità |\n|---|---|---|---|\n"
             "| 1 | L1 | regressione, ridge, lasso | a fondo |\n| 2 | L2 | kernel, Mercer | bene; una lettura RKHS |\n\n"
             "| ripetizione | dove |\n|---|---|\n| capacità e validazione | L6 |\n")
    got = plans.reference_rows(guide)
    assert [(r["text"], r["depth"]) for r in got] == [("regressione, ridge, lasso", "deep"), ("kernel, Mercer", "well")]


def test_the_assessment_is_read_with_the_projects_lead_time():
    assert plans.assessment_of(SYLLABUS) == [{"part": "project", "days": 7}, {"part": "oral", "days": None}]
    assert plans.parse_assessment(plans.assessment_text([{"part": "project", "days": 7}])) == [{"part": "project", "days": 7}]
    assert plans.assessment_text([]) == "none" and plans.parse_assessment("none") == []


def test_the_calendar_weighs_the_references_hours_and_keeps_the_projects_days():
    steps = [{"text": "A: [[a]]", "notes": ["a"], "hours": 6.0, "unit": "h"},
             {"text": "B: [[b]], [[c]], [[d]]", "notes": ["b", "c", "d"], "hours": 2.0, "unit": "h"}]
    plan = {"by": "2026-11-10", "steps": steps, "hours": 5.0,
            "assessment": [{"part": "project", "days": 7}, {"part": "oral", "days": None}]}
    rows = plans.day_by_day(plan, today=date(2026, 10, 25))
    study = [r for r in rows if any(label in ("A", "B") for label, _n in r["steps"])]
    assert study[-1]["day"] == "2026-11-02"   # 16 days, 7 of them the project's
    assert sum(1 for r in study if any(label == "A" for label, _n in r["steps"])) > \
        sum(1 for r in study if any(label == "B" for label, _n in r["steps"]))
    assert rows[9]["steps"][0][0].startswith("Project")
    assert any(label.startswith("Rehearse the oral") for r in rows if r["day"] == "2026-11-09" for label, _n in r["steps"])
    assert sum(h for r in study for h in r["hours"].values()) == pytest.approx(45.0)
    say = plans.budget(plan, date(2026, 10, 25))["say"]
    assert "8 h" in say and "45 h" in say and "Fewer" not in say
    bare = {"by": "2026-11-10", "steps": [{"text": "A", "notes": ["a"]}]}
    assert plans.budget(bare, date(2026, 10, 25))["say"] == "Weighted by notes: the reference gives no hours."


def test_step_lines_carry_and_shed_their_markers():
    st = plans.parse_steps("## Steps\n- [ ] Modelli lineari: [[A]], [[B]] (ref) · not in the vault: x · 8 h · deep\n"
                           "- [ ] Labs: [[C]] · weight 3\n")
    assert (st[0]["hours"], st[0]["unit"], st[0]["depth"]) == (8.0, "h", "deep")
    assert (st[1]["hours"], st[1]["unit"]) == (3.0, "")
    assert [plans._step_label(s) for s in st] == ["Modelli lineari", "Labs"]


def test_the_plan_note_stores_roles_hours_and_assessment_and_an_edit_keeps_them(course, monkeypatch):
    from silica.kernel.learn import objectives as obj

    monkeypatch.setattr(obj, "rewrite", lambda cards: cards)
    out = plans.write_plan("Esame", "steps", "Syllabus", by="2026-11-10", objectives=[], hours=5)
    text = plans._read(out["path"])
    assert "materials: Slides" in text and "references: Pattern Learning Methods (Ann Smith)" in text
    assert "hours: '5'" in text or "hours: 5" in text
    assert "assessment: project 7 | oral" in text and "## Assessment" in text
    assert "Sources: Pattern Learning Methods (Ann Smith), a reference (named among the reference's texts)" in text
    assert "[[Dropout]] (ref)" in text and "· 6 h" in text
    plan = plans.read_plan(out["path"])
    assert plan["hours"] == 5.0 and plan["assessment"][0] == {"part": "project", "days": 7}
    plans.edit_plan(out["path"], "Esame", "steps", "Syllabus", by="2026-11-10", objectives=[],
                    declared={"Pattern Learning Methods (Ann Smith)": "material", "Slides": "reference"})
    text = plans._read(out["path"])
    assert "materials: Pattern Learning Methods (Ann Smith)" in text and "references: Slides" in text
    assert "assessment: project 7 | oral" in text and ("hours: '5'" in text or "hours: 5" in text)
    assert "(you set it)" in text
    plans.edit_plan(out["path"], "Esame", "steps", "Syllabus", by="2026-11-10", objectives=[], assessment="none")
    text = plans._read(out["path"])
    assert "assessment: none" in text and "## Assessment" not in text
    assert "materials: Pattern Learning Methods (Ann Smith)" in text   # the roles the reader set stay
    assert "(as the plan says)" in text and "(you set it)" not in text


def test_the_pace_counts_the_study_days_before_the_projects():
    from datetime import date as real_date, timedelta

    today = real_date.today()
    plan = {"by": (today + timedelta(days=16)).isoformat(), "assessment": [{"part": "project", "days": 7}],
            "steps": [{"text": "A", "notes": ["a"], "hours": 4.0, "unit": "h"}]}
    pace = plans.pace_of(plan, below=90)
    assert pace["per_day"] == 25        # 225 answers over 9 study days, not 14
    assert "then 7 days for the project" in pace["say"]


def test_lecture_gaps_read_the_material_however_its_decks_are_named(course, monkeypatch):
    from silica.kernel.learn import objectives as obj

    monkeypatch.setattr(obj, "rewrite", lambda cards: cards)
    out = plans.write_plan("Gaps", "steps", "Syllabus", by="2026-11-10", objectives=[])
    gaps, decks = plans.lecture_gaps(plans.read_plan(out["path"]))
    assert decks == 4 and "dropout" in gaps and "kernel trick" not in gaps


def test_depth_comes_from_a_guide_and_sets_the_proposed_objective(course, monkeypatch):
    from silica.kernel.learn import objectives as obj

    monkeypatch.setattr(obj, "rewrite", lambda cards: cards)
    course.note("Course/Guida.md", "# Guida\n\n| # | cosa | profondità |\n|---|---|---|\n"
                                   "| 1 | regressione lineare, ridge regression, lasso regression | a fondo |\n"
                                   "| 2 | kernel trick, teorema di Mercer | una lettura |\n")
    assert plans.guide_candidates("Course") == ["Course/Guida.md"]
    out = plans.write_plan("Guided", "steps", "Syllabus", by="2026-11-10", objectives=[], guide="Guida")
    steps = {plans._step_label(s): s for s in plans.read_plan(out["path"])["steps"]}
    assert steps["Modelli lineari"]["depth"] == "deep" and steps["Metodi kernel"]["depth"] == "read"
    cards = {c["node"]: c["intention"] for c in plans.read_plan(out["path"])["cards"]}
    assert cards["Modelli lineari"] == "explain" and cards["Metodi kernel"] == "recall"


def test_align_gives_each_row_to_one_step_in_order():
    coll = {"rows": [{}] * 6}
    steps = [{"i": 0, "scores": {0: 1.0, 4: 0.5}}, {"i": 1, "scores": {1: 1.0, 2: 1.0}}, {"i": 2, "scores": {3: 1.0, 5: 1.0}}]
    assert sources.align(steps, coll) == {0: [0], 1: [1, 2], 2: [3, 5]}


def test_the_form_previews_sources_and_the_budget_route_answers(course):
    from fastapi.testclient import TestClient

    from silica.ui.web import learn_routes, server

    server._reset_session()
    learn_routes._reset()
    client = TestClient(server.app)
    out = client.post("/learn/plan", json={"scope": "", "target": "", "reference": "Syllabus", "preview": True},
                      headers={"Origin": "http://testserver"}).json()
    assert {s["label"]: s["role"] for s in out["sources"]}["Slides"] == "material"
    assert out["assessment"][0]["part"] == "project"
    b = client.post("/learn/plan/budget", headers={"Origin": "http://testserver"},
                    json={"by": "2099-01-20", "hours": 2, "assessment": [], "steps": [{"hours": 4, "unit": "h", "notes": 1}]}).json()
    assert "The reference's own hours: 4 h" in b["say"]


def test_a_reference_the_material_does_not_follow_gives_the_same_steps_and_says_so(course):
    """ADR-0131 amendment: aligned in its own order, a shuffled syllabus gave
    one block 89 notes and another 2 with nothing said; matched by words, the
    steps do not depend on the list's order, and the plan says how."""
    lines = [r["text"] for r in plans.reference_rows(SYLLABUS)]
    one = plans.resolve_scope("steps", "\n".join(lines) + "\n")
    two = plans.resolve_scope("steps", "\n".join(reversed(lines)) + "\n")
    steps = lambda res: {n["node"]: set(n.get("leaves") or []) for n in res["tree"]}  # noqa: E731
    assert steps(one) == steps(two)
    assert one["order_note"].startswith("Too few of the material's sources meet the reference")


def test_best_steps_gives_a_row_to_the_step_holding_it_most_whatever_the_order():
    coll = {"rows": [{"spine": "a"}, {"spine": "a"}, {"spine": "b"}]}
    steps = [{"i": 0, "scores": {0: 1.0, 2: 0.5}}, {"i": 1, "scores": {1: 1.0, 2: 1.0}}]
    names = {0: "Zeta", 1: "Alfa"}
    assert sources.best_steps(steps, coll, names) == {0: [0], 1: [1, 2]}
    assert sources.best_steps(list(reversed(steps)), coll, names) == {0: [0], 1: [1, 2]}


def _even_plan(created: str, by: str) -> dict:
    return {"by": by, "created": created, "hours": 4.0,
            "steps": [{"text": f"S{i}: [[n{i}]]", "notes": [f"n{i}"], "hours": 4.0, "unit": "h"} for i in range(4)]}


def test_the_calendar_is_of_the_steps_left():
    plan = _even_plan("2026-10-18", "2026-11-03")
    rows = plans.day_by_day(plan, today=date(2026, 10, 25), passed=[True, False, False, False])
    labels = [label for r in rows for label, _n in r["steps"]]
    assert "S0" not in labels and labels[0] == "S1"
    plan["steps"][1]["checked"] = True        # a checked box counts as done too
    rows = plans.day_by_day(plan, today=date(2026, 10, 25), passed=[True, False, False, False])
    assert [label for r in rows for label, _n in r["steps"]][0] == "S2"
    say = plans.budget(plan, date(2026, 10, 25), [True, False, False, False])["say"]
    assert "16 h (8 h in the steps left)" in say


def test_today_says_how_far_behind_the_calendar_drawn_when_the_plan_was_made():
    # 16 days, 2 of review: 14 study days, four steps of 3.5 days; the
    # calendar made on the 18th has S2 on the 25th
    plan = _even_plan("2026-10-18", "2026-11-03")
    t = plans.today_line(plan, [False] * 4, today=date(2026, 10, 25))
    assert t["behind"] == 2 and t["expected"] == "S2" and t["first"] == "S0"
    assert "2 steps behind the calendar drawn when the plan was made: it had S2 for today" in t["say"]
    assert t["say"].startswith("Today: S0")
    on_time = plans.today_line(plan, [True, True, False, False], today=date(2026, 10, 25))
    assert on_time["behind"] == 0 and "behind" not in on_time["say"]
    fresh = plans.today_line({**plan, "created": "2026-10-25"}, [False] * 4, today=date(2026, 10, 25))
    assert fresh["behind"] is None


def test_a_stem_two_notes_share_resolves_as_the_walk_did():
    """The name index keeps the walk's answer: the exact path first, then the
    first note in the notes' order holding the stem."""
    notes = {"B/Regressione lineare.md": {}, "A/Sub/Regressione lineare.md": {}, "C/Altro.md": {}}
    assert plans.resolve_title("Regressione lineare", notes) == "B/Regressione lineare.md"
    assert plans.resolve_title("A/Sub/Regressione lineare", notes) == "A/Sub/Regressione lineare.md"
    assert plans.resolve_title("regressione LINEARE", notes) == "B/Regressione lineare.md"
    notes["D/Nuova.md"] = {}
    assert plans.resolve_title("Nuova", notes) == "D/Nuova.md"     # a grown dict rebuilds the index
    assert plans.resolve_title("Nessuna", notes) is None


def test_topic_vectors_survive_a_restart_and_never_cross_models(tmp_vault):
    calls = []

    class Emb:
        model = "m1"

        def embed(self, texts):
            calls.extend(texts)
            return [[float(len(t)), 1.0] for t in texts]

    plans._VEC_MEMO.clear()
    assert plans._embed(["kernel trick"], Emb()) == [[12.0, 1.0]]
    plans._VEC_MEMO.clear()                       # a new process: the disk answers
    assert plans._embed(["kernel trick"], Emb()) == [[12.0, 1.0]] and calls == ["kernel trick"]

    class Other(Emb):
        model = "m2"

    plans._embed(["kernel trick"], Other())
    assert calls == ["kernel trick", "kernel trick"]   # another model embeds again
    plans._VEC_MEMO.clear()


def test_a_close_decision_is_to_confirm_and_no_plan_key_is_written_without_the_form(course, monkeypatch):
    from silica.kernel.learn import objectives as obj

    a = {"key": "a", "label": "A", "spines": [f"a{i}" for i in range(5)], "rows": []}
    b = {"key": "b", "label": "B", "spines": [f"b{i}" for i in range(5)], "rows": []}
    read = {"a": {"order": 0.7, "focus": 0.5, "matched": 5}, "b": {"order": 0.45, "focus": 0.4, "matched": 5}}
    roles = sources.roles([a, b], "", "", [[{"t"}]], read=read)
    assert roles["a"]["role"] == "material" and roles["a"]["confirm"] and roles["a"]["why"].endswith("; to confirm")
    read["b"]["order"] = 0.2                     # a clear margin: decided
    assert sources.roles([a, b], "", "", [[{"t"}]], read=read)["a"]["confirm"] is False
    # a plan made without the form writes no role it was not sure of
    monkeypatch.setattr(obj, "rewrite", lambda cards: cards)
    monkeypatch.setattr(sources, "FOCUS_MARGIN", 0.01)
    monkeypatch.setattr(sources, "CONFIRM_FACTOR", 1e9)   # every focus win is close
    lines = "\n".join(r["text"] for r in plans.reference_rows(SYLLABUS)) + "\n"
    out = plans.write_plan("Unsure", "steps", lines, by="2026-11-10", objectives=[])
    text = plans._read(out["path"])
    assert "materials:" not in text and "to confirm" in text
    out = plans.write_plan("Sure", "steps", lines, by="2026-11-10", objectives=[],
                           declared={"Slides": "material", "Pattern Learning Methods (Ann Smith)": "reference"},
                           declared_why="confirmed in the form")
    text = plans._read(out["path"])
    assert "materials: Slides" in text and "(confirmed in the form)" in text


def test_a_topic_no_source_meets_offers_the_steps_notes_and_keeps_the_readers_answer(course, monkeypatch):
    from silica.kernel.learn import objectives as obj

    monkeypatch.setattr(obj, "rewrite", lambda cards: cards)
    lines = [r["text"] for r in plans.reference_rows(SYLLABUS)]
    lines[1] = "Metodi kernel: kernel trick, teorema di Mercer, spazi di funzioni speciali"
    target = "\n".join(lines) + "\n"
    res = plans.resolve_scope("steps", target)
    node = next(n for n in res["tree"] if n["node"] == "Metodi kernel")
    assert node["missing"] == ["spazi di funzioni speciali"]
    assert set(node["candidates"]["spazi di funzioni speciali"]) <= set(LECTURES["L2"])
    said = {"spazi di funzioni speciali": "Definizione di kernel"}
    res = plans.resolve_scope("steps", target, matched=said)
    node = next(n for n in res["tree"] if n["node"] == "Metodi kernel")
    assert not node.get("missing") and node["matched"] == said
    out = plans.write_plan("Answered", "steps", target, by="2026-11-10", objectives=[], matched=said)
    assert "matched: spazi di funzioni speciali -> Definizione di kernel" in plans._read(out["path"])
    plans.edit_plan(out["path"], "Answered", "steps", target, by="2026-11-10", objectives=[])
    assert plans.read_plan(out["path"])["matched"] == said        # Edit plan keeps the answer


def test_a_topic_met_by_meaning_is_said_on_the_step_line_and_shed_by_its_label():
    node = {"node": "Reti", "by_meaning": [("percettrone multistrato", "Rete di percettroni")], "hours": 6.0, "unit": "h"}
    tail = plans._marks(node)
    assert tail == " · by meaning, percettrone multistrato ≈ Rete di percettroni · 6 h"
    step = plans.parse_steps("## Steps\n- [ ] Reti: [[A]]" + tail + "\n")[0]
    assert plans._step_label({"text": "[[A]]" + tail, "notes": ["A"]}) == "A" and step["hours"] == 6.0
    assert step["notes"] == ["A"]                 # no title in the tail becomes a note of the step
