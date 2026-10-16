# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Centralized language resolution — single source of truth.

Multiple kernel modules (cooccurrence, overlay, keyphrase, cohesion,
prep_delegation) each grew their own copy of language-name mapping,
stopword loading and function-word detection, reaching into each other's
private module state to do it. This module reifies that logic into one
leaf: it imports NO other silica module, does zero LLM work, is fully
offline and deterministic, and never raises — every function degrades to
a usable value on failure.
"""
from __future__ import annotations

import re

from stop_words import StopWordError, get_stop_words


# Snowball-style language names ("italian") -> ISO codes ("it"). This module
# is the SOLE home of this mapping (overlay.py's former private copy was
# deleted; every other module resolves through here). Deliberately NOT a
# verbatim ISO-639-1 table: "norwegian" maps to "nb" (Bokmål), not the
# ambiguous macrolanguage code "no" — a root-fix for the stop_words package
# only shipping a Bokmål stopword list under "nb".
SNOWBALL_TO_ISO: dict[str, str] = {
    "arabic": "ar", "danish": "da", "dutch": "nl", "english": "en",
    "finnish": "fi", "french": "fr", "german": "de", "hungarian": "hu",
    "italian": "it", "norwegian": "nb", "portuguese": "pt", "romanian": "ro",
    "russian": "ru", "spanish": "es", "swedish": "sv",
}

# detect() also names chinese, which has neither a Snowball stemmer nor
# function words its argmax could count. It stays out of SNOWBALL_TO_ISO, whose
# keys settings.py offers as a co-occurrence stemmer, and has its code here.
CHINESE = "chinese"
ISO: dict[str, str] = {**SNOWBALL_TO_ISO, CHINESE: "zh"}

# Letters of any script — [a-zA-ZÀ-ÿ] silently blinded detect() to cyrillic
# and arabic (russian/arabic in SNOWBALL_TO_ISO were unreachable: zero tokens
# -> zero stopword hits -> english), and split romanian ș/ț and hungarian ő/ű
# words, which sit outside Latin-1.
_TOKEN_RE = re.compile(r"[^\W\d_]+")
# Han characters (unified ideographs, extension A, compatibility) and kana.
_HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
# Markup votes for no language. The stop_words English list has 1,333
# entries, among them single letters, "text", "begin", "right", "td" and "tr",
# so math symbols, LaTeX commands outside math delimiters, MinerU's HTML
# tables and its <details> figure captions (machine-written, often English)
# all voted English: on 2026-10-05, 15 of the 32 Italian lectures of the
# Machine Learning course read "english" on their first 4,000 characters.
#
# A single Latin letter votes only where it is a word: the French list holds
# c, d, j, l, m, n, s, t (elided c', d', j'...), which are variables in an
# exercise, and English and Romanian hold whole alphabets. Dropping every
# letter instead lost English "a" and "i" ("My car is a Panda" read French on
# "a" and "car") and Italian "e" ("IA e DL" has no other function word).
# A list that holds the alphabet was assembled without curation, and the
# English one also holds il, la, un, con, come, non, per and se: it votes
# only with the words no other language's list claims, or an Italian slide
# titled "one versus all" ties and the tie goes to English.
# Measured 2026-10-06: on their first 4,000 characters 31 of the 32 lectures
# read Italian (29 before; Lezione 7 casts 7 votes there and ties Danish, so
# nucleation's pin reads the whole chunk), all 32 on 2,000 and whole, and of
# 1,747 notes in the local vaults 43 Italian ones that read English now read
# Italian.
_ALPHABET_LIST = 15
_LETTER_WORDS = {lang: frozenset(letters) for lang, letters in {
    "english": "ai", "romanian": "aio", "italian": "aeio", "french": "ay", "spanish": "aeoy",
    "portuguese": "aeo", "hungarian": "aes", "danish": "i", "norwegian": "i", "swedish": "i", "dutch": "u",
}.items()}
_MARKUP_RE = re.compile(r"<details>.*?</details>|\$\$.*?\$\$|\$[^$\n]+\$|\\\(.*?\\\)|\\\[.*?\\\]"
                        r"|\\[A-Za-z]+|<[^<>\n]+>|!\[[^\]]*\]\([^)]*\)", re.S)

# Loaded stopword sets, cached per Snowball language name.
_stopwords_cache: dict[str, frozenset[str]] = {}


def stopwords_for(lang: str) -> frozenset[str]:
    """Return the stopword set for `lang`, lazily loaded and cached.

    Loads from the `stop_words` package (get_stop_words(iso)) for any
    Snowball language in SNOWBALL_TO_ISO. Unknown languages, or a
    StopWordError from the package -> empty frozenset (filters nothing).
    Never raises.
    """
    if lang in _stopwords_cache:
        return _stopwords_cache[lang]

    iso = SNOWBALL_TO_ISO.get(lang)
    result: frozenset[str]
    if iso is None:
        result = frozenset()
    else:
        try:
            result = frozenset(get_stop_words(iso))
        except StopWordError:
            result = frozenset()

    _stopwords_cache[lang] = result
    return result


_voters_cache: dict[str, frozenset[str]] = {}


def _words(lang: str) -> frozenset[str]:
    """`lang`'s stopwords without the single Latin letters that are not words in it."""
    keep = _LETTER_WORDS.get(lang, frozenset())
    return frozenset(w for w in stopwords_for(lang) if len(w) > 1 or not w.isascii() or w in keep)


