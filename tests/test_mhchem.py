# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0159: `\\ce{}` and `\\pu{}` read as mhchem before latex2mathml, one case per
construct a student writes, and anything else shown as its source, never guessed."""
from __future__ import annotations

import re

import pytest

from silica.kernel.text import mhchem
from silica.kernel.text.render import mathml


@pytest.mark.parametrize("src, want", [
    ("H2O", r"\mathrm{{H}_{2}{O}}"),                                     # digit subscripts
    ("SO4^2-", r"\mathrm{{{S}{O}_{4}}^{2-}}"),                           # a charge after ^
    ("Fe^{3+}", r"\mathrm{{{Fe}}^{3+}}"),                                # a braced charge
    ("NH4+", r"\mathrm{{{N}{H}_{4}}^{+}}"),                              # a trailing sign
    ("2H2O", r"2\,\mathrm{{H}_{2}{O}}"),                                 # a coefficient
    ("2 H2O", r"2\,\mathrm{{H}_{2}{O}}"),                                # ... spaced, as mhchem allows
    ("1/2O2", r"\frac{1}{2}\,\mathrm{{O}_{2}}"),                         # a fraction
    ("NaCl(aq)", r"\mathrm{{Na}{Cl}\text{(aq)}}"),                       # a state
    ("H2O (l)", r"\mathrm{{H}_{2}{O}}\text{(l)}"),                       # ... spaced
    ("Na+(aq)", r"\mathrm{{{Na}}^{+}\text{(aq)}}"),                      # a charge then a state
    ("CuSO4*5H2O", r"\mathrm{{Cu}{S}{O}_{4}}\cdot 5\,\mathrm{{H}_{2}{O}}"),  # a hydrate
    ("CuSO4.5H2O", r"\mathrm{{Cu}{S}{O}_{4}}\cdot 5\,\mathrm{{H}_{2}{O}}"),
    ("CuSO4 * 5 H2O", r"\mathrm{{Cu}{S}{O}_{4}} \cdot 5\,\mathrm{{H}_{2}{O}}"),
    ("Ca(OH)2", r"\mathrm{{Ca}({O}{H})_{2}}"),                           # a group
    ("[Cu(NH3)4]^2+", r"\mathrm{{[{Cu}({N}{H}_{3})_{4}]}^{2+}}"),
    ("^{14}_{6}C", r"\mathrm{{}^{14}_{6}{C}}"),                          # an isotope
    ("2e-", r"2\,\mathrm{{e}^{-}}"),                                     # the electron
    ("CO2 + C -> 2 CO", r"\mathrm{{C}{O}_{2}} + \mathrm{{C}} \longrightarrow 2\,\mathrm{{C}{O}}"),
    ("A <- B", r"\mathrm{{A}} \leftarrow \mathrm{{B}}"),
    ("A <-> B", r"\mathrm{{A}} \leftrightarrow \mathrm{{B}}"),
    ("N2 + 3H2 <=> 2NH3", r"\mathrm{{N}_{2}} + 3\,\mathrm{{H}_{2}} \rightleftharpoons 2\,\mathrm{{N}{H}_{3}}"),
    ("CaCO3 ->[\\Delta] CaO", r"\mathrm{{Ca}{C}{O}_{3}} \overset{\Delta}{\longrightarrow} \mathrm{{Ca}{O}}"),
    ("A ->[heat][cat] B", r"\mathrm{{A}} \underset{\text{cat}}{\overset{\text{heat}}{\longrightarrow}} \mathrm{{B}}"),
    ("Ag+ + Cl- -> AgCl v", r"\mathrm{{{Ag}}^{+}} + \mathrm{{{Cl}}^{-}} \longrightarrow \mathrm{{Ag}{Cl}} \downarrow"),
])
def test_each_construct_reads_as_mhchem_does(src, want):
    assert mhchem.ce_tex(src) == want


@pytest.mark.parametrize("src", ["heat", "H2O ?", "Ca(OH", "2", "H2O -> x y", "A ->[<b>] B", "", "H2O %"])
def test_what_the_grammar_cannot_read_answers_none(src):
    assert mhchem.ce_tex(src) is None


@pytest.mark.parametrize("src, want", [
    ("123 kJ*mol-1", r"123\,\mathrm{{kJ}}\cdot \mathrm{{mol}}^{-1}"),
    ("9.81 m/s2", r"9.81\,\mathrm{{m}}/\mathrm{{s}}^{2}"),
    ("1.6e-19 C", r"1.6\times 10^{-19}\,\mathrm{{C}}"),
    ("1,5 mol", r"1{,}5\,\mathrm{{mol}}"),
    ("kJ mol-1", r"\mathrm{{kJ}}\,\mathrm{{mol}}^{-1}"),
])
def test_units(src, want):
    assert mhchem.pu_tex(src) == want


def test_a_formula_draws_upright_with_its_subscript():
    out = mathml(r"\ce{H2O}", display=False)
    assert "math-err" not in out and "\\ce" not in out
    assert '<mi mathvariant="normal">H</mi>' in out and "<mn>2</mn>" in out


def test_a_ce_beside_ordinary_math_keeps_both():
    out = mathml(r"\Delta H = -286\,\pu{kJ/mol} \text{ for } \ce{H2 + 1/2O2 -> H2O}", display=True)
    assert "math-err" not in out and "&#x02192;" not in out  # the long arrow, not \rightarrow
    assert "&#x027F6;" in out and "<mfrac>" in out


@pytest.mark.parametrize("tex", [r"\ce{A + B -> C}", r"\ce{A ->[heat] B}", r"p \implies q", r"a \longleftrightarrow b"])
def test_a_long_arrow_is_spaced_as_an_operator(tex):
    """latex2mathml writes the long arrows as identifiers, which take no spacing:
    `\\ce{A + B -> C}` drew B⟶C touching while `<=>` stood apart (2026-11-12, in Chrome)."""
    out = mathml(tex, display=False)
    assert "math-err" not in out and not re.search(r"<mi>&#x027F[0-9A-F];</mi>", out)
    assert re.search(r"<mo>&#x027F[0-9A-F];</mo>", out)


@pytest.mark.parametrize("tex", [r"\ce{H2O ?}", r"\ce{H2O", r"\pu{12 kJ#}", r"x + \ce{}"])
def test_an_unreadable_formula_shows_its_source(tex):
    assert mathml(tex, display=False) == f'<code class="math-err">${tex}$</code>'


def test_a_smiles_fence_is_left_for_the_page_to_draw():
    from silica.kernel.text.render import render

    html = render("```smiles\nCC(=O)O<x>\n```\n", link=lambda p, d: d, asset=lambda t: t)
    assert '<pre class="smiles">CC(=O)O&lt;x&gt;\n</pre>' in html
