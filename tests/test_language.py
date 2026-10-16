"""Tests for silica.kernel.text.language — centralized language resolution.

Leaf module: no LLM, offline, deterministic, never raises. Consumers
(cooccurrence, overlay, keyphrase, cohesion, prep_delegation) are rewired to
this module in later tasks — these tests pin the module's own contract.
"""
from __future__ import annotations

import silica.kernel.text.language as language

IT = "Il gatto è sul tavolo e la casa è bella, ma non è facile trovare la strada giusta per la stazione."
EN = "The cat is on the table and the house is very nice, but it is not easy to find the right way to the station."
FR = "Le chat est sur la table et la maison est très belle, mais il n'est pas facile de trouver le bon chemin vers la gare."
DE = "Die Katze ist auf dem Tisch und das Haus ist sehr schön, aber es ist nicht leicht den richtigen Weg zum Bahnhof zu finden."
ES = "El gato está sobre la mesa y la casa es muy bonita, pero no es fácil encontrar el camino correcto hacia la estación."


# --- detect -------------------------------------------------------------

def test_detect_italian():
    assert language.detect(IT) == "italian"


def test_detect_english():
    assert language.detect(EN) == "english"


def test_detect_french():
    assert language.detect(FR) == "french"


def test_detect_german():
    assert language.detect(DE) == "german"


def test_detect_spanish():
    assert language.detect(ES) == "spanish"


ZH = "机器学习是人工智能的一个分支，它让计算机从数据中学习规律，而不是依靠明确编写的规则。"


def test_detect_chinese():
    assert language.detect(ZH) == "chinese"


def test_detect_chinese_under_frontmatter_and_english_terms():
    """Before 2026-10-16 a Chinese note read "english" on its frontmatter: a
    Han clause is one token to the word count, in no stoplist."""
    note = ("---\ntags: [ml, svm]\nrelated: null\n---\n# 支持向量机 (SVM)\n"
            "支持向量机是一种监督学习算法。kernel trick 把数据映射到高维空间，使线性不可分的数据变得可分。")
    assert language.detect(note) == "chinese"


def test_a_chinese_word_in_italian_prose_stays_italian():
    assert language.detect(IT + " Il carattere 学习 vuol dire studiare.") == "italian"


def test_japanese_is_not_chinese():
    assert language.detect("機械学習は人工知能の一分野であり、データから規則を学ぶことを目的としている。") != "chinese"


def test_chinese_keeps_its_surface_forms_and_a_misspelt_language_still_raises():
    import pytest
    from silica.kernel.text.text import stem_word

    assert stem_word("学习", lang="chinese") == "学习"
    with pytest.raises(KeyError):
        stem_word("gatti", lang="italain")


def test_detect_russian():
    # Cyrillic: zero tokens under a latin-only token regex, so russian could
    # never win detection and a russian store silently froze as english.
    ru = ("Кошка сидит на столе и дом очень красивый, но не так легко "
          "найти правильную дорогу к вокзалу.")
    assert language.detect(ru) == "russian"


def test_detect_arabic():
    ar = ("القطة على الطاولة والبيت جميل جدا ولكن ليس من السهل أن تجد "
          "الطريق الصحيح إلى المحطة.")
    assert language.detect(ar) == "arabic"


def test_detect_empty_string_is_english():
    assert language.detect("") == "english"


def test_detect_no_signal_text_is_english():
    # code/formula snippet: no function-word hits in any language
    assert language.detect("x = mc^2; y = f(x, z0);") == "english"


def test_detect_ties_between_two_non_english_languages_break_by_insertion_order():
    # "op" is a stopword shared by ONLY danish and dutch (no other language in
    # SNOWBALL_TO_ISO, including english). A single hit ties those two at 1
    # and leaves every other candidate at 0. Per detect()'s documented rule,
    # ties are broken by SNOWBALL_TO_ISO's fixed insertion order (english
    # first, excluded here since it can't tie); danish precedes dutch there,
    # so danish must win — pinning that rule against silent reordering.
    assert "op" in language.stopwords_for("danish")
    assert "op" in language.stopwords_for("dutch")
    order = list(language.SNOWBALL_TO_ISO)
    assert order.index("danish") < order.index("dutch")

    assert language.detect("op op op") == "danish"


def test_detect_is_deterministic():
    assert language.detect(IT) == language.detect(IT)
    first = language.detect(FR)
    for _ in range(5):
        assert language.detect(FR) == first


# --- resolve --------------------------------------------------------------

def test_resolve_auto_detects_from_sample():
    assert language.resolve("auto", IT) == "italian"


def test_resolve_non_auto_passes_through_ignoring_sample():
    assert language.resolve("french", IT) == "french"


# --- stopwords_for ----------------------------------------------------------

def test_stopwords_for_italian_nonempty():
    assert language.stopwords_for("italian")
    assert "di" in language.stopwords_for("italian") or "e" in language.stopwords_for("italian")


def test_stopwords_for_unknown_language_is_empty_frozenset():
    result = language.stopwords_for("klingon")
    assert result == frozenset()


def test_stopwords_for_norwegian_nonempty():
    # Root-fix: the installed stop_words package has no "no" (raises
    # StopWordError); it expects "nb" (Bokmal). With the old "no" mapping
    # this silently returned frozenset() even with a healthy package.
    assert language.stopwords_for("norwegian")


def test_stopwords_for_is_cached(monkeypatch):
    monkeypatch.setattr(language, "_stopwords_cache", {})
    calls = []

    def _tracking_get_stop_words(iso):
        calls.append(iso)
        return ["a", "b"]

    monkeypatch.setattr(language, "get_stop_words", _tracking_get_stop_words)
    language.stopwords_for("english")
    language.stopwords_for("english")
    assert calls == ["en"], "second call must hit the cache, not the package"


def test_display_name_italian():
    assert language.display_name("italian") == "Italian"


def test_display_name_english():
    assert language.display_name("english") == "English"

