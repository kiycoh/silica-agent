# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Item generation (learn-suite D15): the mechanical kinds read a footnoted
claim line and a Relations bullet, every item rests on a span the leaf
actually holds, and the session payload never carries the answer key."""
from __future__ import annotations

import json
import re

import pytest

from silica.kernel.learn import items

NOTE = """---
title: Margine geometrico
AI: true
---

# Margine geometrico

> Il margine geometrico e' la distanza del punto piu' vicino dall'iperpiano.[^Lezione-11]

## Definizione

Il margine geometrico limita l'errore atteso del classificatore.[^Lezione-11]
Una riga senza nota a pie' di pagina.

## Relations
- derived from [[Iperpiano]]: il margine e' definito sulla distanza dal piano
- [[Bound VC]] applies this (Lezione 11): il bound usa il margine

## Sources
[[sources/Lezione 11]]
[^Lezione-11]: [[sources/Lezione 11]]
"""

LEAF = """# Lezione 11

Introduzione.

Il margine geometrico limita l'errore atteso del classificatore, come
mostrato nel teorema. Il margine geometrico e' la distanza del punto piu'
vicino dall'iperpiano.
"""


@pytest.fixture
def vault(tmp_vault):
    tmp_vault.note("ML/Margine geometrico.md", NOTE)
    tmp_vault.note("sources/Lezione 11.md", LEAF)
    tmp_vault.note("ML/Iperpiano.md", "# Iperpiano\n\nUn piano.\n")
    return tmp_vault


def test_claims_are_the_footnoted_lines_with_their_leaf_and_heading(vault):
    claims = items.claims_of("ML/Margine geometrico.md")
    assert [c["heading"] for c in claims] == ["Margine geometrico", "Definizione"]
    assert claims[1]["text"].startswith("Il margine geometrico limita")
    assert "[^" not in claims[1]["text"]
    assert claims[1]["leaf"] == "sources/Lezione 11.md"
    # the span is located in the leaf, by line
    assert claims[1]["line"] == 5
    assert "limita l'errore" in claims[1]["span"]


def test_cloze_blanks_the_head_term_and_keeps_the_answer_out_of_the_cue(vault):
    it = items.cloze("ML/Margine geometrico.md")
    assert it["kind"] == "cloze" and it["level"] == 1
    assert "____" in it["q"]
    assert it["answer"].lower() in ("margine geometrico", "margine")
    assert it["answer"].lower() not in it["q"].lower()
    assert it["anchor"] and it["source"]["leaf"] == "sources/Lezione 11.md"
    assert it["source"]["line"] > 0


def test_a_cloze_that_leaves_no_words_around_its_blank_is_not_asked(vault):
    # a bold label alone on its line ("**Oggetti matematici**:") blanked to
    # "____:" has nothing to cue the recall; the next claim is asked instead
    vault.note("ML/Oggetti.md", "# Oggetti\n\n**Oggetti matematici**:[^L]\n\n"
               "Un **tensore** generalizza vettori e matrici a più dimensioni.[^L]\n\n## Sources\n[^L]: [[sources/Lezione 11]]\n")
    it = items.cloze("ML/Oggetti.md")
    assert it is None or it["q"] != "Fill the blank: ____:"
    if it is not None:
        assert len(it["q"].replace("____", "").split()) >= 4   # "Fill the blank:" plus the cue's words


def test_structure_reads_a_relations_bullet_in_either_direction(vault):
    got = items.structure("ML/Margine geometrico.md")
    assert got["kind"] == "structure" and got["level"] == 3
    assert "[[Iperpiano]]" in got["q"] or "Iperpiano" in got["q"]
    assert "derived from" in got["answer"] and "distanza" in got["answer"]
    assert "derived from" not in got["q"] and "distanza" not in got["q"]
    other = items.structure("ML/Margine geometrico.md", skip={"Iperpiano"})
    assert "Bound VC" in other["q"] and "applies" in other["answer"]


def test_grounding_drops_an_item_whose_span_is_not_in_the_leaf(vault):
    it = items.cloze("ML/Margine geometrico.md")
    assert items.grounded(it)
    fake = dict(it, source={"leaf": "sources/Lezione 11.md", "line": 3}, span="Questa frase non esiste nella lezione.")
    assert not items.grounded(fake)
    bad_anchor = dict(it, anchor="#Nessuna sezione")
    assert not items.grounded(bad_anchor)


def test_hints_come_from_the_graph_and_the_outline_without_a_model(vault, monkeypatch):
    from silica.kernel.report import learner

    monkeypatch.setattr(learner, "prerequisites_map", lambda: {"ml/margine geometrico": ["ML/Iperpiano.md"]})
    hints = items.hints_for("ML/Margine geometrico.md")
    assert hints[0].startswith("Prerequisite")
    assert "Iperpiano" in hints[0]
    assert "Definizione" in hints[1] and "Relations" in hints[1]


def test_session_payload_carries_no_answer_key(vault):
    """G5: what the reader (or the REPL model) sees before answering."""
    picked = [items.cloze("ML/Margine geometrico.md"), items.structure("ML/Margine geometrico.md")]
    payload = json.dumps(items.cue_payload(picked), ensure_ascii=False)
    assert "answer" not in payload and "span" not in payload and "claim" not in payload
    assert "rubric" not in payload and "criteria" not in payload
    assert not re.search(r"limita l'errore|distanza dal piano", payload)
    assert '"q"' in payload and '"hints"' in payload and '"source"' in payload


def test_model_reply_parses_by_markers_and_drops_ungrounded_items(vault):
    reply = """ITEM 1