def _voters(lang: str) -> frozenset[str]:
    """The stopwords that vote for `lang` in detect()."""
    if lang not in _voters_cache:
        words = _words(lang)
        if sum(len(w) == 1 for w in stopwords_for(lang)) >= _ALPHABET_LIST:
            others = frozenset().union(*(_words(m) for m in SNOWBALL_TO_ISO if m != lang))
            words = frozenset(w for w in words if w not in others or len(w) == 1)
        _voters_cache[lang] = words
    return _voters_cache[lang]


def detect(text: str) -> str:
    """Pick the language whose function-word set best matches `text`.

    Function-word-hit argmax over all languages with a non-empty
    stopwords_for() set (every SNOWBALL_TO_ISO key when the stop_words
    package works; degrades to en/it when it is broken — today's
    behavior). Empty text or no hits -> "english", deterministically.
    Text written mostly in Han characters is "chinese", decided on script
    before any word is counted.
    Candidate order is english-first, then SNOWBALL_TO_ISO insertion order,
    so max() (which keeps the first max on a tie) resolves any tie
    involving english to english, and other ties by that fixed order.

    ponytail: stopword-ratio classifier; swap to langdetect confined here
    if confusables (es/pt/fr/it) misfire on real prose.
    """
    plain = _MARKUP_RE.sub(" ", text)
    words = [w.lower() for w in _TOKEN_RE.findall(plain)]
    if not words:
        return "english"
    # Chinese writes no spaces, so to _TOKEN_RE a clause is one word that no
    # stoplist holds, and a Chinese note read "english" (2026-10-16). A Chinese
    # word is about two characters: more of them than Latin-script words is
    # Chinese, unless more kana than Han run between them, which is Japanese.
    han = len(_HAN_RE.findall(plain))
    if han > len(_KANA_RE.findall(plain)) and han / 2 > sum(1 for w in words if not _HAN_RE.search(w)):
        return CHINESE

    ordered = ["english"] + [name for name in SNOWBALL_TO_ISO if name != "english"]
    candidates = [name for name in ordered if stopwords_for(name)]
    if not candidates:
        return "english"

    return max(
        candidates,
        key=lambda name: sum(1 for w in words if w in _voters(name)),
    )


def resolve(lang: str, sample: str) -> str:
    """Resolve the 'auto' sentinel to a concrete language via detect(sample).

    Anything other than 'auto' is returned unchanged (sample ignored).
    """
    return detect(sample) if lang == "auto" else lang


def display_name(lang: str) -> str:
    """Human-readable form for the distiller {LANGUAGE} placeholder.

    e.g. "italian" -> "Italian".
    """
    return lang.capitalize()
