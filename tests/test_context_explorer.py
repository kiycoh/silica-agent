"""Context explorer — GET /context, the drawer's other reading of a note.

Four deterministic sections and zero LLM calls, so the whole thing is testable
on a tmp vault with no index, no model and no network. The recall legs
(concepts, related) degrade to empty when their stores are cold; what must never
degrade is the structural half — snippets, links, backlinks, ghost links — which
comes straight off the driver.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(tmp_vault, tmp_path, monkeypatch):
    from silica.ui.web import server

    monkeypatch.setattr(server, "SESSIONS_DIR", tmp_path / "web_sessions")
    return TestClient(server.app), tmp_vault


# --- snippet extraction (pure) ----------------------------------------------

def test_snippets_take_the_lead_sentence_of_body_and_each_section():
    from silica.ui.web.server import _key_snippets

    body = (
        "The vault is a graph of notes. A second sentence nobody asked for.\n\n"
        "## Why\n\n- a bullet that says nothing alone\n\n"
        "Because structure beats folders. And more.\n\n"
        "## How\n\nBy writing wikilinks as you go. Trailing prose.\n\n"
        "## Ignored\n\nThe fourth section never shows.\n" + "x " * 400
    )
    out = _key_snippets(body)
    assert [s["heading"] for s in out] == ["", "Why", "How"]        # at most three
    assert out[0]["text"] == "The vault is a graph of notes."       # first sentence only
    assert out[1]["text"] == "Because structure beats folders."     # bullets skipped
    assert out[2]["text"] == "By writing wikilinks as you go."


def test_snippets_are_hidden_for_a_short_note():
    """Reading a short note whole costs less than reading an extract of it."""
    from silica.ui.web.server import _key_snippets

    assert _key_snippets("A tiny note.\n\n## Section\n\nOne line.") == []


def test_long_sentence_is_cut_on_a_word_boundary():
    from silica.ui.web.server import _first_sentence

    out = _first_sentence("word " * 60)
    assert len(out) <= 141 and out.endswith("…") and not out.endswith(" …")


# --- the endpoint ------------------------------------------------------------

def test_context_reports_how_a_note_is_and_should_be_connected(client):
    api, vault = client
    vault.note("Hub.md", "# Hub\n\nA hub note.\n\nSee [[Leaf]] and [[Nowhere]].\n")
    vault.note("Leaf.md", "# Leaf\n\nA leaf that points back to [[Hub]].\n")

    data = api.get("/context", params={"path": "Hub.md"}).json()

    assert data["title"] == "Hub" and data["path"] == "Hub.md" and data["ghost"] is False
    assert {r["name"] for r in data["related"]["outgoing"]} == {"Leaf"}
    assert {r["name"] for r in data["related"]["backlinks"]} == {"Leaf"}
    # "Nowhere" is a wikilink leaving this note that no file answers: the note
    # already claims the connection, only the file is missing.
    ghosts = [s for s in data["suggested"] if s["kind"] == "ghost"]
    assert [g["name"] for g in ghosts] == ["Nowhere"]
    assert ghosts[0]["path"] == ""


def test_context_reads_frontmatter_related(client):
    api, vault = client
    vault.note("A.md", "---\nrelated:\n  - B\n  - Ghosty\n---\n\nBody.\n")
    vault.note("B.md", "# B\n")

    fm = api.get("/context", params={"path": "A.md"}).json()["related"]["frontmatter"]
    assert [r["name"] for r in fm] == ["B", "Ghosty"]
    assert fm[0]["path"] == "B.md"   # resolvable → clickable
    assert fm[1]["path"] == ""       # not a note → listed, not clickable


def test_context_on_a_missing_note_is_graceful(client):
    api, _vault = client
    data = api.get("/context", params={"path": "Nope.md"}).json()
    assert "error" in data and data["ghost"] is False


def test_ghost_context_is_a_subject_of_its_own(client):
    """A ghost node carries path "" — clicking one used to be a silent no-op.
    In context it has a name, the notes that invoke it, and one action."""
    api, vault = client
    vault.note("One.md", "# One\n\nPoints at [[Phantom]].\n")
    vault.note("Two.md", "# Two\n\nAlso points at [[Phantom]].\n")

    data = api.get("/context", params={"ghost": "1", "name": "Phantom"}).json()

    assert data["ghost"] is True and data["title"] == "Phantom" and data["path"] == ""
    assert data["snippets"] == []                      # no body, so no reader either
    assert sorted(r["name"] for r in data["related"]["backlinks"]) == ["One", "Two"]


def test_concept_endpoint_returns_graph_keys(client, monkeypatch):
    """The recall stores key on cooccur_key (path minus .md), the graph on the
    full path. /concept resolves back, or the ids never match a node and the
    focus lights nothing."""
    from silica.ui.web import server

    api, vault = client
    vault.note("Deep/Note.md", "# Note\n")
    monkeypatch.setattr(
        "silica.tools.graph.silica_concepts",
        lambda term="", note="", k=10: {"concept": term, "notes": [{"path": "Deep/Note", "count": 3}]},
    )
    assert api.get("/concept", params={"term": "vault"}).json()["notes"] == ["Deep/Note.md"]
    assert server  # module imported through the app under test


def test_suggested_skips_notes_that_are_already_linked(client, monkeypatch):
    """distance 1 means "already linked" — that is Related's business. This
    section is only for the link that does not exist yet."""
    api, vault = client
    vault.note("A.md", "# A\n\nLinks to [[B]].\n")
    vault.note("B.md", "# B\n")
    vault.note("Far.md", "# Far\n")

    monkeypatch.setattr(
        "silica.tools.graph.silica_related",
        lambda note="", k=5: {"results": [
            {"path": "B", "name": "B", "score": 0.91, "distance": 1},      # linked
            {"path": "Far", "name": "Far", "score": 0.77, "distance": None},  # unreachable
        ]},
    )
    sug = api.get("/context", params={"path": "A.md"}).json()["suggested"]
    notes = [s for s in sug if s["kind"] == "note"]
    assert [s["name"] for s in notes] == ["Far"]
    assert notes[0]["path"] == "Far.md"          # resolved back to a graph key
    assert "unreachable" in notes[0]["why"]
