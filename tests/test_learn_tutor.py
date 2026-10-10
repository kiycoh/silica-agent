# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Learn as a tutor (2026-10-06 e2e on Qwen3.5-9B): a note without footnotes
yields points that are its paragraphs, lists and callouts; the model's items
join to the point their span quotes; a pointer to an unseen text is
stripped before the reader sees it; the grader is told the language."""
from __future__ import annotations

from silica.kernel.learn import grade, items

NOTE = """---
tags: [biologia]
---
# Fotosintesi
La fotosintesi è formata da due fasi:
- La fase luminosa, che produce ATP e NADPH nei tilacoidi
- Il ciclo di Calvin, che fissa la CO2 nello stroma

1. **Fase Luminosa (Light Reactions)**
2. **Ciclo di Calvin (Calvin Cycle)**

## Fase luminosa
Nella fase luminosa la **clorofilla** assorbe la luce e scinde l'acqua, liberando ossigeno come prodotto di scarto.

**Limitazioni:**
- Senza luce la fase si arresta e il ciclo di Calvin esaurisce presto ATP e NADPH.

## Ciclo di Calvin
Il ciclo di Calvin usa ATP e NADPH per ridurre la CO2 a zuccheri a tre atomi di carbonio. L'enzima RuBisCO catalizza il primo passo, legando la CO2 al ribulosio bisfosfato. Il ciclo si ripete tre volte per ogni molecola di gliceraldeide in uscita. A temperature alte la RuBisCO lega anche l'ossigeno, e questa fotorespirazione spreca parte del carbonio fissato. Le piante C4 separano nello spazio la fissazione iniziale per ridurre la fotorespirazione.

