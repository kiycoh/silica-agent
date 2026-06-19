"""Tests for silica.kernel.recon — concept filtering via the DomainOverlay seam.

Concept *extraction* now lives in silica.kernel.keyphrase (YAKE); recon keeps the
overlay-driven *filter* (`is_concept`) applied to every candidate, plus the
collision-ranking helpers. These tests guard the domain knowledge in the overlays
(which headings/words are noise) against the live filter.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from silica.kernel.overlay import DEFAULT_OVERLAY

_EXAMPLE_OVERLAYS = (
    Path(__file__).resolve().parent.parent / "examples" / "overlays"
)


@pytest.fixture
def it_overlay():
    """Load the Italian-academic example overlay."""
    path = _EXAMPLE_OVERLAYS / "it-academic.yaml"
    if not path.exists():
        pytest.skip(f"examples overlay not found: {path}")
    from silica.kernel.overlay import load_overlay
    return load_overlay(path)


# ---------------------------------------------------------------------------
# is_concept — noise rejected (default overlay)
# ---------------------------------------------------------------------------

class TestIsConceptFiltersNoise:
    @pytest.mark.parametrize("phrase", [
        "Chapter 3: Introduction",   # noise pattern ^(Chapter|Lesson|Exercise)\b[:\s]
        "Summary",                   # structural-noise word
        "the",                       # stopword
        "Resources:",                # trailing colon
        "What is recursion?",        # question
        "AI",                        # below MIN_LEN
        "NB: important",             # ^[A-Z]{2,6}:\s noise prefix
    ])
    def test_rejected(self, phrase):
        from silica.kernel.recon import is_concept
        assert not is_concept(phrase, overlay=DEFAULT_OVERLAY)


# ---------------------------------------------------------------------------
# is_concept — real concepts kept
# ---------------------------------------------------------------------------

class TestIsConceptKeepsConcepts:
    @pytest.mark.parametrize("phrase", ["Backpropagation", "Gradient Descent", "PID"])
    def test_kept_default(self, phrase):
        from silica.kernel.recon import is_concept
        assert is_concept(phrase, overlay=DEFAULT_OVERLAY)

    def test_italian_overlay_filters_noise(self, it_overlay):
        from silica.kernel.recon import is_concept
        assert not is_concept("Capitolo 3: Reti Neurali", overlay=it_overlay)
        assert not is_concept("unipa", overlay=it_overlay)  # vault stopword

    def test_italian_overlay_keeps_concepts(self, it_overlay):
        from silica.kernel.recon import is_concept
        assert is_concept("Reti Neurali", overlay=it_overlay)
        assert is_concept("Backpropagation", overlay=it_overlay)  # extends default


# ---------------------------------------------------------------------------
# is_concept — overlay argument honoured
# ---------------------------------------------------------------------------

class TestIsConceptOverlayArg:
    def test_explicit_overlay_used_over_active(self):
        """is_concept uses an explicitly passed overlay, not get_active_overlay."""
        from silica.kernel.overlay import DomainOverlay
        import re
        block_bp = DomainOverlay(
            stopwords=frozenset(),
            noise_patterns=(re.compile(r"^Backpropagation$", re.IGNORECASE),),
        )
        from silica.kernel.recon import is_concept
        assert not is_concept("Backpropagation", overlay=block_bp)
        assert is_concept("Backpropagation", overlay=DEFAULT_OVERLAY)

    def test_explicit_stopword_overlay(self):
        """is_concept filters a word that is a stopword only in the explicit overlay."""
        from silica.kernel.overlay import DomainOverlay
        custom_overlay = DomainOverlay(
            stopwords=frozenset({"neuralnetwork"}),
            noise_patterns=(),
        )
        from silica.kernel.recon import is_concept
        assert not is_concept("neuralnetwork", overlay=custom_overlay)

    def test_none_overlay_uses_active(self, monkeypatch):
        """is_concept(s, overlay=None) falls back to get_active_overlay()."""
        from silica.kernel.overlay import DomainOverlay
        sentinel = DomainOverlay(
            stopwords=frozenset({"sentinel_word"}),
            noise_patterns=(),
        )
        monkeypatch.setattr("silica.kernel.recon.get_active_overlay", lambda: sentinel)
        from silica.kernel.recon import is_concept
        assert not is_concept("sentinel_word")
