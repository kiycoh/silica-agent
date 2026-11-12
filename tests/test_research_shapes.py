# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A research shape's fields on the notes /nucleate makes (ADR-0158 point 10).

A deep research's part is stamped with its shape. An investigation's notes are
one claim each, with who asserts it, the kind of source, the claim's status and
its date, and whom it is about; a literature's study notes carry the study's
identifier, version, design, population, outcome and retraction. The model
names each value in a typed field, code keeps only the allowed ones, and the
literature join (identifier, version, retraction) is code's.

The model is stood in for (`outline._default_ask`); no call leaves the process.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from silica.config import CONFIG
from silica.kernel import outline as ol
from silica.kernel.base import frontmatter
from silica.kernel.base.noteshape import shape_fields, shape_value
from silica.kernel.write.ops import Op
from silica.sources import web_research as wr
from tests.test_quote_citations import _nucleate, _read


def _part(name: str, body: str, bank: dict, trace: list[str], *, shape: str, folder: str = "probe") -> str:
    """A part as deep_research writes it: quote ids, its shape, its leaf."""
    from silica.driver import DRIVER

    bound, sources, audit = wr.bind_citations(body, wr._collect_sources(trace), bank, quote_ids=True)
    rel = wr.unique_inbox_path(name, folder=folder)
    DRIVER.create(rel, wr.build_note(name, bound, sources, lanes=audit, cites_quotes=True, shape=shape))
    wr.write_leaf(rel, trace, bank=dict(bank))
    return rel


def _fm(rel: str) -> dict:
    data, _raw, _body = frontmatter.split(_read(rel))
    return data or {}


def _ask(outline_json: dict, bodies: dict[str, str], calls: list):
    def ask(system, user, *, max_tokens, **kw):
        calls.append(system)
        if system.startswith(ol.STAGE_A_TAG):
            return json.loads(json.dumps(outline_json))
        if system.startswith(ol.STAGE_GAP_TAG):
            return {"ideas": [], "skips": [{"section": "Sources", "reason": "apparatus"}]}
        if system.startswith(ol.STAGE_BODIES_TAG):
            titles = re.findall(r'^- "(.+?)" \(section: ', user.split("IDEAS:\n", 1)[1], re.M)
            return {"bodies": {t: bodies[t] for t in titles if t in bodies}}
        # Stage B, asked only for a shape that may reach into the vault: it
        # disobeys on purpose, joining every new idea to every existing note.
        rows = user.split("EXISTING NOTES:\n", 1)[1].split("\n\nNEW SOURCE IDEAS:\n")
        existing = [r.split(" | ")[1] for r in rows[0].splitlines()]
        new = [r.split(" | ")[0] for r in rows[1].splitlines()]
        return {"edges": [{"from": n, "to": e, "relation": "contrasts", "why": "both are named in the filing"}
                          for n in new for e in existing]}
    return ask


# ------------------------------------------------------------------ the values


def test_a_field_keeps_only_a_value_its_key_allows():
    assert shape_value("status", "Alleged") == "alleged"
    assert shape_value("status", "probably guilty") == ""
    assert shape_value("source_kind", "a blog") == ""
    assert shape_value("status_as_of", "2025-03") == "2025-03"
    assert shape_value("status_as_of", "last spring") == ""
    assert shape_value("about", "[[Jane Roe]]  and\nher firm") == "Jane Roe and her firm"
    assert shape_value("retracted", True) == "true"
    assert shape_value("version", {"x": 1}) == ""
    assert shape_fields("investigation", {"status": "charged", "publish": "true"}) == {
        "asserted_by": "", "source_kind": "", "status": "charged", "status_as_of": "", "about": ""}
    assert shape_fields("cluster", {"status": "charged"}) == {}


def test_an_op_carries_only_a_shapes_own_keys_whatever_a_reply_says():
    """A distiller's reply parses into Op too: `props` must not be a door
    into the frontmatter."""
    from silica.kernel.write.ops_io import parse_ops

    [op] = parse_ops([{"op": "write", "path": "R/X.md", "heading": "X", "source_basename": "p.md",
                       "props": {"status": "charged", "retracted": True, "publish": "true", "AI": False}}])
    assert op.op.value == "write"  # a bool value does not turn the op into a skip
    assert op.props == {"status": "charged", "retracted": "true"}
    assert Op(op="write", path="R/Y.md", heading="Y", source_basename="p.md", props={"AI": "x"}).props is None


