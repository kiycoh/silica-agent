"""ADR-0110 step 1: a note the person wrote, read against the vault by rule.

The vault below is a small firm's: two clients, a chronology, a statute, a
superseded memo, a contested one, a contract with a quotable clause. Every
rule finding of point 5 is planted once, and the pool of point 3 is checked
to keep the other client out.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from silica.kernel.check import GRADES, ORDER, blocks, pool, rules, run
from silica.kernel.text import dates

CHRONO = """---
matter: "2022-009"
client: Northwind Logistics
---
# Chronology

| Date | Event |
| :--- | :--- |
| 2022-04-19 | Distribution Agreement executed |
| 2022-06-02 | First delivery under Schedule 1 |
| 2022-06-30 | Acceptance certificate issued under Schedule 3 |
"""
MEMO_OLD = """---
matter: "2022-009"
superseded_by: "[[Memo 2025 on limitation]]"
---
Time runs from delivery.
"""
MEMO_NEW = """---
matter: "2022-009"
contested: true
---
The six-year period runs from acceptance. Limitation now expires 19 April 2028.
"""
CONTRACT = """---
client: Northwind Logistics
---
Clause 11.3. The indemnity at clause 11.2 is not subject to the limitation of liability in clause 12, and shall survive termination of this Agreement without limit of time.
"""
OTHER = """---
cliente: Ferrovie Nord Adriatico
---
Penale dell'uno per cento al giorno, ridotta ex art. 1384 c.c., decreto 39/2010.
"""
SHARED = """# Clause bank, data processing

