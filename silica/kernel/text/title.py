# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""kernel/title — THE identity for note titles (C3).

Before this module, five call sites held five divergent normalizations
(slugify — not even case-insensitive; recon.normalize; _names_agree's private
lowercase fold; the driver index .lower(); is_title_match) and the write path
never compared a freshly coined title against the vault — «Machine Learning
(9 CFU)» happily created the fourth umbrella note.

Two functions, stdlib only:
  title_key(t)              — the equivalence key (casefold, punctuation fold,
                              parenthetical/dash suffix strip, stopword drop,
                              plural-fold via the kernel/text stemmer).
  near_titles(t, titles)    — fuzzy neighbours below key-equality, via
                              difflib.SequenceMatcher.

And the near-title verdict as text, written and read in one place:
  near_title_flag(...)      the flag a near title leaves in `review:`;
  review_flags(review)      every flag a `review:` value holds;
  near_title_of(review)     the first near-title flag's (candidate, path, ratio).

`templates.slugify` stays what it is — a filename sanitizer, not an identity.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# Fuzzy band default: below key-equality, above unrelated titles. Chosen so
# Descriptor/Description land inside and «ML per la statistica» vs «Machine
# Learning» stays out (fork ⚑ — revisit with the labelled borderline set).
NEAR_BAND = 0.80

_PAREN_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")
_DASH_SUFFIX = re.compile(r"\s+[—–-]\s+.*$")
_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)


# Words the stopword lists drop that are identity in a title: "Apprendimento
# non supervisionato" was the key of "Apprendimento supervisionato" and took its
# patch (2026-09-05); "Back-propagation" collapsed onto "Propagation".
_KEEP = frozenset({"non", "not", "no", "senza", "without", "back", "forward"})


def _vault_lang() -> str | None:
    from silica.config import CONFIG
    lang = CONFIG.cooccurrence_lang or "auto"
    return None if lang == "auto" else lang


def title_key(t: str, *, lang: str | None = None) -> str:
    """Equivalence key: two titles with the same key name the same note.

    ``lang`` pins the stemmer (plural-fold); ``None`` detects it from the
    title itself — pass the vault language when you have it, detection on a
    2-4 word label is weak.
    """
    from silica.kernel.text import language
    from silica.kernel.text.text import stem_word

    s = (t or "").strip()
    for rx in (_PAREN_SUFFIX, _DASH_SUFFIX):
        stripped = rx.sub("", s).strip()
        if stripped:  # never strip a title down to nothing
            s = stripped
    s = _PUNCT.sub(" ", s.casefold())
    if not s.strip():
        return ""
    # The vault's declared language before detection: a 2-word label detects
    # as English and the English stopword list eats "back" and "forward", so
    # "Back-propagation", "Forward propagation" and "Propagation (da l-1 a l)"
    # shared one key and the first was patched onto the last (2026-09-05).
    lang = lang or _vault_lang() or language.detect(s)
    stop = language.stopwords_for(lang)
    stems = [
        stem_word(w, lang=lang)
        for w in re.findall(r"\w+", s)
        if len(w) >= 2 and (w not in stop or w in _KEEP)
    ]
    # Titles made purely of stopwords/short tokens ("Le basi") must not all
    # collapse onto the empty key: fall back to the folded surface.
    return " ".join(stems) if stems else s.strip()


_NUMBERS = re.compile(r"\d+")


def numbers_differ(a: str, b: str) -> bool:
    """Two titles that BOTH carry numbers, and not the same ones.

    The key drops digits, and the fuzzy bands sit "Capitolo 3" next to
    "Capitolo 4" by construction, so every comparator asks this first: a
    different number is a different note (lecture, chapter, version, theorem).
    One-sided numbers stay what they were, a slide enumerator ("Foo" vs
    "Foo 1" fold together, tests/test_golden_probes.py). Measured 2026-09-02:
    without this the lesson-12 outline note was coerced onto lesson 11's.
    """
    na, nb = _NUMBERS.findall(a or ""), _NUMBERS.findall(b or "")
    return bool(na) and bool(nb) and na != nb