def test_the_fields_come_back_from_the_frontmatter_as_they_went_in(tmp_vault):
    from silica.kernel.write.bulk import render_write

    props = {"asserted_by": "The Court: Southern District", "source_kind": "primary", "status": "charged",
             "status_as_of": "2025-03-01", "about": "Jane Roe"}
    note = render_write(Op(op="write", path="R/C.md", heading="C", source_basename="p.md", hub="R",
                           snippet="A claim.", props=props))
    data, _raw, _body = frontmatter.split(note)
    assert {k: data[k] for k in props} == props  # a date stays text, a colon stays in its value
    lit = render_write(Op(op="write", path="R/S.md", heading="S", source_basename="p.md", hub="R",
                          snippet="A study.", props={"retracted": "false", "version": "preprint"}))
    assert frontmatter.split(lit)[0]["retracted"] == "false"


def test_a_shaped_prompt_retries_with_the_shaped_schema_and_any_other_with_its_own(monkeypatch):
    """The constrained retry must ask for `fields` exactly when the prompt
    did, and send every other prompt the schema it always sent."""
    import types

    sent: list = []
    replies = iter(["no json here", json.dumps({"lesson_title": "L", "spine": [], "ideas": []})] * 2)

    def call_llm(**kw):
        sent.append(kw["response_schema"])
        return types.SimpleNamespace(text=next(replies), finish_reason="stop")

    monkeypatch.setattr("silica.model.providers.get_provider",
                        lambda *a, **kw: types.SimpleNamespace(call_llm=call_llm))
    monkeypatch.delenv("SILICA_OUTLINE_CACHE", raising=False)
    plain = ol.STAGE_A.format(lang="English", rels="r")
    ol._default_ask(plain, "src", max_tokens=100)
    ol._default_ask(plain + ol.shape_rule("investigation"), "src", max_tokens=100)

    assert sent == [None, ol.OutlineReply, None, ol.ShapedOutlineReply]


# ---------------------------------------------------------------- investigation


INV_BANK = {
    "Q1": wr.Quote(url="https://court.example/filing", quote="The indictment charges John Doe with fraud.", why="w"),
    "Q2": wr.Quote(url="https://news.example/roe", quote="Jane Roe denied any part in the scheme.", why="w"),
}
INV_BODY = ("## The filing\n\nThe indictment charges John Doe with fraud [Q1]. "
            "Jane Roe, named in the same filing, denied any part in it [Q2].\n")
INV_TRACE = ["Source: https://court.example/filing\nThe indictment charges John Doe with fraud.",
             "Source: https://news.example/roe\nJane Roe denied any part in the scheme."]
INV_OUTLINE = {
    "lesson_title": "The filing", "spine": ["Fraud charge against John Doe", "Denial of involvement"],
    "ideas": [
        {"title": "Fraud charge against John Doe", "section": "The filing",
         "claim": "John Doe is charged with fraud [Q1].", "depends_on": [],
         "fields": {"asserted_by": "The indictment", "source_kind": "primary", "status": "charged",
                    "status_as_of": "2025-03-01", "about": "John Doe"}},
        # Same section, another person, and it disobeys: an edge between the
        # two claims, a status no list holds, a link in a field.
        {"title": "Denial of involvement", "section": "The filing",
         "claim": "Jane Roe denies any part in the scheme [Q2].",
         "depends_on": [{"title": "Fraud charge against John Doe", "relation": "contrasts",
                         "why": "both are named in the filing"}],
         "fields": {"asserted_by": "Jane Roe", "source_kind": "reporting", "status": "probably innocent",
                    "status_as_of": "soon", "about": "[[Jane Roe]]"}},
    ],
}
INV_BODIES = {"Fraud charge against John Doe": "The indictment charges John Doe with fraud [Q1].",
              "Denial of involvement": "Jane Roe denied any part in the scheme [Q2]."}


