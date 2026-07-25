# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Keeps the phase 2 instrument honest: the swap, the tie-break, the truncation.

The measure itself needs a real vault; these cover the three places where a
silent bug would produce a plausible-looking wrong number.
"""
from __future__ import annotations

from evals.probe_ppr_phase2 import _beats, _ppr_builder, _profile_builder, _truncate


class _FakeStore:
    """Only what the swapped builder touches."""

    def adjacency(self, scope=None):
        return {"a": {"b": 1.0}, "b": {"a": 1.0, "c": 1.0}, "c": {"b": 1.0}}


def test_swapped_builder_matches_the_production_call_signature():
    """All four call sites pass (store, seeds) positionally with scope/expand
    keyword-only. If the swap did not accept that shape the arms would crash,
    or worse, silently fall back."""
    import silica.kernel.relatedness as R

    original = R._profile_from_seeds
    with _profile_builder(_ppr_builder(hops=2, alpha=0.5, top_n=10)):
        profile = R._profile_from_seeds(_FakeStore(), {"a": 1.0}, scope=None, expand=False)
        assert profile["b"] > 0.0, "mass never left the seed"
        assert profile["a"] > profile["c"], "restart mass must dominate the 2-hop tail"
    assert R._profile_from_seeds is original, "the global was not restored"


def test_truncation_keeps_the_heaviest_stems():
    profile = {"x": 0.1, "y": 0.9, "z": 0.5}
    assert _truncate(profile, 5) == profile
    assert set(_truncate(profile, 2)) == {"y", "z"}


def test_beats_reproduces_the_fusion_tie_break():
    """`_fuse` sorts by (-score, path): on a tie the lexicographically smaller
    path wins. Getting this backwards would silently shift every ceiling by one."""
    others = {"aaa": 1.0, "zzz": 1.0, "mmm": 2.0}
    assert _beats(1.0, "bbb", others) == 2      # mmm outranks, aaa wins the tie
    assert _beats(1.0, "aa", others) == 1       # target sorts first: it wins both ties
    assert _beats(3.0, "bbb", others) == 0