def same_surface(a: str, b: str) -> bool:
    """The identity for a merge that no judge reviews (ADR-0079): stricter than
    an equal key. The titles must read the same once case, punctuation and
    spacing are folded and a suffix only ONE side carries is set aside
    ("Machine Learning (9 CFU)" / "Machine Learning"). Two different suffixes,
    a number, a stopword or a stem the key folds all fail: of the 15 key-equal
    pairs in the owner's vault, 7 were one note and 8 were two (2026-10-08),
    and the names did not tell the groups apart, "Token (NLP)" / "Token
    (Linguistica computazionale)" being one note and "Tipi di dati (Python)" /
    "(SQL)" two. A failed match goes to the judge, so it costs a call, never
    a note.
    """
    def split(t: str) -> tuple[str, str]:
        head = (t or "").strip()
        suffixes: list[str] = []
        for rx in (_PAREN_SUFFIX, _DASH_SUFFIX):
            m = rx.search(head)
            if m and head[:m.start()].strip():
                suffixes.insert(0, m.group())
                head = head[:m.start()]
        fold = lambda s: " ".join(_PUNCT.sub(" ", s.casefold()).split())
        return fold(head), fold(" ".join(suffixes))

    (ha, sa), (hb, sb) = split(a), split(b)
    return bool(ha) and ha == hb and (not sa or not sb or sa == sb)


def near_titles(
    t: str,
    titles: dict[str, str] | list[str],
    band: float = NEAR_BAND,
    *,
    lang: str | None = None,
) -> list[tuple[str, float]]:
    """Titles fuzzy-close to `t` — similar under the band but NOT key-equal.

    `titles` maps title -> anything (only keys are compared) or is a plain
    list. Returns [(title, ratio)] sorted by ratio desc. Key-equal entries are
    excluded: that is a different verdict (coercion, not review).
    """
    key = title_key(t, lang=lang)
    out: list[tuple[str, float]] = []
    for other in titles:
        other_key = title_key(other, lang=lang)
        if not other_key or other_key == key or numbers_differ(t, other):
            continue
        ratio = SequenceMatcher(None, key, other_key).ratio()
        if ratio >= band:
            out.append((other, ratio))
    return sorted(out, key=lambda kv: -kv[1])



# ---------------------------------------------------------------- the review flags
# The soft verdicts validate leaves on a landed note, `"; "`-joined in its
# `review:` key. The router routes on them (a near title to the dedup judge, a
# thin body to the expand worker) and Learn reads the near-title pair back off
# disk, so the text is composed and parsed here. Four writers spelled it by hand
# and three readers parsed it three ways (re-audit 2026-11-10, agent-runtime W1).

REVIEW_SEP = "; "
SHORT_FLAG = "snippet too short"

# The flag's grammar, as on disk since the C3 gate: a quote inside the title or
# the path ends the field, so such a flag reads as no near title at all, which
# is what every reader did before the readers were one.
_NEAR_TITLE_RE = re.compile(r"near_title candidate='([^']*)' path='([^']*)'(?: ratio=([0-9.]+))?")
# A flag ends at the next separator; a near-title flag is taken whole first,
# because the title it quotes may itself hold "; ".
_FLAG_RE = re.compile(
    rf"(?:^|{REVIEW_SEP})(near_title candidate='[^']*' path='[^']*'(?: ratio=[0-9.]+)?(?={REVIEW_SEP}|$)"
    rf"|.+?(?={REVIEW_SEP}|$))",
    re.S,
)


def near_title_flag(candidate: str, path: str, ratio: float | None = None) -> str:
    """The near-title verdict as `review:` text. No ratio for band 1, an equal
    key whose surfaces differ, which has no fuzzy score to carry."""
    flag = f"near_title candidate='{candidate}' path='{path}'"
    return flag if ratio is None else f"{flag} ratio={ratio:.2f}"


def review_flags(review: str) -> list[str]:
    """Every flag in a `review:` value (or one rejection's reason), in the order
    they were raised."""
    return [m.group(1) for m in _FLAG_RE.finditer(review or "")]


def near_title_of(review: str) -> tuple[str, str, float | None] | None:
    """(candidate, path, ratio) of the first near-title flag in `review`, None
    when it holds none. The ratio is None when the flag carries none or it does
    not parse as a number."""
    for flag in review_flags(review):
        m = _NEAR_TITLE_RE.search(flag)
        if m:
            try:
                ratio = float(m.group(3)) if m.group(3) else None
            except ValueError:
                ratio = None
            return m.group(1), m.group(2), ratio
    return None