def test_an_investigation_writes_one_claim_per_note_with_its_fields_and_no_co_mention_link(tmp_vault, monkeypatch):
    tmp_vault.note("Research/Jane Roe.md", "---\ntype: Note\n---\n\n# Jane Roe\n\n> A person in the vault.\n\nNotes on her.\n")
    rel = _part("Who is charged", INV_BODY, INV_BANK, INV_TRACE, shape="investigation")
    assert _fm(rel)["shape"] == "investigation"
    calls: list = []
    _nucleate(monkeypatch, [rel], _ask(INV_OUTLINE, INV_BODIES, calls))

    # Two claims of one section, two notes: the bodies stage wrote each.
    assert any(c.startswith(ol.STAGE_BODIES_TAG) for c in calls)
    charge, denial = "Research/Fraud charge against John Doe.md", "Research/Denial of involvement.md"
    assert "charges John Doe" in _read(charge) and "Jane Roe denied" not in _read(charge)
    assert {k: _fm(charge)[k] for k in ("asserted_by", "source_kind", "status", "status_as_of", "about")} == {
        "asserted_by": "The indictment", "source_kind": "primary", "status": "charged",
        "status_as_of": "2025-03-01", "about": "John Doe"}
    # What no list holds is "", never the model's sentence; a link is a name.
    assert {k: _fm(denial)[k] for k in ("status", "status_as_of", "about")} == {
        "status": "", "status_as_of": "", "about": "Jane Roe"}
    # No edge between claims about two people, and stage B is never asked.
    assert not any(c.startswith(ol.STAGE_B_TAG) for c in calls)
    for note, other in ((charge, "Denial of involvement"), (denial, "Fraud charge against John Doe")):
        related = " ".join(_fm(note).get("related") or [])
        assert other not in related and "Jane Roe" not in related
        assert "## Relations" not in _read(note)
    assert "Fraud charge" not in _read("Research/Jane Roe.md")
    # The claims' markers are keyed to the leaf as for any part (point 9).
    assert "fraud[^probe-Who-is-charged-Q1]" in _read(charge)


def test_a_later_status_files_the_old_one_under_superseded(tmp_vault, monkeypatch):
    """Run 1 says charged; run 2, a later research, says convicted. The note
    keeps one current status and the old one, dated and attributed, under
    `## Superseded`."""
    first = {"lesson_title": "T", "spine": ["Fraud charge against John Doe"], "ideas": [INV_OUTLINE["ideas"][0]]}
    one = _part("Who is charged", INV_BODY, INV_BANK, INV_TRACE, shape="investigation")
    _nucleate(monkeypatch, [one], _ask(first, INV_BODIES, []))

    later = json.loads(json.dumps(first))
    later["ideas"][0]["fields"].update(status="convicted-not-final", status_as_of="2026-01-15",
                                       asserted_by="The trial court")
    bank = {"Q1": wr.Quote(url="https://court.example/verdict", quote="The jury found John Doe guilty.", why="w")}
    two = _part("Who was convicted", "## The filing\n\nA jury found John Doe guilty of fraud [Q1].\n", bank,
                ["Source: https://court.example/verdict\nThe jury found John Doe guilty."],
                shape="investigation", folder="probe2")
    _nucleate(monkeypatch, [two], _ask(later, {"Fraud charge against John Doe": "A jury found John Doe guilty of fraud [Q1]."}, []))

    note = _read("Research/Fraud charge against John Doe.md")
    data = _fm("Research/Fraud charge against John Doe.md")
    assert (data["status"], data["status_as_of"], data["asserted_by"]) == (
        "convicted-not-final", "2026-01-15", "The trial court")
    superseded = note.split("## Superseded", 1)[1]
    assert ("- Status: charged, as of 2025-03-01, asserted by The indictment (primary). "
            "Followed by: convicted-not-final, as of 2026-01-15.") in superseded


def test_a_status_is_never_replaced_by_nothing_and_survives_a_repeated_claim():
    from silica.kernel.write.contested import apply_shape_props

    note = "---\nstatus: charged\nstatus_as_of: '2025-03-01'\nasserted_by: X\nsource_kind: primary\nabout: Y\n---\n\n# C\n\nBody.\n"
    blank = {"asserted_by": "", "source_kind": "", "status": "", "status_as_of": "", "about": ""}
    assert apply_shape_props(note, blank) == note
    moved = apply_shape_props(note, {**blank, "status": "dismissed", "status_as_of": "2026-02-01"})
    assert "status: dismissed" in moved and "## Superseded" in moved
    assert apply_shape_props(moved, {**blank, "status": "dismissed"}) == moved  # once filed, a rerun adds nothing


