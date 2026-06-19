"""Content-based concept extraction (UKE) — Fase 1: YAKE replaces markup.

The old `recon.extract_concepts` keyed concepts on markdown markup (headings /
bold / acronyms), so prose papers — concepts living in unmarked sentences —
extracted to nearly nothing. This module ranks concepts from the *content*
itself via YAKE, an unsupervised statistical keyphrase extractor.

Return shape (`list[ConceptCandidate]`, ranked best-first) is stable so a future
Fase 2 can fuse extra legs (structural, embedder) without touching callers. See
docs/superpowers/specs/2026-06-19-concept-recon-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from silica.kernel.overlay import DomainOverlay, get_active_overlay
from silica.kernel.recon import _strip_frontmatter, is_concept, normalize

# Cutoff knobs (calibration — tune on a real paper + lecture via the eval).
TOKENS_PER_CONCEPT = 120  # ponytail: more tokens => more concepts allowed
MIN_CONCEPTS = 4          # ponytail: floor; don't force 8 ideas onto 3 sentences
MAX_CONCEPTS = 40
YAKE_POOL = 100           # candidates YAKE proposes (also the Fase 2 rerank pool)

# CONFIG.cooccurrence_lang is Snowball-style ("italian"); YAKE wants ISO ("it").
_SNOWBALL_TO_ISO = {
    "arabic": "ar", "danish": "da", "dutch": "nl", "english": "en",
    "finnish": "fi", "french": "fr", "german": "de", "hungarian": "hu",
    "italian": "it", "norwegian": "no", "portuguese": "pt", "romanian": "ro",
    "russian": "ru", "spanish": "es", "swedish": "sv",
}


@dataclass
class ConceptCandidate:
    phrase: str
    score: float                       # ordering only (YAKE cost; lower = better). NOT calibrated.
    evidence: list[str] = field(default_factory=list)  # provenance/debug, e.g. ["yake:0.12"]


def _yake_leg(text: str, overlay: DomainOverlay, lang: str) -> list[ConceptCandidate] | None:
    """YAKE-ranked candidates (best-first), filtered through the overlay.

    Abstains (None) if YAKE is unimportable or yields nothing. YAKE returns
    (phrase, cost) ascending (lower cost = more relevant), already deduplicated.
    """
    try:
        import yake
    except ImportError:
        return None

    iso = _SNOWBALL_TO_ISO.get(lang.lower(), lang.lower()[:2] or "en")
    kw = yake.KeywordExtractor(lan=iso, n=3, top=YAKE_POOL, dedupLim=0.9)
    raw = kw.extract_keywords(text)  # already sorted ascending (best-first)
    if not raw:
        return None

    out: list[ConceptCandidate] = []
    for phrase, cost in raw:
        norm = normalize(phrase)
        if is_concept(norm, overlay=overlay):
            out.append(ConceptCandidate(phrase=norm, score=float(cost),
                                        evidence=[f"yake:{cost:.3f}"]))
    return out or None


def _cutoff(content: str, ranked: list[ConceptCandidate]) -> list[ConceptCandidate]:
    n_tok = len(content.split())
    k = max(MIN_CONCEPTS, min(MAX_CONCEPTS, n_tok // TOKENS_PER_CONCEPT))
    return ranked[:min(k, len(ranked))]


def extract_keyphrases(
    content: str,
    *,
    overlay: DomainOverlay | None = None,
    lang: str = "english",
    embedder=None,  # Fase 2: ignored in Fase 1
) -> list[ConceptCandidate]:
    """Ranked concept candidates from *content* (Fase 1: YAKE only).

    Returns [] if the leg abstains (yake missing / no candidates), which
    `silica_recon` already handles as an empty report.
    """
    if overlay is None:
        overlay = get_active_overlay()
    body = _strip_frontmatter(content)  # metadata, not content concepts
    ranked = _yake_leg(body, overlay, lang)
    if not ranked:
        return []
    return _cutoff(body, ranked)


if __name__ == "__main__":  # ponytail: self-check, no framework
    txt = ("La discesa del gradiente ottimizza la funzione di perdita aggiornando "
           "i pesi della rete neurale a ogni iterazione del training. " * 3)
    from silica.kernel.overlay import DomainOverlay as _DO
    cands = extract_keyphrases(txt, overlay=_DO(stopwords=frozenset(), noise_patterns=()),
                               lang="italian")
    assert cands, "expected concepts from prose"
    assert MIN_CONCEPTS <= len(cands) <= MAX_CONCEPTS
    print(f"OK: {len(cands)} concepts; top={cands[0].phrase!r}")
