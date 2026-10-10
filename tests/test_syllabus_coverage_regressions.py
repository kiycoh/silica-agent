# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Regressions for the three defects a syllabus-coverage audit surfaced.

Asking "is every concept in this syllabus a note in my vault?" answered
"missing" for concepts that had notes. Three independent causes, one test
each -- see the docstrings on the code under test for the measurements.
"""
from silica.kernel.recall import paths
from silica.kernel.recall.rerank import rerank_related
from silica.kernel.text.recon import is_concept
from silica.kernel.text.overlay import overlay_for_lang


# --- 1. concept names must not end on a function word ----------------------

def test_clause_heading_is_not_a_concept():
    """A slide headed `## Da notare che` became a note called that."""
    it = overlay_for_lang("italian")
    assert not is_concept("Da notare che", overlay=it)
    assert not is_concept("intera presentazione del", overlay=it)
    assert not is_concept("Hidden Layer Nel", overlay=it)


def test_real_concepts_survive_the_tail_rule():
    """The first cut of this rule tested the tail against overlay.stopwords
    and deleted these -- 'ai' is an Italian preposition, 'cfu' is overlay
    noise, 'analysis' is a generic noun."""
    it = overlay_for_lang("italian")
    for name in ("Paradigmi di AI", "Storia dell'AI", "Fisher discriminant analysis",
                 "Chain rules per le derivate", "Algoritmi di apprendimento",
                 "Kernel trick", "Analisi della correlazione canonica"):
        assert is_concept(name, overlay=it), name


# --- 2. source leaves are invisible under a write_dir too ------------------

def test_source_leaf_matches_the_write_dir_copy(monkeypatch):
    """`silica/sources/Lezione 1.md` was answering every search, so verbatim
    lectures outranked the notes distilled from them."""
    monkeypatch.setattr(paths, "is_source_leaf", paths.is_source_leaf)  # unwrapped
    import silica.kernel.vault_manifest as vm
    monkeypatch.setattr(vm, "in_write_dir", lambda rel: f"silica/{rel}")
    assert paths.is_source_leaf("sources/Lezione 1.md")        # legacy root
    assert paths.is_source_leaf("silica/sources/Lezione 1.md")  # composed
    assert not paths.is_source_leaf("silica/Informatica/Kernel trick.md")


def test_source_leaf_survives_an_unresolvable_manifest(monkeypatch):
    import silica.kernel.vault_manifest as vm

    def boom(rel):
        raise RuntimeError("no config")

    monkeypatch.setattr(vm, "in_write_dir", boom)
    assert paths.is_source_leaf("sources/x.md")
    assert not paths.is_source_leaf("silica/sources/x.md")


# --- 3. one long candidate must not silence the whole pool -----------------

class _Rec:
    def __init__(self, path, score=0.03):
        self.path, self.score, self.origin = path, score, "vault"


class _Reranker:
    def scores(self, query, docs):
        return [0.99 if "gold" in d else 0.01 for d in docs]


def _rerank(lengths, monkeypatch, stats):
    """Rerank a pool whose bodies have the given lengths; the last is gold."""
    pool = [_Rec(f"n{i}.md") for i in range(len(lengths))]
    bodies = {f"n{i}.md": ("gold " if i == len(lengths) - 1 else "filler ") * (n // 7 + 1)
              for i, n in enumerate(lengths)}
    monkeypatch.setattr("silica.kernel.recall.rerank._read_body",
                        lambda p, origin="vault": (p, bodies[p]))
    return rerank_related(_Reranker(), "gold", pool, k=len(pool), stats=stats)


def test_one_short_candidate_keeps_the_cross_encoder(monkeypatch):
    """Two 20k-char lectures beside short notes used to win the median vote
    and drop every note's score back to a first-stage cosine."""
    stats: dict = {}
    out = _rerank([20_000, 20_000, 20_000, 900, 900], monkeypatch, stats)
    assert stats["reranked"] is True
    assert out[0].path == "n4.md"
    assert out[0].score == 0.99


def test_an_all_long_pool_still_abstains(monkeypatch):
    """The gate's calibration case -- uniformly unreadable candidates -- must
    keep firing, and must say so."""
    stats: dict = {}
    out = _rerank([20_000, 20_000, 20_000], monkeypatch, stats)
    assert stats["reranked"] is False
    assert [r.path for r in out] == ["n0.md", "n1.md", "n2.md"]  # first-stage order


def test_abstention_is_reported_when_there_is_no_reranker():
    stats: dict = {}
    pool = [_Rec("a.md")]
    assert rerank_related(None, "q", pool, k=1, stats=stats) == pool
    assert stats["reranked"] is False