# ------------------------------------------------------------------- literature


DOI = "https://doi.org/10.1/lnp"
LIT_BANK = {
    "Q1": wr.Quote(url=DOI, quote="Lipid nanoparticles carried the mRNA into cells.", why="w"),
    "Q2": wr.Quote(url="https://blog.example/lnp", quote="LNPs are fat bubbles.", why="w"),
}
LIT_BODY = ("## Delivery\n\nA trial found lipid nanoparticles carried the mRNA into cells [Q1]. "
            "A blog calls them fat bubbles [Q2].\n")
LIT_TRACE = [json.dumps([{"title": "LNP study", "url": DOI, "year": 2021, "type": "preprint",
                          "retracted": True, "content": "Lipid nanoparticles carried the mRNA into cells."}]),
             f"Source: {DOI}\nLNP study\n\nLipid nanoparticles carried the mRNA into cells.",
             "Source: https://blog.example/lnp\nLNPs are fat bubbles."]
LIT_OUTLINE = {
    "lesson_title": "Delivery", "spine": ["LNP delivery trial", "Unverified study", "Lipid nanoparticles"],
    "ideas": [
        {"title": "LNP delivery trial", "section": "Delivery", "claim": "LNPs carried mRNA into cells [Q1].",
         "fields": {"identifier": DOI, "version": "published", "design": "randomised trial",
                    "population": "adult volunteers", "outcome": "cell uptake", "retracted": "false"}},
        # An identifier its own markers do not resolve to: a concept, no fields.
        {"title": "Unverified study", "section": "Delivery", "claim": "A blog describes LNPs [Q2].",
         "fields": {"identifier": "https://doi.org/10.9/elsewhere", "design": "cohort"}},
        {"title": "Lipid nanoparticles", "section": "Delivery", "claim": "LNPs are lipid shells [Q2]."},
    ],
}
LIT_BODIES = {"LNP delivery trial": "A trial found lipid nanoparticles carried the mRNA into cells [Q1].",
              "Unverified study": "A blog calls them fat bubbles [Q2].",
              "Lipid nanoparticles": "A blog calls lipid nanoparticles fat bubbles [Q2]."}


def test_a_literature_study_note_carries_the_studys_fields_and_code_joins_the_retraction(tmp_vault, monkeypatch):
    rel = _part("How LNPs deliver mRNA", LIT_BODY, LIT_BANK, LIT_TRACE, shape="literature")
    _nucleate(monkeypatch, [rel], _ask(LIT_OUTLINE, LIT_BODIES, []))

    study = _fm("Research/LNP delivery trial.md")
    assert {k: study[k] for k in ("identifier", "version", "design", "population", "outcome", "retracted")} == {
        "identifier": DOI, "version": "preprint", "design": "randomised trial", "population": "adult volunteers",
        "outcome": "cell uptake", "retracted": "true"}  # the hit's type and retraction, not the model's
    for concept in ("Unverified study", "Lipid nanoparticles"):
        assert "identifier" not in _fm(f"Research/{concept}.md")


def test_without_a_scholarly_hit_the_retraction_is_unknown_not_false(tmp_vault, monkeypatch):
    trace = LIT_TRACE[1:]  # the abstract was fetched as a page: no scholarly result on file
    rel = _part("How LNPs deliver mRNA", LIT_BODY, LIT_BANK, trace, shape="literature")
    _nucleate(monkeypatch, [rel], _ask(LIT_OUTLINE, LIT_BODIES, []))

    study = _fm("Research/LNP delivery trial.md")
    assert (study["identifier"], study["version"], study["retracted"]) == (DOI, "published", "")


@pytest.mark.parametrize("shape", ["anchored", "cluster", ""])
def test_a_shape_without_fields_writes_none_and_asks_for_none(tmp_vault, monkeypatch, shape):
    rel = _part("How LNPs deliver mRNA", LIT_BODY, LIT_BANK, LIT_TRACE, shape=shape)
    calls: list = []
    _nucleate(monkeypatch, [rel], _ask(LIT_OUTLINE, LIT_BODIES, calls))

    assert ol.FIELDS_TAG not in calls[0]
    assert "identifier" not in _fm("Research/LNP delivery trial.md")
    assert Path(CONFIG.vault_path, "Research/LNP delivery trial.md").exists()
