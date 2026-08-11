"""Tests for the superseded section (spec-contested-bitemporal §4, Fase B).

Three guarantees: `## Superseded` stays the last section whatever appends
next; resolving a contradiction files the callout there instead of leaving a
body block that still claims to be unresolved; and a note with no superseded
section produces byte-identical output to the pre-Fase-B append.
"""
from __future__ import annotations

from silica.kernel.write import frontmatter, templates
from silica.kernel.write.contested import (
    SUPERSEDED_HEADING,
    append_before_superseded,
    contested_callout,
    contested_refs,
    mark_contested,
    parse_stamp,
    resolve_contested,
)

REF = "source: appunti-cardiologia-2026.md"
REF2 = "flagged: dose sbagliata (by user, 2026-07-25)"

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

## Additional notes: Dosaggio Warfarin (from appunti.md)

> [!warning] Contradiction — from appunti.md
> Il dosaggio raccomandato è 50mg/die.
>
> Conflicts with this note. Unresolved.
"""


def _contested(note: str = NOTE, ref: str = REF) -> str:
    return mark_contested(note, ref)


# --- append_before_superseded ------------------------------------------------

def test_append_is_byte_identical_without_the_section():
    """The parity guarantee: no `## Superseded`, no behaviour change."""
    body = "# Titolo\n\nProsa.\n"
    block = "\n\n## Additional notes: X (from y.md)\n\nnuovo\n"
    assert append_before_superseded(body, block) == body.rstrip() + "\n" + block


def test_append_lands_above_the_superseded_section():
    body = (
        "# Titolo\n\nProsa.\n\n"
        f"{SUPERSEDED_HEADING}\n\n> [!quote] vecchio claim\n"
    )
    out = append_before_superseded(body, "\n\n## Additional notes: X (from y.md)\n\nnuovo\n")
    assert out.index("nuovo") < out.index(SUPERSEDED_HEADING)
    assert out.rstrip().endswith("> [!quote] vecchio claim")


def test_superseded_stays_last_across_repeated_patches():
    """The invariant that matters: N appends, section still last."""
    content = f"# Titolo\n\nProsa.\n\n{SUPERSEDED_HEADING}\n\n> [!quote] vecchio\n"
    for i in range(3):
        content = templates.patch_snippet(
            heading=f"Concetto {i}", snippet=f"corpo {i}",
            source_basename="appunti.md", existing_content=content,
        )
    assert content.count(SUPERSEDED_HEADING) == 1
    for i in range(3):
        assert content.index(f"corpo {i}") < content.index(SUPERSEDED_HEADING)


# --- resolve_contested -------------------------------------------------------

def test_resolve_moves_the_callout_and_clears_the_flag():
    out = resolve_contested(_contested(), resolved_by="user", valid_to="2026-07-25")

    assert contested_refs(out) == []
    data, _, body = frontmatter.split(out)
    assert "contested" not in data and "contradictions" not in data
    assert SUPERSEDED_HEADING in body
    assert body.index(SUPERSEDED_HEADING) < body.index("[!warning] Contradiction")
    assert "5mg/die" in body  # the live claim is untouched


def test_resolve_rewrites_the_unresolved_tail():
    """A cleared note must not keep a body block claiming to be unresolved."""
    out = resolve_contested(_contested(), resolved_by="user", valid_to="2026-07-25")
    assert "Unresolved." not in out
    assert "Resolved 2026-07-25." in out


def test_resolve_stamps_valid_to():
    out = resolve_contested(_contested(), resolved_by="user", valid_to="2026-07-25")
    assert parse_stamp(out) == {"valid_to": "2026-07-25", "resolved_by": "user"}


def test_resolve_drops_the_orphaned_provenance_header():
    out = resolve_contested(_contested(), resolved_by="user", valid_to="2026-07-25")
    assert "## Additional notes" not in out  # its only content moved away


def test_resolve_keeps_a_header_that_still_has_content():
    note = NOTE.replace(
        "> Conflicts with this note. Unresolved.\n",
        "> Conflicts with this note. Unresolved.\n\nAltro contenuto della fonte.\n",
    )
    out = resolve_contested(_contested(note), resolved_by="user", valid_to="2026-07-25")
    assert "## Additional notes" in out
    assert "Altro contenuto della fonte." in out


def test_resolve_moves_every_open_callout():
    note = mark_contested(NOTE, REF)
    note = note.replace(
        "> Conflicts with this note. Unresolved.\n",
        "> Conflicts with this note. Unresolved.\n\n"
        + contested_callout("Il dosaggio è 500mg/die.", "altra-fonte.md") + "\n",
    )
    out = resolve_contested(note, resolved_by="user", valid_to="2026-07-25")
    _data, _raw, body = frontmatter.split(out)
    tail = body[body.index(SUPERSEDED_HEADING):]
    assert tail.count("[!warning] Contradiction") == 2


def test_resolve_is_idempotent():
    once = resolve_contested(_contested(), resolved_by="user", valid_to="2026-07-25")
    assert resolve_contested(once, resolved_by="user", valid_to="2026-07-26") == once


def test_resolve_no_ops_on_an_uncontested_note():
    assert resolve_contested(NOTE, resolved_by="user", valid_to="2026-07-25") == NOTE


def test_resolve_leaves_broken_yaml_alone():
    """Mirror of mark_contested: never destroy what we cannot round-trip."""
    broken = "---\ntags: [unclosed\ncontested: true\n---\n\n# X\n"
    assert resolve_contested(broken, resolved_by="user", valid_to="2026-07-25") == broken


# --- through the tool --------------------------------------------------------

def _two_refs() -> str:
    note = mark_contested(NOTE, REF)
    note = note.replace(
        "> Conflicts with this note. Unresolved.\n",
        "> Conflicts with this note. Unresolved.\n\n"
        + contested_callout("Il dosaggio è 500mg/die.", "slides.md") + "\n",
    )
    return mark_contested(note, "source: slides.md")


def test_flag_note_resolves_one_ref_and_stays_contested(tmp_vault):
    """The Fase D selector at the tool: resolving one verdict is not clearing all."""
    from silica.kernel import contested_register
    from silica.tools.notes import silica_flag_note

    path = tmp_vault.note("Farmacologia/Dosaggio Warfarin.md", _two_refs())
    contested_register.add("Farmacologia/Dosaggio Warfarin.md")

    res = silica_flag_note(name="Farmacologia/Dosaggio Warfarin.md", clear=True,
                           ref="source: slides.md")
    assert "error" not in res, res
    assert res["contested"] is True  # one contradiction is still open

    content = tmp_vault.read(path)
    assert contested_refs(content) == [REF]
    assert "500mg/die" in content[content.index(SUPERSEDED_HEADING):]
    assert "Unresolved." in content  # the surviving one still reads unresolved
    assert "Farmacologia/Dosaggio Warfarin.md" in contested_register.entries()


def test_flag_note_unknown_ref_changes_nothing(tmp_vault):
    from silica.tools.notes import silica_flag_note

    path = tmp_vault.note("Farmacologia/Dosaggio Warfarin.md", _two_refs())
    prior = tmp_vault.read(path)

    res = silica_flag_note(name="Farmacologia/Dosaggio Warfarin.md", clear=True,
                           ref="source: mai-vista.md")
    assert res["changed"] is False
    assert res["contested"] is True
    assert tmp_vault.read(path) == prior


def test_flag_note_clear_files_the_callout(tmp_vault):
    from silica.tools.notes import silica_flag_note

    path = tmp_vault.note("Farmacologia/Dosaggio Warfarin.md", _contested())
    res = silica_flag_note(name="Farmacologia/Dosaggio Warfarin.md", clear=True)
    assert "error" not in res, res
    assert res["contested"] is False

    content = tmp_vault.read(path)
    assert contested_refs(content) == []
    assert SUPERSEDED_HEADING in content
    assert "Unresolved." not in content
    assert "50mg/die" in content  # the contested claim survives, filed
