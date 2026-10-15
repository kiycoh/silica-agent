"""silica_read_note(query=): a long note narrowed to the passages the
question's words are densest in, with the line ranges left out, when the
typed decision on the user's request says "part" (ADR-0053, read_scope.py).
Anything else reads the whole note. The decision itself is tested in
test_read_scope.py; here it is a switch."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from silica.agent import read_scope
from silica.driver.base import NoteContent, NoteRef
from silica.kernel.recall import rerank
from silica.kernel.write import provenance
from silica.tools import atomic

FILLER = "\n".join(f"riga {i} della lezione sul metodo knn e le distanze" for i in range(300))
TAIL = "\n".join(f"coda {i} con alberi e foreste casuali" for i in range(100))
LONG = f"# Lezione 32\n\n{FILLER}\n\n## Boosting\n\nIl boosting combina classificatori deboli.\n\n{TAIL}\n"


@pytest.fixture
def note(monkeypatch):
    """A fake driver serving one note; see test_search_stale_flags._fake_driver
    for why the module name is patched and never the proxy."""
    served = {}

    def read_note(name):
        return NoteContent(ref=NoteRef(name=name, path="F/L32.md"), content=served["body"])

    monkeypatch.setattr(atomic, "DRIVER", SimpleNamespace(read_note=read_note))
    monkeypatch.setattr(provenance, "drift_map", lambda **k: {})
    monkeypatch.setattr(rerank, "window_weights", lambda q: {})
    monkeypatch.setattr(read_scope, "wants_part", lambda name, query: True)

    def put(body):
        served["body"] = body
    return put


def test_an_empty_query_reads_the_whole_note(note):
    note(LONG)
    assert atomic.silica_read_note("Lezione 32") == LONG


def test_a_short_note_is_served_whole_whatever_the_query(note):
    short = "# Breve\n\nIl boosting in due righe.\n"
    note(short)
    assert atomic.silica_read_note("Breve", query="boosting") == short


def test_a_long_note_comes_back_as_the_passage_the_query_names(note):
    note(LONG)
    out = atomic.silica_read_note("Lezione 32", query="boosting")
    assert "Il boosting combina classificatori deboli." in out
    assert "riga 0 della lezione" not in out
    assert len(out) < len(LONG) // 2
    assert "Not read: lines 1-" in out
    assert 'silica_read(path="F/L32.md"' in out


def test_the_notes_own_name_does_not_steer_the_windows(note):
    """"Lezione 32" in the query put a window on the first slide dense in
    "lezione" (probe 2026-09-30): the name locates the note, not the passage."""
    note(LONG)
    out = atomic.silica_read_note("Lezione 32", query="Lezione 32 boosting")
    assert "Il boosting combina" in out
    assert "riga 0 della lezione" not in out


def test_a_query_that_is_only_the_name_reads_the_whole_note(note):
    note(LONG)
    assert atomic.silica_read_note("Lezione 32", query="lezione 32") == LONG


def test_the_drift_banner_survives_a_narrowed_read(note, monkeypatch):
    note(LONG)
    monkeypatch.setattr(provenance, "drift_map", lambda **k: {"F/L32.md": "lec.md"})
    out = atomic.silica_read_note("Lezione 32", query="boosting")
    assert out.startswith("> [stale] ")
    assert "Il boosting combina" in out


def test_a_query_reads_whole_unless_the_decision_says_part(note, monkeypatch):
    """The v2 probe (2026-09-30): asked for a summary, the model wrote "devo
    leggere l'intera nota" and still filled `query` in 12 of 16 reads. A query
    alone no longer narrows; a "part" from the decision does."""
    note(LONG)
    for verdict in (False, None):
        monkeypatch.setattr(read_scope, "wants_part", lambda name, query, v=verdict: v)
        assert atomic.silica_read_note("Lezione 32", query="riassunto punti chiave") == LONG


def test_the_model_sees_no_scope_to_choose():
    """v3 (2026-10-01) gave the model `scope` and written rules: it chose
    "part" on 9 of 137 reads, the targeted questions included, and the saving
    went with the errors. The decision reads the request instead (ADR-0053)."""
    from silica.tools import TOOLS

    params = TOOLS["silica_read_note"].json_schema()["function"]["parameters"]["properties"]
    assert list(params) == ["name", "query", "vault"]


def test_a_window_holding_a_heading_runs_on_to_the_formula_under_it(monkeypatch):
    """ctx:32000:jev 2026-10-15: Lezione 18's momentum window ended 15 chars
    before `v <- alpha v - eta g`, and the model wrote a rule the note does not
    hold. Capped by the next heading, by the run-on, and by the next window."""
    body = "x" * 50 + "\n## Momentum\n" + "y" * 40 + "\n$$ v = a v - e g $$\n" + "z" * 400 + "\n## Next\n"
    head = body.index("## Momentum")
    cut = [(0, body[:head + 30])]
    monkeypatch.setattr(atomic, "READ_QUERY_RUN_ON", 0)
    assert atomic._run_on(body, cut) == cut
    monkeypatch.setattr(atomic, "READ_QUERY_RUN_ON", 100)
    (off, text), = atomic._run_on(body, cut)
    assert off == 0 and "v = a v - e g" in text and "## Next" not in text
    assert len(text) == head + 30 + 100
    monkeypatch.setattr(atomic, "READ_QUERY_RUN_ON", 10_000)
    assert atomic._run_on(body, cut)[0][1] == body[:body.index("## Next")]
    two = atomic._run_on(body, [(0, body[:head + 30]), (head + 60, body[head + 60:head + 70])])
    assert two[0][1] == body[:head + 60]
    assert atomic._run_on(body, [(0, "x" * 20)]) == [(0, "x" * 20)]  # no heading, no run-on
