"""Tests for the claim stamp (spec-contested-bitemporal §3, Fase A).

Three things must hold: the stamp round-trips and cannot break out of its own
HTML comment; the event clock resolves with the documented precedence and is
shared with the source-leaf writer; and a write with no `valid_from` produces
byte-identical output to the pre-stamp behaviour, so every non-FSM path
(dedup, enrich, refine, interactive tools) and the goldens are untouched.
"""
from __future__ import annotations

from silica.kernel import templates
from silica.kernel.contested import parse_stamp, stamp
from silica.kernel.ops import Op, OpType
from silica.kernel.provenance import source_valid_from


# --- stamp / parse_stamp (pure) ----------------------------------------------

def test_stamp_round_trip():
    s = stamp(valid_from="2023-05-08", run="b07f126889f04ee5")
    assert s == "<!-- silica: valid_from=2023-05-08 run=b07f126889f04ee5 -->"
    assert parse_stamp(s) == {"valid_from": "2023-05-08", "run": "b07f126889f04ee5"}


def test_stamp_drops_empty_fields_and_is_empty_when_all_empty():
    assert stamp(valid_from="2023-05-08", run="") == "<!-- silica: valid_from=2023-05-08 -->"
    assert stamp(valid_from="", run=None) == ""


def test_stamp_value_cannot_close_the_comment():
    """A hostile value must not end the comment early or inject markdown."""
    s = stamp(valid_from="2023 --> <script>alert(1)</script>")
    assert s.count("-->") == 1
    assert "<script>" not in s
    assert parse_stamp(s)["valid_from"]


def test_parse_stamp_absent():
    assert parse_stamp("# Titolo\n\nSolo prosa.") == {}


# --- source_valid_from (pure) ------------------------------------------------

SOURCE = "---\ndate: 2023-05-08\nsource_id: session_1.md\n---\n\nCaroline: ciao!\n"


def test_valid_from_prefers_capture_clock():
    assert source_valid_from(SOURCE, "2026-07-25") == "2026-07-25"


def test_valid_from_falls_back_to_source_date():
    assert source_valid_from(SOURCE, None) == "2023-05-08"


def test_valid_from_defaults_to_today_without_signal():
    import datetime
    today = datetime.date.today().isoformat()
    assert source_valid_from("# Nessun frontmatter\n", None) == today
    assert source_valid_from("", None) == today


def test_valid_from_survives_broken_yaml():
    """Unparseable frontmatter must degrade to today, never raise into the FSM."""
    import datetime
    broken = "---\ndate: [unclosed\n---\n\nbody\n"
    assert source_valid_from(broken, None) == datetime.date.today().isoformat()


# --- patch_snippet parity ----------------------------------------------------

def test_patch_snippet_byte_identical_without_valid_from():
    """The parity guarantee: no valid_from, no diff."""
    expected = (
        "\n\n## Note aggiuntive — Warfarin (da appunti.md)\n\n"
        "Il dosaggio raccomandato è 5mg/die.\n"
    )
    assert templates.patch_snippet(
        heading="Warfarin", snippet="Il dosaggio raccomandato è 5mg/die.",
        source_basename="appunti.md",
    ) == expected


def test_patch_snippet_stamps_under_the_header():
    out = templates.patch_snippet(
        heading="Warfarin", snippet="Il dosaggio raccomandato è 5mg/die.",
        source_basename="appunti.md", valid_from="2023-05-08",
    )
    header_i = out.index("## Note aggiuntive")
    stamp_i = out.index("<!-- silica:")
    body_i = out.index("Il dosaggio raccomandato")
    assert header_i < stamp_i < body_i
    assert parse_stamp(out) == {"valid_from": "2023-05-08"}


# --- write / patch through the executor --------------------------------------

NOTE = """---
parent note: "[[Farmacologia]]"
related:
  - "[[Farmacologia]]"
tags:
  - farmacologia
last modified: 2026-07-02
AI: true
---

# Dosaggio Warfarin

Il dosaggio raccomandato è 5mg/die.
"""


def test_execute_write_stamps_under_the_h1(tmp_vault):
    from silica.driver import DRIVER
    from silica.kernel.bulk import execute_one

    execute_one(Op(
        op=OpType.write, heading="Dosaggio Warfarin", source_basename="appunti.md",
        path="Farmacologia/Dosaggio Warfarin.md", hub="Farmacologia",
        snippet="Il dosaggio raccomandato è 5mg/die.", valid_from="2023-05-08",
    ))

    content = DRIVER.read_note("Farmacologia/Dosaggio Warfarin.md").content
    assert parse_stamp(content) == {"valid_from": "2023-05-08"}
    assert content.index("# Dosaggio Warfarin") < content.index("<!-- silica:")
    assert content.index("<!-- silica:") < content.index("Il dosaggio")


def test_execute_write_without_valid_from_emits_no_stamp(tmp_vault):
    from silica.driver import DRIVER
    from silica.kernel.bulk import execute_one

    execute_one(Op(
        op=OpType.write, heading="Dosaggio Warfarin", source_basename="appunti.md",
        path="Farmacologia/Dosaggio Warfarin.md", hub="Farmacologia",
        snippet="Il dosaggio raccomandato è 5mg/die.",
    ))
    content = DRIVER.read_note("Farmacologia/Dosaggio Warfarin.md").content
    assert "<!-- silica:" not in content


def test_execute_patch_stamps_the_block(tmp_vault):
    from silica.kernel.bulk import execute_one

    path = tmp_vault.note("Farmacologia/Dosaggio Warfarin.md", NOTE)
    execute_one(Op(
        op=OpType.patch, heading="Monitoraggio INR", source_basename="appunti.md",
        path="Farmacologia/Dosaggio Warfarin.md", hub="Farmacologia",
        snippet="INR target 2.0-3.0.", valid_from="2023-05-08",
    ))

    content = tmp_vault.read(path)
    assert parse_stamp(content) == {"valid_from": "2023-05-08"}
    assert content.index("## Note aggiuntive") < content.index("<!-- silica:")
    assert "5mg/die" in content  # the pre-existing claim is untouched


def test_stamp_does_not_leak_into_the_moc_bullet(tmp_vault):
    """hub_desc reads op.snippet, which the stamp must never mutate: a MOC
    bullet reading '<!-- silica: ... -->' would be a visible regression."""
    from silica.kernel.bulk import execute_one
    from silica.kernel.moc import hub_desc

    op = Op(
        op=OpType.write, heading="Dosaggio Warfarin", source_basename="appunti.md",
        path="Farmacologia/Dosaggio Warfarin.md", hub="Farmacologia",
        snippet="Il dosaggio raccomandato è 5mg/die.", valid_from="2023-05-08",
    )
    execute_one(op)
    assert hub_desc(op.snippet) == "Il dosaggio raccomandato è 5mg/die."
