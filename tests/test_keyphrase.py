"""Tests for silica.kernel.keyphrase — content-based concept extraction (Fase 1).

The thesis: markup-only extraction (recon.extract_concepts) returns ~0 real
concepts on prose with no headings/bold/acronyms; YAKE recovers them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_EXAMPLE_OVERLAYS = Path(__file__).resolve().parent.parent / "examples" / "overlays"

# Italian prose, NO markup: the case that broke the old markup-only recon.
_PROSE = (
    "La discesa del gradiente stocastico ottimizza la funzione di perdita "
    "aggiornando i pesi della rete neurale a ogni iterazione del training. "
    "Il tasso di apprendimento controlla l'ampiezza del passo di aggiornamento. "
    "La retropropagazione calcola i gradienti rispetto a ciascun parametro del modello."
)


@pytest.fixture
def it_overlay():
    path = _EXAMPLE_OVERLAYS / "it-academic.yaml"
    if not path.exists():
        pytest.skip(f"examples overlay not found: {path}")
    from silica.kernel.overlay import load_overlay
    return load_overlay(path)


def test_prose_extracts_content_concepts(it_overlay):
    """Prose with no markup yields real domain concepts (markup-only gave ~0)."""
    from silica.kernel.keyphrase import extract_keyphrases

    cands = extract_keyphrases(_PROSE, overlay=it_overlay, lang="italian")
    phrases = " ".join(c.phrase.lower() for c in cands)

    assert cands, "no concepts extracted from prose"
    assert "gradiente" in phrases or "rete neurale" in phrases


def _fake_ranked(n):
    from silica.kernel.keyphrase import ConceptCandidate
    return [ConceptCandidate(phrase=f"c{i}", score=float(i)) for i in range(n)]


def test_cutoff_scales_with_tokens_and_caps():
    """k = clamp(n_tok / TOKENS_PER_CONCEPT, MIN, MAX), capped at candidates."""
    from silica.kernel.keyphrase import (
        MAX_CONCEPTS, MIN_CONCEPTS, TOKENS_PER_CONCEPT, _cutoff,
    )
    pool = _fake_ranked(100)

    huge = "w " * (TOKENS_PER_CONCEPT * (MAX_CONCEPTS + 10))   # well past MAX
    assert len(_cutoff(huge, pool)) == MAX_CONCEPTS

    mid = "w " * (TOKENS_PER_CONCEPT * 12)                     # 12 in [MIN, MAX]
    assert len(_cutoff(mid, pool)) == 12

    tiny = "w " * 5                                            # below MIN => floor
    assert len(_cutoff(tiny, pool)) == MIN_CONCEPTS

    assert len(_cutoff(huge, _fake_ranked(7))) == 7           # never exceed candidates


def test_frontmatter_ignored(it_overlay):
    """YAML front matter is metadata, not content: it must not change concepts."""
    from silica.kernel.keyphrase import extract_keyphrases

    body = _PROSE
    with_fm = "---\ntitle: ZzzParolaSegreta\ntags: [nascosto]\n---\n" + body
    a = [c.phrase for c in extract_keyphrases(with_fm, overlay=it_overlay, lang="italian")]
    b = [c.phrase for c in extract_keyphrases(body, overlay=it_overlay, lang="italian")]

    assert a == b


def test_empty_content_abstains(it_overlay):
    """No content => empty list (silica_recon handles it as an empty report)."""
    from silica.kernel.keyphrase import extract_keyphrases

    assert extract_keyphrases("", overlay=it_overlay, lang="italian") == []
