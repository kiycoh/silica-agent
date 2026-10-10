# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The claim inventory (study-mode D31): sections, ids stable per section,
grounding, the closed vocabularies and the relation claims rendered from the
skeleton's edges. The model is a fake keyed on each stage's tag, and it
rewords a section every time it decomposes it, as a real model does, so a
section decomposed twice cannot keep its ids by accident."""
from __future__ import annotations

import json
import re

import pytest

from silica.kernel import outline
from silica.kernel.learn import inventory

LEAF = """---
source_file: Lezione 03.pdf
source_sha256: 0f
---

# Lezione 03

Docente Mario Rossi

Corso di Analisi 1, anno accademico 2026

Dipartimento di Matematica

Orario del ricevimento: martedì

# Continuità

Una funzione f è continua in x0 se il limite di f per x che tende a x0 vale f(x0).

## Esempio

La funzione x^2 è continua su tutto l'asse reale.

## Teorema di Weierstrass

Una funzione continua su un intervallo chiuso e limitato ammette massimo e minimo.

## Esempio

La funzione 1/x non ammette massimo sull'intervallo (0, 1].
"""

LEAF_PATH = "sources/Analisi 1 - Lezione 03.md"

IDEAS = [
    {"title": "Funzione continua", "section": "Continuità", "claim": "Definizione di continuità.",
     "depends_on": []},
    {"title": "Teorema di Weierstrass", "section": "Teorema di Weierstrass",
     "claim": "Esistenza di massimo e minimo.",
     "depends_on": [{"title": "Funzione continua", "relation": "derives", "why": "Usa la continuità."}]},
]


class FakeModel:
    """One reply per stage, keyed on the system prompt's tag."""

    def __init__(self, ideas=IDEAS, gap_skips=({"section": "Esempio", "reason": "an example"},)):
        self.ideas = ideas
        self.gap_skips = list(gap_skips)
        self.calls: list[str] = []
        self.decomposed: list[str] = []
        self.times: dict[str, int] = {}
        self.overrides: dict[str, list[dict]] = {}
        self.fail: set[str] = set()
        self.assigned: dict[str, str] = {}

    def __call__(self, system: str, user: str, *, max_tokens: int, raw: bool = False) -> dict:
        if system.startswith(outline.STAGE_A_TAG):
            self.calls.append("outline")
            return {"lesson_title": "Continuità", "spine": [i["title"] for i in self.ideas], "ideas": self.ideas}
        if system.startswith(outline.STAGE_GAP_TAG):
            self.calls.append("gap")
            return {"ideas": [], "skips": self.gap_skips}
        if system.startswith(inventory.DECOMPOSE_TAG):
            section = re.search(r"^SECTION: (.+)$", user, re.M).group(1)
            self.calls.append("decompose")
            self.decomposed.append(section)
            if section in self.fail:
                raise ValueError("no JSON after retry")
            if section in self.overrides:
                return {"claims": self.overrides[section]}
            n = self.times[section] = self.times.get(section, 0) + 1
            body = user.split("TEXT:\n", 1)[1]
            lines = [l for l in body.splitlines() if l.strip() and not l.startswith("#")]
            idea = next((i["title"] for i in self.ideas if i["section"] == section), "")
            # The wording moves on every decomposition: v1, v2, ...
            return {"claims": [{"text": f"{l.strip()} (v{n})", "span": l.strip(), "idea": idea,
                                "kind": "definition", "role": "core", "qualifiers": []} for l in lines]}
        if system.startswith(inventory.ASSIGN_TAG):
            self.calls.append("assign")
            ids = re.findall(r"^- ([0-9a-f]{12}) \|", user, re.M)
            return {"assign": {i: self.assigned.get(i, "") for i in ids}}
        raise AssertionError(f"unexpected stage: {system[:40]}")


@pytest.fixture
def leaf(tmp_vault):
    tmp_vault.note(LEAF_PATH, LEAF)
    return tmp_vault


