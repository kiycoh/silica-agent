# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The controls that keep the eval gates honest have to be kept honest too.

In `tests/` and not in `evals/` on purpose: the eval tests sit outside the default
testpaths, and a guard against rot that only runs when someone remembers to point
pytest at it is the rot.
"""
import pytest

from evals import negative_controls as nc


def test_every_registered_control_holds():
    """Each metric scores its fixtures exactly as registered — this is the check
    the gate runner performs, run in the suite so a broken metric surfaces at
    edit time and not at gate time."""
    nc.assert_metrics_discriminate(*nc.CONTROLS)


def test_a_metric_that_cannot_fail_is_caught(monkeypatch):
    """The NexusRAG shape: a metric whose pattern can never match, so it reports
    the same clean number on every input."""
    monkeypatch.setitem(
        nc.CONTROLS, "effective_citations",
        (lambda body: 0, nc.CONTROLS["effective_citations"][1], "always zero"),
    )
    with pytest.raises(SystemExit, match="failed their own controls"):
        nc.assert_metrics_discriminate("effective_citations")


def test_a_control_whose_cases_agree_proves_nothing(monkeypatch):
    """Two fixtures expecting the same value cannot separate a live metric from a
    constant, so the control itself is the artifact."""
    monkeypatch.setitem(
        nc.CONTROLS, "vacuous", (lambda x: 1, [((1,), 1), ((2,), 1)], "no contrast"),
    )
    with pytest.raises(SystemExit, match="cannot tell a live metric"):
        nc.assert_metrics_discriminate("vacuous")


def test_an_unregistered_metric_fails_the_run():
    with pytest.raises(SystemExit, match="no negative control registered"):
        nc.assert_metrics_discriminate("effective_citations", "brand_new_metric")


def test_the_gate_runner_refuses_before_it_touches_a_provider(monkeypatch, tmp_path):
    """The check has to sit ahead of the model work, or a dead metric is only
    found after the run has already been paid for."""
    from evals import probe_web_gate

    monkeypatch.setitem(
        nc.CONTROLS, "bank_validity",
        (lambda bank, pages: 1.0, nc.CONTROLS["bank_validity"][1], "always perfect"),
    )

    def _boom(*a, **kw):  # pragma: no cover - must never be reached
        raise AssertionError("a judge call was made despite a dead metric")

    monkeypatch.setattr(probe_web_gate, "replay_run", _boom)
    (tmp_path / "run.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="failed their own controls"):
        probe_web_gate.main(["replay", "--runs", str(tmp_path)])
