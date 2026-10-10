# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Keeps the rubric instrument honest: the gates, the parse, the sampler.

The rubric itself needs a real vault and a judge; these cover the places where
a silent bug would hand back a plausible-looking wrong verdict — a gate that
passes on a constant column, a batch that fabricates zeros when the judge
fails, a replication arm that never actually varies anything.
"""
from __future__ import annotations

from evals.probe_explain_rubric import AXES, _rho, gates, run_arm, score_batch


def _scores(rows: dict[str, tuple[int, int, int, int]]) -> dict[str, dict]:
    return {k: dict(zip(AXES, v), missing="") for k, v in rows.items()}


def _spread(n: int = 12, *, jitter: int = 0) -> dict[str, dict]:
    """n notes whose four axes vary independently — the shape a live rubric has."""
    return _scores({
        f"n{i:02d}": ((i * 7) % 100, (i * 31 + jitter) % 100,
                      (i * 13) % 100, (i * 53) % 100)
        for i in range(n)
    })


def _notes(scores: dict, degree_of=lambda i: (i * 5) % 7) -> list[dict]:
    """Keys are zero-padded upstream so sorted() is numeric order — otherwise
    'n10' sorts before 'n2' and the degree column silently scrambles."""
    return [{"key": k, "degree": degree_of(i), "chars": 100}
            for i, k in enumerate(sorted(scores))]


def test_a_constant_axis_fails_the_variance_gate():
    """The failure this probe exists to catch: a rubric that rates everything
    the same reads as 'the vault is uniformly fine' instead of 'no signal'."""
    flat = _scores({f"n{i:02d}": (50, 50, 50, 50) for i in range(12)})
    g = gates(flat, flat, _scores({f"n{i:02d}": (0, 0, 0, 0) for i in range(12)}), _notes(flat))
    assert not g["G1_variance"]
    assert g["axes_with_spread"] == []
    assert g["verdict"] == "KILL"


def test_four_names_for_one_axis_fails_the_independence_gate():
    same = _scores({f"n{i:02d}": (i * 8, i * 8, i * 8, i * 8) for i in range(12)})
    g = gates(same, same, _scores({f"n{i:02d}": (0, 0, 0, 0) for i in range(12)}), _notes(same))
    assert g["G1_variance"], "the column does vary — it is the collinearity that is fatal"
    assert not g["G2_independence"]


def test_replication_noise_larger_than_signal_fails_reproducibility():
    a = _spread()
    r = _spread(jitter=40)  # comprehension moves ~40 points between passes
    t = _scores({k: (0, 0, 0, 0) for k in a})
    g = gates(a, r, t, _notes(a))
    assert not g["G3_reproducible"]
    assert not g["reproducibility"]["comprehension"]["ok"]
    assert g["reproducibility"]["memory"]["ok"], "the stable axes must stay stable"


def test_structure_that_restates_degree_fails_the_proxy_gate():
    a = _scores({f"n{i:02d}": (100 - i * 7, (i * 31) % 100, i * 8, (i * 53) % 100)
                 for i in range(12)})
    t = _scores({k: (0, 0, 0, 0) for k in a})
    g = gates(a, a, t, _notes(a, degree_of=lambda i: i))  # degree rises with structure
    assert not g["G4_not_a_proxy"]
    assert g["degree_correlation"]["structure"] > 0.9


def test_a_judge_that_ignores_the_body_reads_as_harness_bug_not_as_kill():
    """Title-only scoring like full scoring means the numbers are noise. That
    is a broken instrument, and must not be reported as a verdict on the idea."""
    a = _spread()
    g = gates(a, a, dict(a), _notes(a))  # arm T identical to arm A
    assert not g["H_judge_reads_the_note"]
    assert g["verdict"] == "HARNESS BUG"


def test_a_clean_rubric_passes_every_gate():
    a = _spread()
    t = _scores({k: (5, 0, 0, 0) for k in a})
    g = gates(a, a, t, _notes(a))
    assert (g["G1_variance"], g["G2_independence"], g["G3_reproducible"],
            g["G4_not_a_proxy"], g["H_judge_reads_the_note"]) == (True,) * 5
    assert g["verdict"] == "PASS"


def test_rho_is_zero_not_nan_on_a_constant_column():
    """nan compares False against every threshold, so an undefined correlation
    would pass G2 and G4 silently — the exact shape of a fake PASS."""
    assert _rho([1.0] * 5, [1.0, 2.0, 3.0, 4.0, 5.0]) == 0.0
    assert _rho([1.0, 2.0], [2.0, 1.0]) == 0.0, "too few points to rank"


def test_an_unparseable_judge_reply_drops_rows_instead_of_zeroing_them(monkeypatch):
    """0 is a real score on this rubric ('no evidence'), so a failed parse must
    never mint one — it would push every gate the wrong way and look like data.
    parse_json raises on garbage, so the drop happens one level up in run_arm;
    this pins that the arm swallows it and loses the batch rather than the run."""
    monkeypatch.setattr("silica.agent.llm.call_llm",
                        lambda **kw: type("R", (), {"text": "not json at all"})())
    notes = [{"key": "a", "title": "A", "body": "b", "chars": 1, "degree": 0}]
    assert run_arm(notes, "m") == {}


def test_out_of_range_indices_are_rejected(monkeypatch):
    """A schema-valid index of 99 (or -1) must not wrap onto another note's key."""
    monkeypatch.setattr(
        "silica.agent.llm.call_llm",
        lambda **kw: type("R", (), {"text": '{"scores": [{"index": 99, "memory": 1,'
                                            '"comprehension": 1, "structure": 1,'
                                            '"application": 1},'
                                            '{"index": -1, "memory": 2,'
                                            '"comprehension": 2, "structure": 2,'
                                            '"application": 2},'
                                            '{"index": 0, "memory": 3,'
                                            '"comprehension": 3, "structure": 3,'
                                            '"application": 300}]}'})())
    notes = [{"key": "a", "title": "A", "body": "b", "chars": 1, "degree": 0}]
    out = score_batch(notes, "m")
    assert set(out) == {"a"}
    assert out["a"]["memory"] == 3
    assert out["a"]["application"] == 100, "scores clamp to the 0-100 the rubric declares"
