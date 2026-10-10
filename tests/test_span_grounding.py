"""Span-grounding gate — math/code spans in a distilled body must be locatable
in the source excerpt (langextract-style char grounding, warn-only).

The distiller prompt requires formulas/code verbatim from the excerpt, but the
requirement was prompt-only: nothing verified it post-distillation. An invented
formula ($\\epsilon = 10^{-8}$ where the excerpt never mentions epsilon) now
surfaces as an `ungrounded` entry. Never a rejection: the prompt's own few-shot
sanctions re-typesetting ASCII math into LaTeX, so a span class is gated only
when the source itself uses that markup.
"""
from __future__ import annotations

import pytest

from silica.kernel.write.provenance import ungrounded_spans
from silica.kernel.write.validate import validate_operations


@pytest.fixture(autouse=True)
def _low_snippet_floor(monkeypatch):
    # These integration tests exercise the grounding gate, not the length gate;
    # keep the raised write-snippet floor from shadowing short verbatim fixtures.
    monkeypatch.setenv("SILICA_MIN_WRITE_SNIPPET_CHARS", "1")


# --- pure function -----------------------------------------------------------

def test_exact_math_is_grounded():
    src = "The loss gradient is $\\frac{\\partial L}{\\partial w} = \\delta \\cdot a$ as shown."
    body = "Formally: $\\frac{\\partial L}{\\partial w} = \\delta \\cdot a$"
    assert ungrounded_spans(body, src) == []


def test_whitespace_drift_is_grounded():
    src = "Update rule: $$w_{t+1} = w_t - \\eta \\nabla L(w_t)$$"
    body = "$$\nw_{t+1} = w_t   -   \\eta \\nabla L(w_t)\n$$"
    assert ungrounded_spans(body, src) == []


def test_a_bold_macro_swap_is_still_the_same_formula():
    # The dominant flag class on a converted-PDF source: the converter emits the
    # macro its PDF encoded, the model re-emits the one it learned, and a vector
    # equation copied faithfully reads as fabricated.
    src = "The normal equation is $\\mathbf{w}^* = (\\mathbf{Z}^T \\mathbf{Z})^{-1} \\mathbf{Z}^T \\mathbf{y}$."
    body = "$$\\boldsymbol{w}^* = (\\boldsymbol{Z}^T \\boldsymbol{Z})^{-1} \\boldsymbol{Z}^T \\boldsymbol{y}$$"
    assert ungrounded_spans(body, src) == []


def test_the_normalizer_folds_bold_and_leaves_alphabets_alone():
    # The fold is one closed family, deliberately not an alias table: blackboard
    # bold and calligraphic are different sets, not two spellings of one.
    from silica.kernel.write.provenance import _norm_math

    assert _norm_math("\\mathbf{w}") == _norm_math("\\boldsymbol{w}") == _norm_math("\\pmb{w}")
    assert _norm_math("\\mathcal{X}") != _norm_math("\\mathbb{X}")
    assert _norm_math("\\bmod") == "\\bmod"  # \b-guarded: not a bold macro


def test_an_upright_wrapper_swap_is_still_the_same_formula():
    # The dominant residue class of the 2026-08-05 run: converter says
    # \mathrm (spaced out by MinerU), model says \text — one macro swap on a
    # short span used to fall below the locality floor and read as fabricated.
    src = "Spearman: $\\rho _ { \\mathrm { a l l } } = 0 . 6 3 4$ over all pairs."
    body = "Correlation was $\\rho_{\\text{all}} = 0.634$ on the full set."
    assert ungrounded_spans(body, src) == []


def test_operatorname_star_and_pr_are_the_same_operator():
    src = ("Bound: $\\operatorname* { P r } \\left[ | \\mathrm { M M - s r } | "
           "\\le 4 . 4 2 \\right] \\ge 1 - \\gamma$ holds.")
    body = "$$\\Pr\\left[ |\\mathrm{MM-sr}| \\le 4.42 \\right] \\ge 1 - \\gamma$$"
    assert ungrounded_spans(body, src) == []


def test_the_normalizer_folds_upright_wrappers_and_leaves_alphabets_alone():
    from silica.kernel.write.provenance import _norm_math

    assert (_norm_math("\\mathrm{err}") == _norm_math("\\text{err}")
            == _norm_math("\\operatorname{err}") == "err")
    assert _norm_math("\\operatorname*{Pr}") == _norm_math("\\Pr") == "Pr"
    assert _norm_math("\\mathsf{Fill}") == _norm_math("\\mathrm{Fill}")
    assert _norm_math("\\Prime") == "\\Prime"  # guarded: not the \Pr operator
    assert _norm_math("\\mathbb{R}") != _norm_math("\\mathcal{R}")


