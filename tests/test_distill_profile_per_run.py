# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Per-run distill profile: the run decides, not the process.

The profile used to be process-global (SILICA_DISTILL_PROFILE env > vault
manifest), so a run could not say "I am extractive" without mutating state
every concurrent run would inherit. /promote is the first caller that needs
exactly that: a promotion stub is finished verbatim content, and the default
authoring lens + 275-char floor rejects every honest distillation of it
(measured: 55/155/34 chars, three independent runs, all no_ops).
"""
from __future__ import annotations

import pytest

# A phrase unique to the extractive lens (profiles/extractive/rubric.md).
EXTRACTIVE_MARKER = "You are a selector, not"
# A phrase unique to the promotion lens (profiles/promotion/rubric.md).
PROMOTION_MARKER = "the user consented"


class TestRenderPrompt:
    def test_an_explicit_profile_wins_over_the_global_one(self, monkeypatch):
        from silica.kernel import prep_delegation

        monkeypatch.setattr(prep_delegation, "active_distill_profile",
                            lambda: "default")

        rendered = prep_delegation.render_prompt(
            target="Concepts/AI", profile="extractive")

        assert EXTRACTIVE_MARKER in rendered

    def test_no_profile_keeps_the_global_resolution(self, monkeypatch):
        from silica.kernel import prep_delegation

        monkeypatch.setattr(prep_delegation, "active_distill_profile",
                            lambda: "default")

        rendered = prep_delegation.render_prompt(target="Concepts/AI")

        assert EXTRACTIVE_MARKER not in rendered

    def test_the_ephemeral_routing_rule_follows_the_profile(self, monkeypatch):
        """Root cause of the empty bodies, measured on the raw model output:
        the FIXED contract said 'personal, time-bound facts do not belong in
        notes — emit them in ephemerals', so the model dutifully drained the
        whole promotion stub into ephemerals and wrote a note with no body.
        No lens can win against an unconditional contract section: the routing
        directive itself must follow the profile."""
        from silica.kernel import prep_delegation

        monkeypatch.setattr(prep_delegation, "active_distill_profile",
                            lambda: "default")

        default = prep_delegation.render_prompt(target="Life")
        promo = prep_delegation.render_prompt(target="Life", profile="promotion")

        assert "do not belong in notes" in default
        assert "do not belong in notes" not in promo
        assert "already live in the episodic store" in promo
        # The section header and its mechanics stay contract, every profile.
        assert "## Ephemeral Facts (episodic routing)" in promo

    def test_the_promotion_lens_exists_and_never_diverts_to_ephemerals(self, monkeypatch):
        """Measured on a real /promote: the extractive lens (written for the
        ingest direction) told the model 'time-bound personal facts belong in
        ephemerals' — so it skipped every fact of the stub, which came FROM
        the ephemeral store in the first place. Promotion needs its own lens."""
        from silica.kernel import prep_delegation

        monkeypatch.setattr(prep_delegation, "active_distill_profile",
                            lambda: "default")

        rendered = prep_delegation.render_prompt(
            target="Life", profile="promotion")

        assert PROMOTION_MARKER in rendered


class _Stop(Exception):
    """Raised by the render recorder so run_distiller never reaches the LLM."""


class TestRunDistiller:
    def test_the_profile_reaches_the_prompt(self, monkeypatch):
        from silica.kernel import prep_delegation

        seen = {}

        def _record(**kwargs):
            seen.update(kwargs)
            raise _Stop  # the prompt is the first thing built — stop there

        monkeypatch.setattr(prep_delegation, "render_prompt", _record)

        with pytest.raises(_Stop):
            prep_delegation.run_distiller(
                payload={"batches": []}, target="Concepts", profile="extractive")

        assert seen["profile"] == "extractive"


class TestFloor:
    """validate.py:666: 'a 60-char verbatim fact is real content, not the
    prose-placeholder this gate guards against — so the extractive arm sets a
    lower floor'. The floor follows the profile now, not just the env."""

    def test_extractive_lowers_the_floor(self, monkeypatch):
        from silica.kernel.write.validate import min_write_snippet_chars

        monkeypatch.delenv("SILICA_MIN_WRITE_SNIPPET_CHARS", raising=False)

        assert min_write_snippet_chars() == 275
        assert min_write_snippet_chars(profile="extractive") == 40

    def test_the_authoring_env_pin_does_not_strangle_extractive_runs(self, monkeypatch):
        """Measured live: the operator's SILICA_MIN_WRITE_SNIPPET_CHARS=275
        (set for the authoring floor, when there was only one lens) reached a
        promotion run and rejected its verbatim body at '0 < 275'. The pin
        governs the default profile; extractive-class runs keep their own
        floor, with a dedicated env if an operator ever needs the lever."""
        from silica.kernel.write.validate import min_write_snippet_chars

        monkeypatch.setenv("SILICA_MIN_WRITE_SNIPPET_CHARS", "275")

        assert min_write_snippet_chars() == 275
        assert min_write_snippet_chars(profile="extractive") == 40
        assert min_write_snippet_chars(profile="promotion") == 40

        monkeypatch.setenv("SILICA_EXTRACTIVE_MIN_SNIPPET_CHARS", "15")
        assert min_write_snippet_chars(profile="promotion") == 15
        assert min_write_snippet_chars() == 275  # the two levers stay apart

    def test_promotion_is_extractive_class(self, monkeypatch):
        """Same verbatim bodies, same short-durable-facts: the promotion lens
        inherits the extractive floor and the extractivity enforcement."""
        from silica.kernel.write.validate import (
            min_write_snippet_chars, validate_operations)

        monkeypatch.delenv("SILICA_MIN_WRITE_SNIPPET_CHARS", raising=False)
        assert min_write_snippet_chars(profile="promotion") == 40

        excerpt = "- [since 2026-08-01] Rex, a German shepherd, runs daily"
        body = "The user's dog Rex is a German shepherd that runs every day."
        validated, rejected = validate_operations(
            [_write_op(body)], _payload(excerpt), "Life", profile="promotion")
        assert validated == []
        assert "not verbatim" in rejected[0].reason


