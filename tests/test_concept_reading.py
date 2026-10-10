# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading a concept through the notes: two or more concepts selected together
light the notes that carry ALL of them, and the work panel quotes the lines
that carry each (the InfraNodus "read the passages" gesture, on the store and
the driver's search Silica already has)."""
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"
NOTES_JS = WEB / "app-notes.js"
WORK_JS = WEB / "work.js"


@pytest.fixture
def client(tmp_vault):
    _fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.ui.web import server

    server._reset_session()
    return TestClient(server.app), tmp_vault


def _concepts_stub(term="", note="", k=10):
    notes = {
        "feedback": [{"path": "Both", "count": 3}, {"path": "OnlyFeedback", "count": 1}],
        "stability": [{"path": "Both", "count": 2}, {"path": "OnlyStability", "count": 4}],
        "vapour": [],
    }[term]
    return {"concept": term.capitalize(), "notes": notes}


def test_concepts_endpoint_intersects_the_terms_and_quotes_their_lines(client, monkeypatch):
    api, vault = client
    vault.note("Both.md", "# Both\n\nFeedback keeps the loop honest.\n\nStability follows from bounded feedback.\n")
    vault.note("OnlyFeedback.md", "# F\n\nFeedback alone.\n")
    vault.note("OnlyStability.md", "# S\n\nStability alone.\n")
    monkeypatch.setattr("silica.tools.graph.silica_concepts", _concepts_stub)

    d = api.get("/concepts", params={"terms": "feedback,stability"}).json()
    assert [(t["term"], t["concept"], t["notes"]) for t in d["terms"]] == [
        ("feedback", "Feedback", 2), ("stability", "Stability", 2)]
    assert [n["path"] for n in d["notes"]] == ["Both.md"]
    lines = d["notes"][0]["passages"]
    by_text = {p["text"]: p["terms"] for p in lines}
    assert by_text["Feedback keeps the loop honest."] == ["feedback"]
    assert sorted(by_text["Stability follows from bounded feedback."]) == ["feedback", "stability"]
    assert all(p["line"] > 0 for p in lines) and len(lines) == 2, "one entry per line, not per term"


def test_one_term_is_the_old_concept_lookup_and_an_empty_intersection_says_so(client, monkeypatch):
    api, vault = client
    vault.note("Both.md", "Feedback and stability.\n")
    vault.note("OnlyFeedback.md", "Feedback.\n")
    monkeypatch.setattr("silica.tools.graph.silica_concepts", _concepts_stub)
    d = api.get("/concepts", params={"terms": "feedback"}).json()
    assert sorted(n["path"] for n in d["notes"]) == ["Both.md", "OnlyFeedback.md"]
    d = api.get("/concepts", params={"terms": "feedback,vapour"}).json()
    assert d["notes"] == [] and [t["notes"] for t in d["terms"]] == [2, 0]


def test_the_selection_is_a_set_and_the_panel_reads_it():
    notes, work = NOTES_JS.read_text(), WORK_JS.read_text()
    assert re.search(r"let litConcepts = \[\];", notes), "the active concepts, an intersection"
    body = notes[notes.index("async function lightConcept("):notes.index("window.lightConcept = lightConcept")]
    assert '"/concepts?terms="' in body and 'new CustomEvent("silica:concepts"' in body
    assert "focusGraphNodes(" in body
    assert 'addEventListener("silica:concepts"' in work and "function renderConcepts()" in work
    # a pill shows whether its concept is in the selection, whichever panel drew it
    assert "window.isConceptLit" in notes and "window.isConceptLit" in work