def test_invented_formula_is_flagged():
    src = "Adam uses $\\beta_1 = 0.9$ and $\\beta_2 = 0.999$ for its moving averages of gradients."
    body = "Hyperparameters: $\\beta_1 = 0.9$, $\\beta_2 = 0.999$, $\\epsilon_{stability} = 10^{-8}$ (numerical stability)."
    spans = ungrounded_spans(body, src)
    assert len(spans) == 1
    assert "epsilon" in spans[0]


def test_short_inline_math_is_skipped():
    src = "A discussion of variables with $x$ somewhere."
    body = "Let $y$ be the output and $z$ the latent."  # too short to gate
    assert ungrounded_spans(body, src) == []


def test_latex_not_gated_when_source_has_no_math_markup():
    # Re-typesetting ASCII math into LaTeX is sanctioned (few-shot does it).
    src = "Formula: dL/dw = delta * a. No dollar signs anywhere in this excerpt."
    body = "$$\\frac{\\partial L}{\\partial w} = \\delta \\cdot a$$"
    assert ungrounded_spans(body, src) == []


def test_near_miss_invented_index_is_flagged():
    # beta_3=0.95 assembled next to real beta_1/beta_2 — the dangerous class:
    # global-scatter fuzzy matching used to self-ground it (probe 2026-07-12).
    src = "Adam uses $\\beta_1 = 0.9$ and $\\beta_2 = 0.999$ for its moving averages of gradients."
    body = "Adam extension: $\\beta_3 = 0.95$ controls the third moment."
    assert ungrounded_spans(body, src) == ["\\beta_3 = 0.95"]


def test_altered_constant_is_flagged():
    # 0.01 → 0.1 is a subsequence edit, invisible to any similarity ratio;
    # caught by the verbatim numeric-literal check.
    src = "Learning rate $\\eta_{decay} = 0.01$ halves every epoch."
    body = "We set $\\eta_{decay} = 0.1$ per the schedule."
    assert ungrounded_spans(body, src) == ["\\eta_{decay} = 0.1"]


def test_recombined_formula_is_flagged():
    # Plausible recombination of true fragments scattered across the excerpt;
    # caught by the locality window (fragments can't all fit in one window).
    src = ("The gradient $\\nabla L(w_t)$ drives the update $w_{t+1} = w_t - \\eta \\nabla L(w_t)$ "
           "with momentum $v_t = \\gamma v_{t-1} + \\eta \\nabla L(w_t)$ as usual.")
    body = "$$v_{t+1} = w_t - \\gamma \\nabla L(v_t)$$"
    assert ungrounded_spans(body, src) == ["v_{t+1} = w_t - \\gamma \\nabla L(v_t)"]


def test_invented_code_block_is_flagged():
    src = "```python\nimg = Image.open('cat.png')\n```"
    body = "```python\nmodel.fit(X_train, y_train, epochs=100)\n```"
    spans = ungrounded_spans(body, src)
    assert len(spans) == 1
    assert "model.fit" in spans[0]


def test_matching_code_block_is_grounded():
    src = "Load images:\n```python\nimg = Image.open('cat.png')\n```"
    body = "```python\nimg = Image.open('cat.png')\n```"
    assert ungrounded_spans(body, src) == []


# --- validate_operations integration ----------------------------------------

def _payload(excerpt: str) -> dict:
    return {
        "schema_version": 1,
        "batches": [{
            "inbox_file": "/inbox/lez.md",
            "concepts": [{
                "name": "Adam Optimizer",
                "action_hint": "create",
                "inbox_excerpt": excerpt,
                "vault_collision": None,
            }],
        }],
    }


def _write_op(snippet: str) -> dict:
    return {
        "op": "write",
        "path": "Corso/Adam Optimizer.md",
        "heading": "Adam Optimizer",
        "source_basename": "lez.md",
        "snippet": snippet,
    }


def test_ungrounded_op_validates_but_is_reported(tmp_vault):
    excerpt = "Adam combines momentum and RMSProp, with $\\beta_1 = 0.9$ controlling the first moment."
    snippet = (
        "Adam (Adaptive Moment Estimation) maintains moving averages of the "
        "gradient and its square. Typical values: $\\beta_1 = 0.9$ and "
        "$\\epsilon_{stability} = 10^{-8}$ for numerical robustness."
    )
    ungrounded: list[dict] = []
    validated, rejected = validate_operations(
        [_write_op(snippet)], [_payload(excerpt)], "Corso",
        ungrounded_out=ungrounded,
    )
    assert rejected == []            # warn-only: never a rejection
    assert any(o.heading == "Adam Optimizer" for o in validated)
    assert len(ungrounded) == 1
    assert ungrounded[0]["heading"] == "Adam Optimizer"
    assert any("epsilon" in s for s in ungrounded[0]["spans"])