>[!question] Perché la fotorespirazione è uno spreco?
> Perché la RuBisCO lega l'ossigeno al posto della CO2 e il carbonio già fissato viene in parte rilasciato.
"""
PATH = "Bio/Fotosintesi.md"


def _points(tmp_vault):
    tmp_vault.note(PATH, NOTE)
    return items.key_points(PATH)


def test_a_list_joins_its_intro_and_bare_labels_are_not_points(tmp_vault):
    pts = _points(tmp_vault)
    texts = [p["text"] for p in pts]
    assert texts[0].startswith("La fotosintesi è formata da due fasi:") and "ciclo di Calvin, che fissa" in texts[0]
    # a list of labels is one enumeration point (asked as "what are the two
    # phases?"), never one point per label with a word of it blanked
    assert sum("Light Reactions" in t for t in texts) == 1
    assert any("Senza luce la fase si arresta" in t for t in texts), "a bullet holds content and is a point"
    assert all("**" not in t and "==" not in t for t in texts)
    assert all(p["self"] and p["line"] for p in pts)


def test_a_long_paragraph_is_split_at_sentences_and_a_callout_is_a_point(tmp_vault):
    pts = _points(tmp_vault)
    calvin = [p for p in pts if p["heading"] == "Ciclo di Calvin"]
    assert len(calvin) >= 3, "the 500-char paragraph becomes two points, the callout a third"
    assert all(len(p["text"]) <= items.MAX_PARA_CHARS + 120 for p in calvin)
    assert any(p["text"].startswith("Perché la fotorespirazione è uno spreco?") for p in calvin)


def test_points_and_asks_spread_over_the_headings_of_a_long_note(tmp_vault):
    words = iter("z" + chr(97 + i // 26) + chr(97 + i % 26) + "ola" for i in range(80))
    body = "# Lunga\n" + "".join(
        f"\n## Sezione {s}\n" + "".join(f"\nLa misura del {next(words)} dipende dal {next(words)} e cambia con il {next(words)} osservato.\n"
                                         for k in range(6))
        for s in range(4))
    tmp_vault.note("Lunga.md", body)
    pts = items.key_points("Lunga.md")
    assert len(pts) == items.MAX_CLAIMS_PER_NOTE
    assert {p["heading"] for p in pts} == {f"Sezione {s}" for s in range(4)}
    assert [p["note_line"] for p in pts] == sorted(p["note_line"] for p in pts)
    asks = items.ask_plan(pts, ["explain"])
    assert len(asks) == items.MAX_QUESTIONS
    asked = {next(p["heading"] for p in pts if p["i"] == a["point"]) for a in asks}
    assert asked == {f"Sezione {s}" for s in range(4)}, "eight asks in reading order covered two sections"


def test_cloze_blanks_a_bold_term_in_one_sentence(tmp_vault):
    tmp_vault.note(PATH, NOTE)
    light = next(p for p in items.key_points(PATH) if "clorofilla" in p["text"])
    it = items.cloze(PATH, point=light)
    assert it["answer"] == "clorofilla"
    assert it["q"] == "Fill the blank: Nella fase luminosa la ____ assorbe la luce e scinde l'acqua, liberando ossigeno come prodotto di scarto."
    assert items.grounded(it) and it["source"]["self"] is True
    label = next(p for p in items.key_points(PATH) if p["text"].startswith("Limitazioni:"))
    assert items.cloze(PATH, point=label)["answer"] != "Limitazioni", "a label is not a term to recall"


def _reply(*blocks: str) -> str:
    return "\n".join(f"ITEM {n}\nPATH: {PATH}\n{b}\nEND" for n, b in enumerate(blocks, 1))


def test_an_item_joins_the_point_its_span_quotes_and_rests_on_the_point(tmp_vault, monkeypatch):
    pts = _points(tmp_vault)
    light = next(p for p in pts if p["heading"] == "Fase luminosa" and "clorofilla" in p["text"])
    first = pts[0]
    asks = [{"point": first["i"], "objective": "recall", "kind": "recall"},
            {"point": light["i"], "objective": "explain", "kind": "explain"}]
    reply = _reply(
        # labelled with the first point's number, quoting the light-phase point
        f"KIND: explain\nLEVEL: 3\nOBJECTIVE: explain\nPOINT: {first['i']}\nANCHOR: Fase luminosa\n"
        "SPAN: Nella fase luminosa la clorofilla assorbe la luce e scinde l'acqua\n"
        "ANSWER: La clorofilla assorbe la luce e scinde l'acqua.\nQ: Da dove viene l'ossigeno della fotosintesi?\n"
        "CRITERIA: la scissione dell'acqua\nH1: Pensa a quale molecola viene scissa.",
        # a span the model retyped: not found, still kept on its point
        f"KIND: recall\nLEVEL: 1\nOBJECTIVE: recall\nPOINT: {light['i']}\nANCHOR: Fotosintesi\n"
        "SPAN: La fotosintesi si compone di due momenti distinti e separati tra loro\n"
        "ANSWER: Fase luminosa e ciclo di Calvin.\nQ: Quali sono le due fasi della fotosintesi?\n"
        "CRITERIA: fase luminosa; ciclo di Calvin\nH1: Una dipende dalla luce.")
    monkeypatch.setattr(items, "_llm", lambda p, m: (reply, "stop"))
    got = items.generate([{"path": PATH, "kind": "explain", "level": 3, "asks": asks, "objective": "explain"}])[PATH]
    by_point = {it["point"]: it for it in got}
    assert by_point[light["i"]]["q"].startswith("Da dove viene l'ossigeno")
    assert by_point[light["i"]]["claim"] == items.plain(light["text"])
    # the second item was relabelled away from the light point? no: its span quotes nothing,
    # so it keeps its own label, which the first item already took, and is dropped
    assert first["i"] not in by_point or by_point[first["i"]]["q"].startswith("Quali sono")


def test_an_unlocated_span_falls_back_to_the_point_and_self_span_is_the_whole_point(tmp_vault, monkeypatch):
    pts = _points(tmp_vault)
    first = pts[0]
    asks = [{"point": first["i"], "objective": "recall", "kind": "recall"}]
    reply = _reply(
        f"KIND: recall\nLEVEL: 1\nOBJECTIVE: recall\nPOINT: {first['i']}\nANCHOR: Nessuna\n"
        "SPAN: una frase che la nota non contiene in nessuna forma riconoscibile\n"
        "ANSWER: Fase luminosa e ciclo di Calvin.\nQ: Quali sono le due fasi della fotosintesi?\n"
        "CRITERIA: fase luminosa; ciclo di Calvin\nH1: Una dipende dalla luce.")
    monkeypatch.setattr(items, "_llm", lambda p, m: (reply, "stop"))
    got = items.generate([{"path": PATH, "kind": "explain", "level": 3, "asks": asks, "objective": "recall"}])[PATH]
    assert len(got) == 1
    it = got[0]
    assert it["anchor"] == first["heading"] and it["source"] == {"leaf": PATH, "line": first["line"], "self": True}
    assert it["span"] == it["claim"] == items.plain(first["text"]), "the grader grades against the whole point"


def test_the_model_may_downgrade_explain_to_recall_and_is_recorded_as_recall(tmp_vault, monkeypatch):
    pts = _points(tmp_vault)
    first = pts[0]
    asks = [{"point": first["i"], "objective": "explain", "kind": "explain"}]
    reply = _reply(
        f"KIND: explain\nLEVEL: 3\nOBJECTIVE: recall\nPOINT: {first['i']}\nANCHOR: Fotosintesi\n"
        "SPAN: La fotosintesi è formata da due fasi\nANSWER: Fase luminosa e ciclo di Calvin.\n"
        "Q: Quali sono le due fasi della fotosintesi?\nCRITERIA: le due fasi\nH1: Una dipende dalla luce.")
    monkeypatch.setattr(items, "_llm", lambda p, m: (reply, "stop"))
    it = items.generate([{"path": PATH, "kind": "explain", "level": 3, "asks": asks, "objective": "explain"}])[PATH][0]
    assert (it["objective"], it["kind"], it["level"]) == ("recall", "recall", 1)


def test_the_prompt_names_the_language_and_shows_only_the_asked_points(tmp_vault):
    pts = _points(tmp_vault)
    asks = [{"point": pts[1]["i"], "objective": "explain", "kind": "explain"}]
    block = items.note_block(1, {"path": PATH, "kind": "explain", "level": 3, "asks": asks, "objective": "explain"})
    assert "LANGUAGE: Italian" in block
    assert f"POINT {pts[1]['i']} (heading: " in block and f"POINT {pts[0]['i']} (heading: " not in block
    assert "untrusted" in items.GENERATE_PROMPT.casefold()


def test_a_pointer_to_an_unseen_text_is_stripped_from_the_question():
    reply = ("ITEM 1\nPATH: x.md\nKIND: recall\nQ: Qual è la definizione di fotosintesi fornita nel testo?\n"
             "ANSWER: a\nEND\nITEM 2\nPATH: x.md\nKIND: recall\nQ: Come si genera il significato secondo la pragmatica?\n"
             "ANSWER: a\nEND")
    qs = [it["q"] for it in items.parse_model_items(reply)]
    assert qs == ["Qual è la definizione di fotosintesi?", "Come si genera il significato secondo la pragmatica?"]
    strip = items._SOURCE_REF_RE.sub
    assert strip("", "Cosa significa trasmettere informazioni secondo la definizione data?") == "Cosa significa trasmettere informazioni?"
    assert strip("", "Cosa afferma il test secondo la definizione di Turing?") == "Cosa afferma il test secondo la definizione di Turing?"
    tail = ("ITEM 1\nPATH: x.md\nKIND: recall\nQ: Come cambia l'attenzione nella comunicazione secondo il punto "
            "sull'intenzione comunicativa?\nANSWER: a\nEND")
    assert items.parse_model_items(tail)[0]["q"] == "Come cambia l'attenzione nella comunicazione?"


def test_the_grader_is_told_the_language_of_the_reply(monkeypatch):
    seen = []
    monkeypatch.setattr(grade, "_llm", lambda p, n: (seen.append(p), ("GRADE: recalled\nPRECISE: no\nRUBRIC: x\nFEEDBACK: y\n", "stop"))[1])
    grade.grade({"q": "Perché la fotorespirazione è uno spreco?", "level": 3, "criteria": "c", "claim": "c", "span": "s"},
                production="perché la rubisco lega l'ossigeno e il carbonio si perde", conf=70, hints=0, told=False)
    assert "LANGUAGE: Italian" in seen[0]
    assert 'never "the production"' in grade.GRADER_PROMPT
