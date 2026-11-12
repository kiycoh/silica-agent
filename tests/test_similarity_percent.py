"""ADR-0164: the percentage on near and similar notes, and the list at a note's foot.

The number is the raw cosine of the two notes' embeddings, rounded and floored at
0, and only where the embedding leg proposed the pair; the foot list is the
embedding leg alone, over the embedder's own floor.
"""
from __future__ import annotations

import pytest

from silica.kernel.recall import relatedness
from silica.kernel.recall.embed import EmbedStore
from silica.kernel.recall.relatedness import RelatedNote, similarity_percent


def test_the_percentage_is_the_rounded_cosine_floored_at_zero():
    assert similarity_percent(0.834) == 83
    assert similarity_percent(0.995) == 100
    assert similarity_percent(-0.2) == 0          # "unlike" is no list's meaning
    assert similarity_percent(None) is None       # no cosine, no number


def test_a_note_only_the_concepts_proposed_has_no_percentage():
    both = RelatedNote(path="a", name="a", score=0.03, evidence=["embed:0.71", "cooccur:w4"],
                       embed_score=0.71, cooccur_weight=4)
    concepts = RelatedNote(path="b", name="b", score=0.02, evidence=["cooccur:w9"], cooccur_weight=9)
    assert both.similarity == 71 and concepts.similarity is None


def test_the_floor_is_the_embedders_and_a_model_nobody_measured_gets_the_default():
    assert relatedness.similar_at("jina-embeddings-v5-text-small-retrieval") == 0.60
    assert relatedness.similar_at("perplexity/pplx-embed-v1-4b") == 0.60
    assert relatedness.similar_at("google/embeddinggemma-2") == 0.84
    assert relatedness.similar_at("Qwen3-Embedding-0.6B") == relatedness.SIMILAR_AT
    assert relatedness.similar_at("") == relatedness.SIMILAR_AT


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = EmbedStore(path=tmp_path / "embeddings.json")
    s.upsert("Notes/A", "A", [1.0, 0.0, 0.0])
    s.upsert("Notes/Twin", "Twin", [0.9, 0.1, 0.0])        # cosine 0.99
    s.upsert("Notes/Kin", "Kin", [0.7, 0.7, 0.0])          # cosine 0.71
    s.upsert("Notes/Far", "Far", [0.0, 1.0, 0.0])          # cosine 0
    s.upsert("log", "log", [1.0, 0.0, 0.0])               # a vault artifact, never a neighbour
    monkeypatch.setattr("silica.kernel.recall.embed.get_store", lambda: s)
    monkeypatch.setattr(relatedness, "similar_at", lambda model: 0.60)
    return s


def test_the_foot_lists_the_notes_over_the_floor_nearest_first(store):
    near = relatedness.similar_notes("Notes/A.md")
    assert [(r.path, r.similarity) for r in near] == [("Notes/Twin", 99), ("Notes/Kin", 71)]


def test_the_foot_is_none_without_a_vector_and_empty_when_nothing_clears_the_floor(store, monkeypatch):
    assert relatedness.similar_notes("Notes/Missing.md") is None
    monkeypatch.setattr(relatedness, "similar_at", lambda model: 0.999)
    assert relatedness.similar_notes("Notes/A.md") == []


def test_the_foot_is_none_on_a_vault_with_no_vectors(tmp_path, monkeypatch):
    empty = EmbedStore(path=tmp_path / "empty.json")
    monkeypatch.setattr("silica.kernel.recall.embed.get_store", lambda: empty)
    assert relatedness.similar_notes("Notes/A.md") is None


def test_the_route_hands_the_drawer_vault_paths_and_null_without_vectors(monkeypatch):
    from silica.ui.web import notes_routes

    monkeypatch.setattr(notes_routes, "note_resolver", lambda: lambda p: p if p.endswith(".md") else p + ".md")
    monkeypatch.setattr(relatedness, "similar_notes", lambda path, k=5: [
        RelatedNote(path="Notes/Twin", name="Twin", score=0.99, evidence=[], embed_score=0.99)])
    assert notes_routes.note_similar("Notes/A.md") == {
        "notes": [{"name": "Twin", "path": "Notes/Twin.md", "similarity": 99}]}
    monkeypatch.setattr(relatedness, "similar_notes", lambda path, k=5: None)
    assert notes_routes.note_similar("Notes/A.md") == {"notes": None}


def test_a_minhash_duplicate_states_its_reason_and_no_percentage():
    from silica.kernel.report.curator import compose_curation_plan
    from silica.kernel.report.graph_report import VaultReport
    from silica.kernel.report.graph_report.models import DuplicatePair

    report = VaultReport(generated_at="2026-11-12T00:00:00Z", scope="", totals={}, god_nodes=[],
                         bridges=[], orphans=[], dangling=[], clusters=[])
    report.confirmed_duplicate_pairs = [DuplicatePair("A.md", "B.md", 0.931)]
    report.duplicate_pairs = [DuplicatePair("C.md", "D.md", 0.81, cosine=False)]
    reasons = [i.reason for i in compose_curation_plan(report).by_kind("dedup")]
    assert reasons == ["93% similar", "shares most of its wording"]