Standard contractual clauses under Decision 2010/87/EU, repealed by Decision (EU) 2021/914.
"""


@pytest.fixture
def firm(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    for rel, text in {
        "Matters/Chronology.md": CHRONO,
        "Research/Memo 2023 on limitation.md": MEMO_OLD,
        "Research/Memo 2025 on limitation.md": MEMO_NEW,
        "Contracts/Northwind Meridian Distribution Agreement 2022.md": CONTRACT,
        "Clienti/Contratto Ferrovie.md": OTHER,
        "Precedents/Clause bank, data processing.md": SHARED,
        "GRAPH_REPORT.md": "Northwind 19 April 2028 acceptance six years\n",
        "silica/Matters/Chronology.md": CHRONO,
    }.items():
        (v / rel).parent.mkdir(parents=True, exist_ok=True)
        (v / rel).write_text(text, encoding="utf-8")
    return v


def _draft(v: Path, body: str, fm: str = 'matter: "2022-009"\n') -> str:
    (v / "Drafts").mkdir(exist_ok=True)
    (v / "Drafts/Opinion.md").write_text(f"---\n{fm}---\n\n# Opinion\n\n{body}\n", encoding="utf-8")
    return "Drafts/Opinion.md"


def _kinds(report) -> list[tuple[str, str]]:
    return [(f["kind"], f["words"]) for f in report["findings"]]


# ---------------------------------------------------------------- blocks

def test_a_block_is_a_paragraph_item_row_or_footnote_and_abbreviations_do_not_split_it():
    text = ("---\nmatter: x\n---\n# Heading that is long enough to count\n\n"
            "Under art. 1384 c.c. and Cass. n. 123/2020 the penalty is reduced by the court.\n\n"
            "- first item that has six words at least\n  continued here\n"
            "| 2022-06-30 | Acceptance certificate issued under Schedule 3 |\n| --- | --- |\n\n"
            "```\ncode that has many many words inside it\n```\n\n$$\nx = y + z and more words\n$$\n\n"
            "Short one.\n\nA claim with a footnote reference here[^1] for the reader.\n\n"
            "[^1]: The source is [[Memo 2025 on limitation]] at page four.\n")
    out, defs = blocks.split(text)
    assert [(b.kind, b.line) for b in out] == [("paragraph", 6), ("item", 8), ("row", 10),
                                               ("paragraph", 23), ("footnote", 25)]
    assert out[1].text.endswith("continued here")
    assert out[3].footnotes == ["1"] and "Memo 2025" in defs["1"]


# ---------------------------------------------------------------- dates and identifiers

def test_dates_need_a_year_and_periods_name_their_event():
    found = [d for d, _s, _e in dates.find_dates("19 April 2028, 1° marzo 2026, 2022-06-30, 12.3.2021, 3.5, 19 April")]
    assert found == ["2028-04-19", "2026-03-01", "2022-06-30", "2021-03-12"]
    assert [(p["n"], p["unit"], p["event"]) for p in dates.find_periods(
        "the six-year period runs from acceptance. sei anni dall'accettazione; "
        "six years from the date on which the cause accrued")] == [(6, "years", "acceptance"), (6, "years", "accettazione")]
    assert dates.add_period("2024-02-29", 1, "years") == "2025-02-28"


def test_identifiers_follow_point_4_and_match_without_their_letters():
    got = dict(rules.identifiers("art. 2409-bis, d.lgs. 39/2010, Decision 2010/87/UE, case C-311/18, "
                                 "on 12.3.2021, 1.250.000 euro, version 3.5, a 30-day notice, Q1"))
    assert got == {"2409-bis": "2409", "39/2010": "39/2010", "2010/87/UE": "2010/87", "C-311/18": "311/18"}
    assert rules.identifiers("2010/87/EU")[0][1] == got["2010/87/UE"]


# ---------------------------------------------------------------- the pool

def test_the_pool_keeps_another_client_out_and_takes_one_hop_of_party_values(firm):
    notes, _files = pool.walk(str(firm))
    p = pool.build("Matters/Chronology.md", notes, pool.DEFAULT_PARTY_KEYS, "silica", set())
    assert "Contracts/Northwind Meridian Distribution Agreement 2022.md" in p.paths   # client via one hop
    assert "Clienti/Contratto Ferrovie.md" not in p.paths                           # another party, Italian key
    assert "Precedents/Clause bank, data processing.md" in p.paths                  # shared material
    assert "GRAPH_REPORT.md" not in p.paths and "silica/Matters/Chronology.md" not in p.paths
    assert p.scope == "party" and p.values == {"2022-009", "northwind logistics"}
    named = pool.build("Matters/Chronology.md", notes, pool.DEFAULT_PARTY_KEYS, "silica", {"Clienti/Contratto Ferrovie.md"})
    assert "Clienti/Contratto Ferrovie.md" in named.paths                           # the person named it


def test_vault_yaml_replaces_the_party_keys_and_a_vault_without_them_is_searched_whole(firm, tmp_path):
    (firm / "vault.yaml").write_text("check_party_keys: [project]\n", encoding="utf-8")
    assert pool.party_keys(str(firm)) == ("project",)
    notes, _ = pool.walk(str(firm))
    p = pool.build("Matters/Chronology.md", notes, ("project",), "", set())
    assert p.scope == "whole" and "Clienti/Contratto Ferrovie.md" in p.paths


# ---------------------------------------------------------------- the rule findings

def test_every_rule_finding_on_one_draft(firm):
    path = _draft(firm, "\n\n".join([
        "Limitation on the Northwind claim expires on 19 April 2028, six years from acceptance under Schedule 3.",
        "As the [[Memo 2023 on limitation]] explains, time runs from the first delivery of the goods.",
        "The clause bank still relies on Decision 2010/87/UE and on decree 99/2099 for transfers.",
        "The directions hearing listed for 17 February 2026 was adjourned at the defendant's request.",
        "Under the Northwind Meridian Distribution Agreement 2022 clause 11.3 reads \"The indemnity at clause 11.2 "
        "is not subject to the limitation of liability in clause 12, and shall survive termination for six years.\"",
        "Elsewhere we wrote \"shall survive termination of this Agreement without limit of time\" in the summary.",
        "Our [[Nonexistent advice note]] covers the remaining points of the claim in detail.",
    ]))
    r = run.check_note(path, str(firm), write_dir="silica")
    kinds = _kinds(r)
    assert ("inconsistent", "19 April 2028") in kinds
    inc = next(f for f in r["findings"] if f["kind"] == "inconsistent")
    assert "2022-04-19" in inc["say"] and "2022-06-30" in inc["say"] and "30 June 2028" in inc["say"]
    assert ("source status", "Memo 2023 on limitation") in kinds
    assert ("not in the vault", "99/2099") in kinds and ("not in the vault", "2010/87/UE") not in kinds
    assert ("date not found", "17 February 2026") in kinds
    assert any(k == "misquoted" for k, _ in kinds)
    assert any(k == "quotation without its source" for k, _ in kinds)
    assert ("not in the vault", "Nonexistent advice note") in kinds
    assert not any("Ferrovie" in (f.get("at") or "") + f["say"] for f in r["findings"])
    assert {f["grade"] for f in r["findings"]} <= set(ORDER)
    assert all(GRADES[f["kind"]] == f["grade"] for f in r["findings"])


def test_a_derived_date_is_a_state_with_its_arithmetic(firm):
    path = _draft(firm, "On acceptance the claim runs until 30 June 2028, six years from acceptance under Schedule 3.")
    r = run.check_note(path, str(firm), write_dir="silica")
    assert not r["findings"]
    assert any(s["state"] == "derived date" and "2022-06-30" in s["say"] for s in r["states"])


def test_a_plain_blockquote_is_the_writers_own_and_a_lead_in_attributes_one(firm):
    q = "> The indemnity at clause 11.2 is not subject to the limitation of liability in clause 12."
    plain = run.check_note(_draft(firm, "My own summary follows here for the reader.\n\n" + q), str(firm), write_dir="silica")
    assert not any(f["kind"] in ("misquoted", "not found", "quotation without its source") for f in plain["findings"])
    led = run.check_note(_draft(firm, "The Northwind Meridian Distribution Agreement 2022 provides:\n\n"
                                + q.replace("clause 12", "clause 13")), str(firm), write_dir="silica")
    assert any(f["kind"] == "misquoted" for f in led["findings"])


def test_absence_is_decided_only_with_both_floors_and_otherwise_waits(firm):
    path = _draft(firm, "Photosynthesis converts light energy into chemical energy inside the chloroplast.")
    no_vectors = run.check_note(path, str(firm), write_dir="silica", search=lambda q, k: [])
    assert not no_vectors["findings"] and no_vectors["states"][0]["state"] == "not checked"
    far = run.check_note(path, str(firm), write_dir="silica", search=lambda q, k: [],
                         dense=lambda t, p: 0.1, dense_floor=0.5)
    assert [(f["kind"], f["grade"]) for f in far["findings"]] == [("not found", "warning")]
    near = run.check_note(path, str(firm), write_dir="silica", dense=lambda t, p: 0.9, dense_floor=0.5,
                          search=lambda q, k: [])
    assert not near["findings"] and near["waiting"] == 1


def test_the_search_is_cut_to_the_pool(firm):
    seen = []

    def search(q, k):
        hit = [{"path": "Clienti/Contratto Ferrovie.md", "line": 4, "coverage": 0.99},
               {"path": "Matters/Chronology.md", "line": 10, "coverage": 0.2}]
        seen.append(k)
        return hit
    path = _draft(firm, "La penale dell'uno per cento al giorno va ridotta dal giudice secondo equità.")
    r = run.check_note(path, str(firm), write_dir="silica", search=search, dense=lambda t, p: 0.1, dense_floor=0.5)
    assert seen and [f["kind"] for f in r["findings"]] == ["not found"]   # the other client's 0.99 never counts


def test_the_check_never_writes(firm):
    path = _draft(firm, "Limitation on the Northwind claim expires on 19 April 2028, six years from acceptance.")
    before = {p: p.read_bytes() for p in firm.rglob("*") if p.is_file()}
    run.check_note(path, str(firm), write_dir="silica")
    assert {p: p.read_bytes() for p in firm.rglob("*") if p.is_file()} == before


def test_a_dense_floor_belongs_to_an_embedder_silica_ships_a_row_for():
    """A cosine floor is a property of one embedder (ADR-0078); a key that names
    no row would sit unused while every check said "no vectors"."""
    from silica.config import EMBEDDER_DEFAULTS

    assert set(run.DENSE_FLOORS) <= set(EMBEDDER_DEFAULTS)
    assert 0 < run.LEXICAL_FLOOR < 1