def test_patch_grounds_against_collision_excerpt_too(tmp_vault):
    # A patch restating a formula from the colliding vault note (for coherence)
    # is not fabrication — the distiller legitimately saw that excerpt.
    note_path = tmp_vault.note("Corso/Adam Optimizer.md", "# Adam Optimizer\nexisting body")
    payload = {
        "schema_version": 1,
        "batches": [{
            "inbox_file": "/inbox/lez.md",
            "concepts": [{
                "name": "Adam Optimizer",
                "action_hint": "enrich",
                "inbox_excerpt": "Adam also decays the second moment with $\\beta_2 = 0.999$.",
                "vault_collision": {
                    "path": note_path,
                    "match_type": "title",
                    "total_hits": 1,
                    "excerpt": "First moment decay is $\\beta_1 = 0.9$ (momentum-like).",
                },
            }],
        }],
    }
    patch_op = {
        "op": "patch",
        "path": note_path,
        "heading": "Adam Optimizer",
        "source_basename": "lez.md",
        # beta_1 comes from the VAULT excerpt, beta_2 from the inbox excerpt
        "snippet": "With $\\beta_1 = 0.9$ on the first moment, Adam adds $\\beta_2 = 0.999$ for the second.",
    }
    ungrounded: list[dict] = []
    validated, rejected = validate_operations(
        [patch_op], [payload], "Corso", ungrounded_out=ungrounded,
    )
    assert rejected == []
    assert any(o.heading == "Adam Optimizer" for o in validated)
    assert ungrounded == []


def test_span_verbatim_elsewhere_in_source_is_not_flagged(tmp_vault):
    # The steer model sees the whole bundle and legitimately stitches from
    # neighboring windows (IDK.md, 2026-08-05: all 3 flagged spans were
    # verbatim in the file, outside the concept's own excerpt). When the
    # source document is readable, a span grounded anywhere in it clears.
    src_path = tmp_vault.note(
        "Inbox/lez.md",
        "Intro on optimizers.\n\n"
        "Momentum uses $v_t = \\gamma v_{t-1} + \\eta \\nabla L(w_t)$ as usual.\n\n"
        "Adam combines momentum and RMSProp, with $\\beta_1 = 0.9$.",
    )
    payload = {
        "schema_version": 1,
        "batches": [{
            "inbox_file": src_path,
            "concepts": [{
                "name": "Adam Optimizer",
                "action_hint": "create",
                # The excerpt window does NOT contain the momentum formula.
                "inbox_excerpt": "Adam combines momentum and RMSProp, with $\\beta_1 = 0.9$.",
                "vault_collision": None,
            }],
        }],
    }
    snippet = (
        "Adam builds on momentum, $v_t = \\gamma v_{t-1} + \\eta \\nabla L(w_t)$, "
        "with $\\beta_1 = 0.9$ on the first moment."
    )
    ungrounded: list[dict] = []
    validated, rejected = validate_operations(
        [_write_op(snippet)], [payload], "Corso", ungrounded_out=ungrounded,
    )
    assert rejected == []
    assert any(o.heading == "Adam Optimizer" for o in validated)
    assert ungrounded == []


def test_fabrication_stays_flagged_with_full_source_available(tmp_vault):
    # The fallback only ever clears spans that ARE in the document — an
    # invented constant fails against the whole file exactly as it failed
    # against the excerpt.
    src_path = tmp_vault.note(
        "Inbox/lez.md",
        "Adam combines momentum and RMSProp, with $\\beta_1 = 0.9$.",
    )
    payload = {
        "schema_version": 1,
        "batches": [{
            "inbox_file": src_path,
            "concepts": [{
                "name": "Adam Optimizer",
                "action_hint": "create",
                "inbox_excerpt": "Adam combines momentum and RMSProp, with $\\beta_1 = 0.9$.",
                "vault_collision": None,
            }],
        }],
    }
    snippet = "Typical: $\\beta_1 = 0.9$ and $\\epsilon_{stability} = 10^{-8}$ for robustness."
    ungrounded: list[dict] = []
    validate_operations(
        [_write_op(snippet)], [payload], "Corso", ungrounded_out=ungrounded,
    )
    assert len(ungrounded) == 1
    assert any("epsilon" in s for s in ungrounded[0]["spans"])


def test_grounded_op_reports_nothing(tmp_vault):
    excerpt = (
        "Adam combines momentum and RMSProp. First moment decay $\\beta_1 = 0.9$, "
        "second moment decay $\\beta_2 = 0.999$."
    )
    snippet = (
        "Adam (Adaptive Moment Estimation) maintains moving averages of the "
        "gradient and its square, with $\\beta_1 = 0.9$ and $\\beta_2 = 0.999$ "
        "as decay rates for the first and second moment respectively."
    )
    ungrounded: list[dict] = []
    validated, rejected = validate_operations(
        [_write_op(snippet)], [_payload(excerpt)], "Corso",
        ungrounded_out=ungrounded,
    )
    assert rejected == []
    assert any(o.heading == "Adam Optimizer" for o in validated)
    assert ungrounded == []