def _payload(excerpt: str):
    return [{"batches": [{"inbox_file": "/inbox/user.dog.md", "concepts": [
        {"name": "user.dog", "inbox_excerpt": excerpt, "vault_collision": None},
    ]}]}]


def _write_op(body: str):
    return {"op": "write", "path": "Life/Dog.md", "heading": "user.dog",
            "source_basename": "user.dog.md", "snippet": body}


class TestValidateOperations:
    """The gate must judge by the RUN's profile: extractivity on, floor down —
    without SILICA_DISTILL_PROFILE / SILICA_EXTRACTIVE_ENFORCE env."""

    def test_a_short_verbatim_fact_passes_under_the_run_profile(self, monkeypatch):
        from silica.kernel.write.validate import validate_operations

        monkeypatch.delenv("SILICA_MIN_WRITE_SNIPPET_CHARS", raising=False)
        excerpt = "- [since 2026-08-01] Rex, a German shepherd, runs daily"
        ops = [_write_op(excerpt)]  # verbatim, 55 chars: real content

        validated, rejected = validate_operations(
            ops, _payload(excerpt), "Life", profile="extractive")

        assert [r.reason for r in rejected] == []
        # The gate may add a hub op of its own; ours must be among the validated.
        assert any(op.path == "Life/Dog.md" for op in validated)

    def test_a_rewritten_body_is_rejected_under_the_run_profile(self, monkeypatch):
        from silica.kernel.write.validate import validate_operations

        monkeypatch.delenv("SILICA_MIN_WRITE_SNIPPET_CHARS", raising=False)
        excerpt = "- [since 2026-08-01] Rex, a German shepherd, runs daily"
        body = ("The user's dog is a German shepherd breed also known as an "
                "Alsatian; this note documents alternative naming conventions "
                "and their usage in different countries and kennel clubs.")
        ops = [_write_op(body)]  # long enough for any floor, but authored

        validated, rejected = validate_operations(
            ops, _payload(excerpt), "Life", profile="extractive")

        assert validated == []
        assert "not verbatim" in rejected[0].reason


class TestFSMThreading:
    """The run's profile must reach BOTH seams: the prompt (DELEGATE) and the
    gate (VALIDATE). One without the other is worse than neither — an
    extractive prompt judged by the default floor rejects its own output."""

    def _fsm(self):
        from silica.router.orchestrator import InjectorFSM, InjectorState

        fsm = InjectorFSM("Inbox/test.md", "TargetDir",
                          distill_profile="extractive")
        fsm._chunks = [{"chunk_id": 0, "concepts": ["a"]}]
        fsm._current_chunk_idx = 0
        return fsm, InjectorState

    def test_delegate_hands_the_profile_to_the_distiller(self, monkeypatch):
        from unittest.mock import patch

        monkeypatch.setattr(
            "silica.router.states.distill.orch.CONFIG.distill_concurrency", 1)
        fsm, InjectorState = self._fsm()
        fsm.state = InjectorState.DELEGATE
        seen = {}

        def _run(**kwargs):
            seen.update(kwargs)
            return {"updates": []}

        with patch("silica.router.states.distill.run_distiller", side_effect=_run), \
             patch.object(fsm, "_make_tmp", return_value="tmp.json"):
            fsm.step()

        assert seen["profile"] == "extractive"

    def test_validate_judges_by_the_same_profile(self, monkeypatch):
        from unittest.mock import patch

        fsm, InjectorState = self._fsm()
        fsm.state = InjectorState.VALIDATE
        fsm._chunk_ctx["sanitized"] = {"parsed": {"updates": []}}
        seen = {}

        def _validate(ops_path, **kwargs):
            seen.update(kwargs)
            return {"validated_count": 0, "rejected_count": 0,
                    "validated_ops": [], "rejected_ops": []}

        with patch("silica.router.states.distill.orch.silica_validate_ops",
                   side_effect=_validate), \
             patch.object(fsm, "_make_tmp", return_value="tmp.json"):
            fsm.step()

        assert seen["profile"] == "extractive"


def test_coordinator_forwards_the_profile():
    from silica.router.coordinator import Coordinator

    coord = Coordinator(inbox_files=["Inbox/test.md"], target_dir="TargetDir",
                        distill_profile="extractive")
    assert coord.fsm.distill_profile == "extractive"