PATH: ML/Margine geometrico.md
KIND: explain
LEVEL: 3
ANCHOR: Definizione
Q: Perche' il margine geometrico limita l'errore atteso?
CRITERIA: names the bound; links margin to generalization
ANSWER: Perche' il bound sull'errore decresce col margine.
SPAN: Il margine geometrico limita l'errore atteso del classificatore
H1: Pensa al teorema che lega margine ed errore.
END
ITEM 2
PATH: ML/Margine geometrico.md
KIND: recall
LEVEL: 1
ANCHOR: Definizione
Q: Cosa misura il margine geometrico?
CRITERIA: distance to the closest point
ANSWER: La distanza del punto piu' vicino dall'iperpiano.
SPAN: Una frase inventata che il leaf non contiene affatto.
END
"""
    parsed = items.parse_model_items(reply)
    assert len(parsed) == 2 and parsed[0]["kind"] == "explain" and parsed[0]["hints"] == ["Pensa al teorema che lega margine ed errore."]
    kept = [it for it in parsed if items.grounded(it)]
    assert [it["kind"] for it in kept] == ["explain"]
    assert kept[0]["source"]["leaf"] == "sources/Lezione 11.md" and kept[0]["source"]["line"] == 5


def test_build_session_uses_one_batched_call_and_falls_back_to_mechanical_kinds(vault, monkeypatch):
    """D15: one generation call per session; a dropped or missing model item
    backfills from grounded cloze/structure; nothing is invented to keep n."""
    calls = []

    def fake_llm(prompt, max_tokens):
        calls.append(prompt)
        assert "untrusted" in prompt.casefold() and "ML/Margine geometrico.md" in prompt
        return ("ITEM 1\nPATH: ML/Margine geometrico.md\nKIND: explain\nLEVEL: 3\nANCHOR: Definizione\n"
                "Q: Perche' il margine limita l'errore?\nCRITERIA: the bound\nANSWER: Per il bound.\n"
                "SPAN: Il margine geometrico limita l'errore atteso del classificatore\nH1: Pensa al bound.\nEND\n"
                "ITEM 2\nPATH: ML/Iperpiano.md\nKIND: recall\nLEVEL: 1\nANCHOR: Iperpiano\n"
                "Q: Cos'e'?\nCRITERIA: x\nANSWER: y\nSPAN: frase che non esiste\nEND\n"), "stop"

    monkeypatch.setattr(items, "_llm", fake_llm)
    picked = [
        {"path": "ML/Margine geometrico.md", "kind": "explain", "level": 3, "pool": "due"},
        {"path": "ML/Iperpiano.md", "kind": "recall", "level": 1, "pool": "due"},
    ]
    built = items.build_session(picked)
    assert len(calls) == 1
    kinds = {it["path"]: it["kind"] for it in built}
    assert kinds["ML/Margine geometrico.md"] == "explain"
    assert built[0]["hints"][0] == "Pensa al bond." or built[0]["hints"][0] == "Pensa al bound."
    assert "ML/Iperpiano.md" not in kinds          # no span, no claims to fall back on: dropped
    # without a model the mechanical kinds carry the session
    monkeypatch.setattr(items, "_llm", lambda *a, **k: ("", "stop"))
    built = items.build_session([{"path": "ML/Margine geometrico.md", "kind": "explain", "level": 3, "pool": "due"}])
    assert built and built[0]["kind"] in ("cloze", "structure") and built[0]["grader"] == "self"


def test_a_note_without_footnotes_grounds_on_its_own_lines(tmp_vault):
    """A vault written before the footnote lane has no leaf to cite: the
    claim is the note's own line, located by number, so the reveal still
    shows where the claim sits and the session is not empty."""
    tmp_vault.note("Old/Entropia.md", "# Entropia\n\nL'entropia misura l'incertezza media di una variabile casuale discreta.\n\nUna riga corta.\n")
    claims = items.claims_of("Old/Entropia.md")
    assert claims and claims[0]["leaf"] == "Old/Entropia.md" and claims[0]["line"] == 3 and claims[0]["self"] is True
    it = items.cloze("Old/Entropia.md")
    assert it and it["source"] == {"leaf": "Old/Entropia.md", "line": 3, "self": True}
    assert "____" in it["q"] and it["answer"].lower() == "entropia"
    assert items.grounded(it)
    model_item = {"path": "Old/Entropia.md", "kind": "recall", "level": 1, "q": "?", "answer": "x", "anchor": "Entropia",
                  "span": "misura l'incertezza media di una variabile casuale discreta", "source": {"leaf": "", "line": 0}}
    assert items.grounded(model_item) and model_item["source"]["leaf"] == "Old/Entropia.md" and model_item["source"]["self"] is True
    assert "CLAIMS:" in items.note_block(1, {"path": "Old/Entropia.md", "kind": "recall", "level": 1})
