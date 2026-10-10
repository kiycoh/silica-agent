# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""_hub_desc: the MOC bullet text derived from a note body must be clean prose,
not the raw first line — which was often a fabricated callout, producing garbage
bullets like `- [[X]] — > [!NOTE] Documento originale: ...` (audit finding 3)."""
from silica.router.states.write import _hub_desc


def test_strips_callout_syntax_from_first_line():
    # The audit fix is "strip callout/markdown from the first line": the markdown
    # syntax must go, leaving clean text — no `> [!NOTE]` in the bullet.
    body = "> [!NOTE] Documento originale: lezione 3\n\nLa normalizzazione riscala le feature."
    assert _hub_desc(body) == "Documento originale: lezione 3"


def test_strips_heading_and_list_markers():
    assert _hub_desc("# Titolo") == "Titolo"
    assert _hub_desc("- primo punto") == "primo punto"


def test_skips_purely_structural_first_line_to_next():
    # A bare callout marker with no inline text falls through to the prose line.
    assert _hub_desc("> [!NOTE]\n\ntesto reale") == "testo reale"


def test_plain_first_line_passes_through():
    assert _hub_desc("Una definizione chiara del concetto.") == "Una definizione chiara del concetto."


def test_caps_length():
    assert len(_hub_desc("x " * 200)) <= 120


def test_empty_body_is_empty_desc():
    assert _hub_desc("") == ""
    assert _hub_desc("\n\n> [!NOTE]\n") == ""


# --- safe-boundary cap (2026-08-21: the raw [:120] slice cut the run's hub
# bullets mid-word, mid-LaTeX and mid-link; the edge strip unpaired bolds) ---

def test_cap_cuts_on_word_boundary_with_ellipsis():
    line = ("McCulloch e Pitts introdussero il modello di neurone artificiale "
            "una unita computazionale ispirata al neurone biologico del cervello")
    out = _hub_desc(line)
    assert out.endswith("…") and len(out) <= 121
    assert not out[:-1].endswith(("ispir", "neuron", "biologic"))  # no mid-word
    assert out[:-1].rstrip() == out[:-1]  # no trailing space before ellipsis


def test_cap_never_leaves_open_math_or_link():
    math = ("La trasposta di una matrice molto lunga davvero e' "
            "$\\boldsymbol{A}^{\\mathsf{T}} \\in \\mathbb{R}^{m \\times n}$ "
            "seguita da parole aggiuntive che spingono oltre il limite di centoventi")
    out = _hub_desc(math)
    assert out.count("$") % 2 == 0, out
    link = ("Una descrizione che si avvicina molto al limite dei centoventi "
            "caratteri prima del wikilink assai lungo [[Analisi delle componenti principali]]")
    out = _hub_desc(link)
    assert ("[[" not in out) or ("]]" in out), out


def test_bold_pairs_strip_wholesale():
    # strip('*_`') ate only the leading pair: "Error rate**: proporzione..."
    assert _hub_desc("**Error rate**: proporzione di errori") == \
        "Error rate: proporzione di errori"


def test_letterless_lines_skip_to_prose():
    # A lone $$ opens display math: what follows is TeX until it closes.
    assert _hub_desc("$$\nE = mc^2 dentro la formula\n") == ""
    assert _hub_desc("---\n\nProsa dopo la riga orizzontale.") == \
        "Prosa dopo la riga orizzontale."


def test_display_math_fences_are_never_descriptions():
    # "- [[ha bias]] — $$ \operatorname{bias}(..." — the run-262e6847 hub
    # carried raw TeX innards as bullets when a body opens with display math.
    body = "$$ \\operatorname{bias}(\\hat{\\sigma}^2) = -\\sigma^2/m $$\nDerivazione completa del bias."
    assert _hub_desc(body) == "Derivazione completa del bias."
    multiline = "$$\nE = mc^2 dentro la fence\n$$\nProsa vera dopo la formula."
    assert _hub_desc(multiline) == "Prosa vera dopo la formula."
