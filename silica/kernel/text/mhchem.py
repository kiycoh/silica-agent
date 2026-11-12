# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""`\\ce{...}` and `\\pu{...}` (mhchem) as plain LaTeX, for latex2mathml (ADR-0159).

latex2mathml has no mhchem: `\\ce{H2O}` came out as an identifier `\\ce` beside an
italic "H2O". PyPI holds no maintained mhchem port (searched 2026-11-12; the
reference parser is JavaScript), so this is a grammar of the forms a student
writes: species with digit subscripts, charges, coefficients, states, groups,
hydrate dots and isotopes, `+`, and four arrows with optional text over and under.
Anything outside it answers None and the caller shows the formula's source,
never a guess.
"""
from __future__ import annotations

import re

# Longest first: `<=>` and `<->` both start like `<-`.
_ARROWS = {"<=>": r"\rightleftharpoons", "<->": r"\leftrightarrow", "->": r"\longrightarrow", "<-": r"\leftarrow"}
_ARROW = re.compile(r"(<=>|<->|->|<-)(?:\[([^\[\]]*)\])?(?:\[([^\[\]]*)\])?(?=\s|$)")
_COEF = re.compile(r"(\d+/\d+|\d+(?:\.\d+)?)(?=[A-Z(\[^]|$)")
_COEF_ALONE = re.compile(r"\d+/\d+|\d+(?:\.\d+)?")
_ELECTRON_COEF = re.compile(r"(\d+)(?=e(?:-|\^-|\^\{-\})$)")
_ISOTOPE = re.compile(r"\^\{?(\d+)\}?_\{?(\d+)\}?(?=[A-Z])")
_ELEMENT = re.compile(r"[A-Z][a-z]?")
_COUNT = re.compile(r"\d+")
_STATE = re.compile(r"\((aq|s|l|g)\)$")
# `^2-`, `^{3+}`, or the signs a species ends on (`Na+`, `NO3-`), before its state if any.
_CHARGE = re.compile(r"\^(?:\{(\d*[+-])\}|(\d*[+-]))|([+-]+)(?=$|\((?:aq|s|l|g)\)$)")
_GREEK = frozenset(r"\alpha \beta \gamma \delta \Delta \epsilon \lambda \mu \nu \pi \sigma \omega \Omega \theta".split())
_LABEL_TEXT = re.compile(r"[A-Za-z0-9 ,.°%'-]+")


def _body(s: str, i: int, close: str = "") -> tuple[str, int] | None:
    """One formula from s[i]: elements and counts, bracketed groups, an isotope prefix.
    Stops at `close` (the group's own bracket), a state, a charge, or a hydrate dot."""
    out: list[str] = []
    while i < len(s):
        c = s[i]
        if close and c == close:
            return "".join(out), i
        if c in "([":
            if _STATE.match(s, i):
                break  # `(aq)` closing the species is its state, not a group
            inner = _body(s, i + 1, ")" if c == "(" else "]")
            if inner is None or inner[1] >= len(s) or not inner[0]:
                return None
            text, i = inner
            out.append(f"{c}{text}{s[i]}")
            i += 1
        elif (m := _ISOTOPE.match(s, i)) is not None:
            out.append(f"{{}}^{{{m.group(1)}}}_{{{m.group(2)}}}")
            i = m.end()
            continue
        elif (m := _ELEMENT.match(s, i)) is not None:
            out.append("{" + m.group(0) + "}")
            i = m.end()
        else:
            break
        if (n := _COUNT.match(s, i)) is not None:
            out[-1] += f"_{{{n.group(0)}}}"
            i = n.end()
    if close:
        return None  # the group never closed
    return "".join(out), i


def _species(tok: str) -> str | None:
    """`2H2O`, `SO4^2-`, `Ca(OH)2`, `CuSO4*5H2O`, `NaCl(aq)`, `^{14}_{6}C`, `e-`."""
    parts, i = [], 0
    while True:
        coef = ""
        if (m := _COEF.match(tok, i)) is not None or (m := _ELECTRON_COEF.match(tok, i)) is not None:
            num = m.group(1)
            coef = (r"\frac{%s}{%s}" % tuple(num.split("/")) if "/" in num else num) + r"\,"
            i = m.end()
        if tok[i:] in ("e-", "e^-", "e^{-}"):  # the electron, the one lowercase species
            parts.append(coef + r"\mathrm{{e}^{-}}")
            i = len(tok)
            break
        got = _body(tok, i)
        if got is None or not got[0]:
            return None
        text, i = got
        if (m := _CHARGE.match(tok, i)) is not None:
            text = f"{{{text}}}^{{{m.group(1) or m.group(2) or m.group(3)}}}"
            i = m.end()
        if (m := _STATE.match(tok, i)) is not None:
            text += r"\text{%s}" % m.group(0)
            i = m.end()
        parts.append(coef + r"\mathrm{" + text + "}")
        if i < len(tok) and tok[i] in "*.":
            i += 1
            continue
        break
    return r"\cdot ".join(parts) if i == len(tok) else None


def _label(text: str) -> str | None:
    """What an arrow says over or under it: a species, a Greek letter, `$math$`, or words."""
    text = text.strip()
    if not text:
        return ""
    if text.startswith("$") and text.endswith("$") and len(text) > 2:
        return text[1:-1]
    if text in _GREEK:
        return text
    if " " not in text and (sp := _species(text)) is not None:
        return sp
    return r"\text{%s}" % text if _LABEL_TEXT.fullmatch(text) else None


def ce_tex(src: str) -> str | None:
    """The body of a `\\ce{}` as LaTeX, or None when it is not mhchem this grammar reads."""
    out: list[str] = []
    i, n = 0, len(src)
    coef, last = "", False  # a number not yet given its species; whether out[-1] is a species
    while i < n:
        if src[i].isspace():
            i += 1
            continue
        if (m := _ARROW.match(src, i)) is not None:
            above, below = _label(m.group(2) or ""), _label(m.group(3) or "")
            if above is None or below is None:
                return None
            arrow = _ARROWS[m.group(1)]
            if above:
                arrow = r"\overset{%s}{%s}" % (above, arrow)
            if below:
                arrow = r"\underset{%s}{%s}" % (below, arrow)
            out.append(arrow)
            i = m.end()
            last = False
            continue
        end = i
        while end < n and not src[end].isspace():
            end += 1
        tok = src[i:end]
        i = end
        # mhchem's spaced forms, `2 H2O`, `H2O (l)` and `CuSO4 * 5 H2O`, are one
        # species written in pieces: a number waits for the species it counts.
        if coef:
            if (sp := _species(coef + tok)) is None:
                return None
            out.append(sp)
            coef, last = "", True
            continue
        if _COEF_ALONE.fullmatch(tok):
            coef = tok
            continue
        if tok == "+":
            out.append("+")
        elif tok in ("v", "^"):  # a precipitate, a gas given off
            out.append(r"\downarrow" if tok == "v" else r"\uparrow")
        elif tok in ("*", "."):
            out.append(r"\cdot")
        elif last and _STATE.fullmatch(tok):
            out[-1] += r"\text{%s}" % tok
        elif (sp := _species(tok)) is not None:
            out.append(sp)
            last = True
            continue
        else:
            return None
        last = False
    return " ".join(out) if out and not coef else None


_NUMBER = re.compile(r"(-?\d+(?:[.,]\d+)?)(?:[eE]([+-]?\d+))?")
_UNIT = re.compile(r"([A-Za-zµμΩÅ°%]+)(?:\^?\{?([+-]?\d+)\}?)?")
_SEP = re.compile(r"\s*([*./])\s*|\s+")


def pu_tex(src: str) -> str | None:
    """The body of a `\\pu{}`: an optional number then units, `123 kJ*mol-1`, `9.81 m/s2`."""
    s = src.strip()
    out = ""
    if (m := _NUMBER.match(s)) is not None:
        out = m.group(1).replace(",", "{,}")
        if m.group(2):
            out += r"\times 10^{%s}" % m.group(2).lstrip("+")
        s = s[m.end():].strip()
        if s:
            out += r"\,"
    i, units = 0, []
    while i < len(s):
        u = _UNIT.match(s, i)
        if u is None:
            return None
        units.append(r"\mathrm{{%s}}" % u.group(1) + (f"^{{{u.group(2)}}}" if u.group(2) else ""))
        i = u.end()
        if i < len(s):
            sep = _SEP.match(s, i)
            if sep is None or sep.end() == len(s):
                return None
            units.append({"*": r"\cdot ", ".": r"\cdot ", "/": "/"}.get(sep.group(1) or "", r"\,"))
            i = sep.end()
    return out + "".join(units) if out or units else None


_CALL = re.compile(r"\\(ce|pu)\s*\{")


def expand(tex: str) -> str:
    """`tex` with every `\\ce{}` and `\\pu{}` replaced by plain LaTeX. Raises ValueError on
    one it cannot read, so the whole formula falls back to its source."""
    out, pos = [], 0
    for m in _CALL.finditer(tex):
        if m.start() < pos:
            continue  # inside a call already replaced
        depth, j = 1, m.end()
        while j < len(tex) and depth:
            depth += {"{": 1, "}": -1}.get(tex[j], 0)
            j += 1
        if depth:
            raise ValueError("unclosed \\%s{" % m.group(1))
        inner = tex[m.end():j - 1]
        conv = ce_tex(inner) if m.group(1) == "ce" else pu_tex(inner)
        if conv is None:
            raise ValueError("not mhchem: %r" % inner)
        out.append(tex[pos:m.start()] + "{" + conv + "}")
        pos = j
    out.append(tex[pos:])
    return "".join(out)
