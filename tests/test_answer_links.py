"""ADR-0127 point 1: an answer links the notes its turn read, by title, and a
name several notes share is never resolved silently (ADR-0114)."""
from __future__ import annotations

import json

from silica.agent.recall_watch import READS_KEY, read_refs
from silica.kernel.recall.mindmap import _matches_in, _resolve_in, answer_resolver

PATHS = ["Corso B/Lezione 16.md", "Inbox/ml/Lezione 16.md", "Inbox/ml/Lezione 6.md",
         "Percettrone.md", "ML.md"]
GRAPH = ({p: None for p in PATHS}, {}, None)


def _html(answer: str, refs) -> str:
    from silica.ui.web.server import _answer_html

    return _answer_html(answer, refs, graph=GRAPH)


def test_matches_lists_every_note_a_name_can_mean_and_the_old_resolver_keeps_the_first():
    assert _matches_in("Lezione 16", PATHS, {}) == ["Corso B/Lezione 16.md", "Inbox/ml/Lezione 16.md"]
    assert _matches_in("Inbox/ml/Lezione 16", PATHS, {}) == ["Inbox/ml/Lezione 16.md"]
    assert _resolve_in("Lezione 16", PATHS, {}) == "Corso B/Lezione 16.md"   # notes still render as before


def test_a_shared_name_resolves_to_the_note_the_turn_read_or_to_nothing():
    resolve, read = answer_resolver(["Inbox/ml/Lezione 16.md"], graph=GRAPH)
    assert read == ["Inbox/ml/Lezione 16.md"]
    assert resolve("Lezione 16") == "Inbox/ml/Lezione 16.md"
    resolve, read = answer_resolver(["Lezione 16"], graph=GRAPH)   # the ref is itself ambiguous: read nothing
    assert read == [] and resolve("Lezione 16") == ""
    assert resolve("Percettrone") == "Percettrone.md" and resolve("Assente") is None


def test_a_bare_title_of_a_read_note_links_and_code_and_case_do_not():
    html = _html("Vedi Lezione 6, e `Lezione 6`, e lezione 6.", ["Inbox/ml/Lezione 6.md"])
    assert html.count('data-path="Inbox/ml/Lezione 6.md"') == 1, html
    assert "<code>Lezione 6</code>" in html


def test_a_title_inside_a_longer_number_or_a_link_is_left_alone():
    html = _html("Lezione 66 e [testo Lezione 6](https://e.com)", ["Inbox/ml/Lezione 6.md"])
    assert "note-link" not in html, html


def test_a_title_shorter_than_four_characters_never_links_bare():
    html = _html("Il ML moderno.", ["ML.md"])
    assert "note-link" not in html


def test_an_ambiguous_wikilink_is_plain_text_not_a_broken_link():
    html = _html("Vedi [[Lezione 16]].", [])
    assert "note-link" not in html and "Lezione 16" in html
    html = _html("Vedi [[Lezione 16]].", ["Corso B/Lezione 16.md"])
    assert 'data-path="Corso B/Lezione 16.md"' in html


def _call(cid: str, name: str, args: dict) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_read_refs_names_this_turn_only_and_survives_a_stubbed_result():
    msgs = [
        {"role": "user", "content": "prima"},
        _call("a", "silica_read_note", {"name": "Vecchia"}),
        {"role": "tool", "tool_call_id": "a", "content": "..."},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "cosa dice la Lezione 16?"},
        _call("b", "silica_recall", {"query": "x"}),
        {"role": "tool", "tool_call_id": "b", "content": json.dumps({"notes": ["Inbox/ml/Lezione 16.md"]})},
        _call("c", "silica_read_note", {"name": "Percettrone"}),
        {"role": "tool", "tool_call_id": "c", "content": "[stubbed]"},
        _call("d", "silica_search_context", {"query": "y"}),
        {"role": "tool", "tool_call_id": "d", "content": json.dumps({"hits": [{"path": "ML.md"}]})},
        {"role": "assistant", "content": "La Lezione 16 dice..."},
    ]
    assert read_refs(msgs) == ["Inbox/ml/Lezione 16.md", "Percettrone", "ML.md"]
    assert read_refs(msgs, 4) == ["Vecchia"]   # a reopened chat reads each answer's own turn


def test_reads_never_reach_the_provider():
    from silica.agent.providers import INTERNAL_KEYS

    assert READS_KEY in INTERNAL_KEYS