def _by_section(inv: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for c in inv["claims"]:
        out.setdefault(c["section"], []).append(c)
    return out


# ------------------------------------------------------------------ sections --
def test_a_repeated_heading_gets_an_ordinal():
    names = [s["section"] for s in inventory.sections(LEAF, untitled="Lezione 03")]
    assert names == ["Lezione 03", "Continuità", "Esempio", "Teorema di Weierstrass", "Esempio (2)"]


def test_a_section_over_the_limit_is_cut_at_a_blank_line():
    para = "Parola " * 140  # ~1k characters, one paragraph
    text = "# Lungo\n\n" + "\n\n".join(f"{i} {para}" for i in range(9))
    parts = inventory.sections(text, untitled="x")
    assert [p["section"] for p in parts] == ["Lungo", "Lungo (2)"]
    assert [p["cut"] for p in parts] == [True, True]
    assert not any(s["cut"] for s in inventory.sections(LEAF, untitled="x"))
    assert all(len(p["text"]) <= inventory.MAX_SECTION_CHARS for p in parts)
    # cut between two paragraphs, never inside one
    assert parts[0]["text"].rstrip().endswith(para.rstrip())
    assert re.match(r"\d ", parts[1]["text"].lstrip())


def test_text_before_any_heading_is_named_after_the_leaf():
    names = [s["section"] for s in inventory.sections("Un paragrafo.\n\n# Dopo\n\nTesto.", untitled="Lezione 03")]
    assert names == ["Lezione 03", "Dopo"]


def test_a_heading_inside_a_code_fence_is_not_a_section():
    text = "# Codice\n\n```python\n# commento\nx = 1\n```\n"
    assert [s["section"] for s in inventory.sections(text, untitled="x")] == ["Codice"]


def test_a_title_only_heading_yields_no_claims_and_no_call(tmp_vault):
    tmp_vault.note("sources/T.md", "# Capitolo\n\n## Definizione\n\nUna retta è un insieme di punti allineati tra loro.\n")
    model = FakeModel(ideas=[{"title": "Retta", "section": "Definizione", "claim": "c", "depends_on": []}],
                      gap_skips=[{"section": "Capitolo", "reason": "title"}])
    inv = inventory.build("sources/T.md", language="Italian", ask=model)
    assert model.decomposed == ["Definizione"]
    assert [s["section"] for s in inv["sections"]] == ["Capitolo", "Definizione"]


# ----------------------------------------------------------------- the build --
def test_build_stores_the_inventory_under_the_index_keyed_by_leaf_stem(leaf):
    inv = inventory.build(LEAF_PATH, language="Italian", ask=FakeModel())
    stored = inventory.path_for(LEAF_PATH)
    assert stored.name == "Analisi 1 - Lezione 03.json" and stored.parent.name == "inventory"
    assert json.loads(stored.read_text()) == inv == inventory.load(LEAF_PATH)
    assert inv["leaf"] == LEAF_PATH
    assert len(inv["leaf_sha256"]) == 64


def test_an_unchanged_leaf_is_not_rebuilt(leaf):
    first = inventory.build(LEAF_PATH, language="Italian", ask=FakeModel())
    model = FakeModel()
    again = inventory.build(LEAF_PATH, language="Italian", ask=model)
    assert model.calls == []
    assert again == first


def test_ids_hash_leaf_stem_section_and_normalized_text(leaf):
    inv = inventory.build(LEAF_PATH, language="Italian", ask=FakeModel())
    c = next(c for c in inv["claims"] if c["section"] == "Esempio (2)")
    assert c["id"] == inventory.claim_id("Analisi 1 - Lezione 03", "Esempio (2)", c["text"])
    assert c["id"] == inventory.claim_id("Analisi 1 - Lezione 03", "Esempio (2)", "  " + c["text"].upper() + ". ")
    assert re.fullmatch(r"[0-9a-f]{12}", c["id"])


def test_ids_are_stable_across_a_rebuild_of_an_unchanged_section(leaf):
    model = FakeModel()  # one model across both builds: a second decomposition would reword
    first = _by_section(inventory.build(LEAF_PATH, language="Italian", ask=model))
    leaf.note(LEAF_PATH, LEAF.replace("ammette massimo e minimo", "ha massimo e minimo"))
    second = _by_section(inventory.build(LEAF_PATH, language="Italian", ask=model))
    for name in ("Continuità", "Esempio", "Esempio (2)"):
        assert [c["id"] for c in second[name]] == [c["id"] for c in first[name]]


def test_editing_one_section_decomposes_only_that_section_even_when_ideas_are_renamed(leaf):
    first = inventory.build(LEAF_PATH, language="Italian", ask=FakeModel())
    leaf.note(LEAF_PATH, LEAF.replace("ammette massimo e minimo", "ha massimo e minimo"))
    renamed = [dict(IDEAS[0], title="Funzioni continue"),
               dict(IDEAS[1], depends_on=[dict(IDEAS[1]["depends_on"][0], title="Funzioni continue")])]
    model = FakeModel(ideas=renamed)
    cont = next(c for c in first["claims"] if c["section"] == "Continuità")
    model.assigned = {cont["id"]: "Funzioni continue"}
    model.times = {"Teorema di Weierstrass": 1}  # the model has seen it once: the next wording is v2
    inv = inventory.build(LEAF_PATH, language="Italian", ask=model)
    assert model.decomposed == ["Teorema di Weierstrass"]
    assert model.calls.count("assign") == 1
    kept = next(c for c in inv["claims"] if c["id"] == cont["id"])
    assert kept["text"] == cont["text"] and kept["idea"] == "Funzioni continue"
    # the old wording of the edited section is archived, so old ledger lines resolve
    old = {c["id"] for c in first["claims"] if c["section"] == "Teorema di Weierstrass" and c["kind"] != "relation"}
    assert old and old <= {c["id"] for c in inv["archived"]}
    assert not old & {c["id"] for c in inv["claims"]}


def test_an_assignment_naming_no_idea_keeps_the_old_idea_when_it_survives(leaf):
    first = inventory.build(LEAF_PATH, language="Italian", ask=FakeModel())
    leaf.note(LEAF_PATH, LEAF.replace("ammette massimo e minimo", "ha massimo e minimo"))
    inv = inventory.build(LEAF_PATH, language="Italian", ask=FakeModel())  # assigns "" to every id
    cont = next(c for c in first["claims"] if c["section"] == "Continuità")
    assert next(c for c in inv["claims"] if c["id"] == cont["id"])["idea"] == "Funzione continua"


def test_a_failed_section_is_left_undone_and_the_next_build_asks_only_for_it(leaf):
    model = FakeModel()
    model.fail = {"Esempio (2)"}
    inv = inventory.build(LEAF_PATH, language="Italian", ask=model)
    assert "Esempio (2)" not in {s["section"] for s in inv["sections"]}
    assert "Continuità" in _by_section(inv)
    retry = FakeModel()
    inventory.build(LEAF_PATH, language="Italian", ask=retry)
    assert retry.decomposed == ["Esempio (2)"]
    assert "outline" not in retry.calls  # the leaf did not change: the skeleton stands


def test_a_reply_without_a_claims_list_leaves_the_section_undone(leaf):
    model = FakeModel()
    real = model.__call__

    def ask(system, user, **kw):
        if system.startswith(inventory.DECOMPOSE_TAG) and "SECTION: Esempio (2)" in user:
            return {"lesson_title": "x"}  # parsed JSON, wrong shape
        return real(system, user, **kw)

    inv = inventory.build(LEAF_PATH, language="Italian", ask=ask)
    assert "Esempio (2)" not in {s["section"] for s in inv["sections"]}


def test_a_reply_of_the_wrong_shape_is_asked_once_more(leaf):
    # live 2026-10-05: a bare array of claims came back, which the JSON
    # decoder reads as its first claim alone
    model = FakeModel()
    real = model.__call__
    wrong = {"n": 0}

    def ask(system, user, **kw):
        if system.startswith(inventory.DECOMPOSE_TAG) and "SECTION: Esempio (2)" in user and not wrong["n"]:
            wrong["n"] += 1
            return {"text": "La funzione 1/x non ammette massimo", "span": "x", "kind": "claim"}
        return real(system, user, **kw)

    inv = inventory.build(LEAF_PATH, language="Italian", ask=ask)
    assert "Esempio (2)" in {s["section"] for s in inv["sections"]}
    assert _by_section(inv)["Esempio (2)"]


def test_the_decomposition_prompt_carries_the_language_rule(leaf):
    seen = []
    model = FakeModel()

    def ask(system, user, **kw):
        if system.startswith(inventory.DECOMPOSE_TAG):
            seen.append(system)
        return model(system, user, **kw)

    inventory.build(LEAF_PATH, language="Italian", ask=ask)
    assert seen and all("Write every claim in Italian." in s for s in seen)


# ------------------------------------------------------- vocabulary, grounding --
def test_a_kind_or_role_outside_the_vocabulary_reads_claim_and_supporting(leaf):
    model = FakeModel()
    model.overrides["Esempio (2)"] = [{"text": "1/x non ha massimo", "span": "La funzione 1/x non ammette massimo",
                                      "kind": "theorem", "role": "key", "idea": "", "qualifiers": []}]
    inv = inventory.build(LEAF_PATH, language="Italian", ask=model)
    c = _by_section(inv)["Esempio (2)"][0]
    assert (c["kind"], c["role"]) == ("claim", "supporting")


def test_the_model_cannot_produce_a_relation_claim(leaf):
    model = FakeModel()
    model.overrides["Esempio (2)"] = [{"text": "1/x non ha massimo", "span": "La funzione 1/x non ammette massimo",
                                      "kind": "relation", "role": "core", "idea": "", "qualifiers": []}]
    c = _by_section(inventory.build(LEAF_PATH, language="Italian", ask=model))["Esempio (2)"][0]
    assert c["kind"] == "claim"


def test_a_span_missing_from_the_leaf_is_dropped_with_its_reason(leaf):
    model = FakeModel()
    model.overrides["Esempio (2)"] = [
        {"text": "1/x non ha massimo", "span": "La funzione 1/x non ammette massimo", "kind": "example",
         "role": "illustrative", "idea": "", "qualifiers": ["(0, 1]"]},
        {"text": "1/x è derivabile", "span": "La funzione 1/x è derivabile ovunque nel suo dominio naturale",
         "kind": "claim", "role": "core", "idea": "", "qualifiers": []},
    ]
    inv = inventory.build(LEAF_PATH, language="Italian", ask=model)
    kept = _by_section(inv)["Esempio (2)"]
    assert [c["text"] for c in kept] == ["1/x non ha massimo"]
    assert kept[0]["span"]["line"] == LEAF.splitlines().index("La funzione 1/x non ammette massimo sull'intervallo (0, 1].") + 1
    assert kept[0]["qualifiers"] == ["(0, 1]"]
    assert {"text": "1/x è derivabile", "reason": "span not in leaf", "section": "Esempio (2)",
            "span": "La funzione 1/x è derivabile ovunque nel suo dominio naturale"} in inv["dropped"]


def test_a_claim_restating_the_header_is_dropped_as_apparatus(tmp_vault):
    tmp_vault.note("sources/H.md", "# Analisi 1\n\nDocente Mario Rossi\n\nCorso di laurea in Matematica\n\n"
                   "Anno accademico 2026\n\nLezione numero tre\n\nPagina del corso\n\n"
                   "## Limiti\n\nIl limite di una somma è la somma dei limiti.\n")
    model = FakeModel(ideas=[{"title": "Limite della somma", "section": "Limiti", "claim": "c", "depends_on": []}])
    model.overrides["Analisi 1"] = [{"text": "Il docente è Mario Rossi", "span": "Docente Mario Rossi",
                                     "kind": "claim", "role": "core", "idea": "", "qualifiers": []}]
    model.overrides["Limiti"] = [{"text": "Il limite di una somma è la somma dei limiti",
                                  "span": "Il limite di una somma è la somma dei limiti", "kind": "mechanism",
                                  "role": "core", "idea": "Limite della somma", "qualifiers": []}]
    inv = inventory.build("sources/H.md", language="Italian", ask=model)
    assert [c["text"] for c in inv["claims"]] == ["Il limite di una somma è la somma dei limiti"]
    assert inv["dropped"] == [{"text": "Il docente è Mario Rossi", "reason": "header apparatus", "section": "Analisi 1"}]


def test_every_heading_is_claimed_or_skipped(leaf):
    model = FakeModel(gap_skips=[])  # the coverage pass answers nothing for "Esempio"
    inv = inventory.build(LEAF_PATH, language="Italian", ask=model)
    claimed = {outline._sec_key(i["section"]) for i in inv["ideas"]} | {outline._sec_key(s["section"]) for s in inv["skips"]}
    for h in outline.source_headings(LEAF):
        assert outline._sec_key(h) in claimed
    assert any(s["section"] == "Esempio" and s["reason"] for s in inv["skips"])


# -------------------------------------------------------------- relation claims --
def test_relation_claims_are_rendered_from_the_edges_with_no_model_call(leaf):
    model = FakeModel()
    inv = inventory.build(LEAF_PATH, language="Italian", ask=model)
    rel = [c for c in inv["claims"] if c["kind"] == "relation"]
    assert [c["text"] for c in rel] == ["Teorema di Weierstrass derived from Funzione continua"]
    c = rel[0]
    assert (c["role"], c["span"], c["section"], c["idea"]) == ("supporting", None, "Teorema di Weierstrass",
                                                                "Teorema di Weierstrass")
    # 5 sections, 5 decompositions: no call rendered the relation
    assert model.calls.count("decompose") == 5


def test_a_relation_claim_takes_the_first_section_its_idea_names(leaf):
    ideas = [IDEAS[0], dict(IDEAS[1], section="Continuità, Teorema di Weierstrass")]
    inv = inventory.build(LEAF_PATH, language="Italian", ask=FakeModel(ideas=ideas))
    assert next(c for c in inv["claims"] if c["kind"] == "relation")["section"] == "Continuità"


def test_the_language_is_detected_over_the_whole_leaf(tmp_vault):
    # Lezione 7 of the Machine Learning course opens with 4,000 characters of
    # display math: detection on that window has nothing to read (2026-10-05).
    math = "\n\n".join(f"$$\nb i a s (\\hat {{\\theta}} _ {{{i}}}) = E [ \\hat \\theta ] - \\theta\n$$" for i in range(80))
    tmp_vault.note("sources/M.md", "# Stimatori\n\n" + math + "\n\nLo stimatore è corretto quando il suo bias è nullo "
                   "e la sua varianza tende a zero con il numero dei campioni.\n")
    seen = []
    model = FakeModel(ideas=[{"title": "Stimatore corretto", "section": "Stimatori", "claim": "c", "depends_on": []}])

    def ask(system, user, **kw):
        seen.append(system)
        return model(system, user, **kw)

    inventory.build("sources/M.md", ask=ask)
    assert any("Write every claim in Italian." in s for s in seen)


SLIDES = """# Algebra lineare

Operazioni tra matrici:

La somma di due matrici si calcola elemento per elemento.

Norma:

La norma di un vettore misura la sua lunghezza.

## Ortogonalità, ortonormalità

Due vettori sono ortogonali se il loro prodotto scalare è nullo.
"""


@pytest.mark.parametrize("named, expected", [
    ("Operazioni tra matrici", "Algebra lineare"),          # a slide title MinerU wrote as a plain line
    ("Norma, Ortogonalità, ortonormalità", "Algebra lineare"),  # several named: the first one found
    ("Ortogonalità e Ortonormalità", "Ortogonalità, ortonormalità"),  # the heading reworded
])
def test_a_relation_finds_the_section_its_idea_names_when_it_is_not_a_heading(tmp_vault, named, expected):
    tmp_vault.note("sources/S.md", SLIDES)
    ideas = [{"title": "Prodotto scalare", "section": "Ortogonalità, ortonormalità", "claim": "c", "depends_on": []},
             {"title": "Norma vettoriale", "section": named, "claim": "c",
              "depends_on": [{"title": "Prodotto scalare", "relation": "derives", "why": "w"}]}]
    inv = inventory.build("sources/S.md", language="Italian", ask=FakeModel(ideas=ideas))
    rel = [c for c in inv["claims"] if c["kind"] == "relation"]
    assert [c["section"] for c in rel] == [expected]


FORMULAS = """# Ortogonalità

Vettori a norma unitaria:

$$
\\boldsymbol {x} \\in \\mathbb {R} ^ {n} \\qquad \\| \\boldsymbol {x} \\| _ {2} = 1
$$

Fine.
"""


@pytest.mark.parametrize("span", [
    # the label and its display block, which MinerU puts four lines apart
    "Vettori a norma unitaria: $$\\boldsymbol {x} \\in \\mathbb {R} ^ {n} \\qquad \\| \\boldsymbol {x} \\| _ {2} = 1$$",
    # the same LaTeX without MinerU's spacing
    "\\boldsymbol{x} \\in \\mathbb{R}^{n} \\qquad \\|\\boldsymbol{x}\\|_{2} = 1",
])
def test_a_formula_span_grounds_across_blank_lines_and_latex_spacing(span):
    line, text = inventory.ground(span, FORMULAS)
    # the label's line; for the bare formula, its block ("$$" or the formula line)
    assert line == 3 if span.startswith("Vettori") else line in (5, 6)
    assert "mathbb" in text


def test_a_formula_rewritten_in_unicode_does_not_ground():
    assert inventory.ground("x ∈ ℝ^n, ||x||_2 = 1", FORMULAS) == (0, "")


def test_the_decomposition_prompt_asks_for_latex_as_written():
    assert "LaTeX exactly as the TEXT writes it" in inventory.DECOMPOSE


def test_the_decomposition_budget_has_room_for_text_and_span_per_claim(tmp_vault):
    # live 2026-10-05: 4 of 6 retried decompositions were cut at
    # finish=length with a budget of one token per section character, since
    # every claim carries its text and a verbatim span
    body = "Il gradiente indica la direzione di massima crescita della funzione. " * 45  # ~3,100 characters
    tmp_vault.note("sources/B.md", "# Gradiente\n\n" + body + "\n")
    budgets = []
    model = FakeModel(ideas=[{"title": "Gradiente", "section": "Gradiente", "claim": "c", "depends_on": []}])

    def ask(system, user, *, max_tokens, **kw):
        if system.startswith(inventory.DECOMPOSE_TAG):
            budgets.append(max_tokens)
        return model(system, user, max_tokens=max_tokens, **kw)

    inventory.build("sources/B.md", language="Italian", ask=ask)
    assert budgets and budgets[0] >= 2 * len(body)
