"""Tests for silica.kernel.recon — concept extraction via DomainOverlay seam."""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
# extract_concepts — DEFAULT overlay (no vault overlay file)
# ---------------------------------------------------------------------------

class TestExtractConceptsDefault:
    def test_heading_concept_kept(self):
        """A real content heading survives with the default overlay."""
        from silica.kernel.recon import extract_concepts
        content = "# Backpropagation\n\nSome explanation."
        result = extract_concepts(content)
        assert "Backpropagation" in result

    def test_bold_concept_kept(self):
        """A bold concept phrase survives with the default overlay."""
        from silica.kernel.recon import extract_concepts
        content = "The algorithm uses **Gradient Descent** to minimise loss."
        result = extract_concepts(content)
        assert "Gradient Descent" in result

    def test_structural_heading_filtered(self):
        """'Chapter 3: Introduction' is filtered by noise pattern and stopword.

        The heading matches the noise pattern ^(Chapter|Lesson|Exercise)\\b[:\\s]
        and the word "chapter" is also a stopword.
        """
        from silica.kernel.recon import extract_concepts
        content = "# Chapter 3: Introduction\n\nContent here."
        result = extract_concepts(content)
        assert not any("Chapter" in c for c in result)

    def test_summary_heading_filtered(self):
        """'Summary' as a bare heading is filtered as structural noise."""
        from silica.kernel.recon import extract_concepts
        content = "# Summary\n\nKey points."
        result = extract_concepts(content)
        assert "Summary" not in result

    def test_stopword_only_candidate_filtered(self):
        """'the' in a heading (after normalization) is dropped as a stopword."""
        from silica.kernel.recon import extract_concepts
        content = "# the\n"
        result = extract_concepts(content)
        assert "the" not in result

    def test_trailing_colon_filtered(self):
        """Heading ending with a colon is filtered as structural noise."""
        from silica.kernel.recon import extract_concepts
        content = "# Resources:\n\nSee links."
        result = extract_concepts(content)
        assert "Resources:" not in result

    def test_question_candidate_filtered(self):
        """Heading ending in '?' is filtered."""
        from silica.kernel.recon import extract_concepts
        content = "# What is recursion?\n"
        result = extract_concepts(content)
        assert not any("?" in c for c in result)

    def test_acronym_extracted(self):
        """Acronyms like PID are extracted via from_acronyms."""
        from silica.kernel.recon import extract_concepts
        content = "The PID controller regulates output."
        result = extract_concepts(content)
        assert "PID" in result

    def test_frontmatter_stripped(self):
        """Concepts inside YAML front matter are not extracted."""
        from silica.kernel.recon import extract_concepts
        content = "---\ntitle: SecretConcept\ntags: [hidden]\n---\n# Visible\n"
        result = extract_concepts(content)
        assert "SecretConcept" not in result
        assert "Visible" in result


# ---------------------------------------------------------------------------
# extract_concepts — Italian-academic overlay passed explicitly
# ---------------------------------------------------------------------------

class TestExtractConceptsItalianOverlay:
    def test_capitolo_heading_filtered(self, it_overlay):
        """'Capitolo 3: Reti Neurali' is filtered by the Italian noise pattern."""
        from silica.kernel.recon import extract_concepts
        content = "# Capitolo 3: Reti Neurali\n"
        result = extract_concepts(content, overlay=it_overlay)
        # The heading string itself is noisy; neither the full string nor "Capitolo" should survive
        assert not any("Capitolo" in c for c in result)

    def test_unipa_is_stopword(self, it_overlay):
        """'unipa' is a stopword in the Italian overlay and must be filtered."""
        from silica.kernel.recon import extract_concepts
        content = "# unipa\n"
        result = extract_concepts(content, overlay=it_overlay)
        assert "unipa" not in result

    def test_reti_neurali_bold_survives(self, it_overlay):
        """**Reti Neurali** as a bold concept survives the Italian overlay."""
        from silica.kernel.recon import extract_concepts
        content = "L'approccio delle **Reti Neurali** è fondamentale."
        result = extract_concepts(content, overlay=it_overlay)
        assert "Reti Neurali" in result

    def test_italian_overlay_still_keeps_english_content(self, it_overlay):
        """The Italian overlay extends default, so English concepts still survive."""
        from silica.kernel.recon import extract_concepts
        content = "# Backpropagation\n"
        result = extract_concepts(content, overlay=it_overlay)
        assert "Backpropagation" in result


# ---------------------------------------------------------------------------
# is_concept — overlay argument honoured
# ---------------------------------------------------------------------------

class TestIsConceptOverlayArg:
    def test_explicit_overlay_used_over_active(self):
        """is_concept uses an explicitly passed overlay, not get_active_overlay."""
        from silica.kernel.overlay import DomainOverlay, DEFAULT_OVERLAY
        import re
        # Build a minimal overlay that rejects "Backpropagation" via a noise pattern
        block_bp = DomainOverlay(
            stopwords=frozenset(),
            noise_patterns=(re.compile(r"^Backpropagation$", re.IGNORECASE),),
        )
        from silica.kernel.recon import is_concept
        # With the blocking overlay it should be rejected
        assert not is_concept("Backpropagation", overlay=block_bp)
        # With default overlay it should pass
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


# ---------------------------------------------------------------------------
# Acronym path — from_acronyms with default overlay
# ---------------------------------------------------------------------------

class TestAcronymPathDefault:
    def test_pid_extracted(self):
        from silica.kernel.recon import extract_concepts
        content = "The PID algorithm maintains stability."
        assert "PID" in extract_concepts(content)

    def test_short_acronym_too_short_filtered(self):
        """Two-character uppercase word: from_acronyms catches >=2 chars but MIN_LEN=3 filters it."""
        from silica.kernel.recon import extract_concepts
        content = "Use AI now."
        # "AI" is 2 chars; MIN_LEN=3, so it must be filtered by is_concept
        result = extract_concepts(content)
        assert "AI" not in result

    def test_noise_prefixed_acronym_filtered(self):
        """Uppercase prefix like 'NB: something' is a noise pattern, not a concept."""
        from silica.kernel.recon import extract_concepts
        content = "# NB: important\n"
        result = extract_concepts(content)
        # "NB: important" matches '^[A-Z]{2,6}:\s' noise pattern
        assert not any("NB:" in c for c in result)
