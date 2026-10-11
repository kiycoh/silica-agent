# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The preservation rule of ADR-0093 decision 1, clause by clause.

`lost(before, after)` is None when `after` kept every line and value of
`before`; otherwise it names the first thing lost. Inserting is always free.
"""
from __future__ import annotations

import sys
import subprocess

import pytest

from silica.kernel.write.preserve import Loss, lost

FM = "---\ntitle: T\ntags:\n  - a\n  - b\nstatus: active\n---\n\n"
BODY = "# T\n\nFirst line.\n\n- item\n  - nested\n\nLast line.\n"
NOTE = FM + BODY


def test_the_module_does_not_import_the_driver():
    code = ("import sys, silica.kernel.write.preserve; "
            "sys.exit(1 if 'silica.driver' in sys.modules else 0)")
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


def test_identical_and_inserted_lines_pass():
    assert lost(NOTE, NOTE) is None
    assert lost(NOTE, NOTE.replace("First line.\n", "First line.\n\nAn inserted line.\n")) is None
    assert lost(NOTE, NOTE + "\n## New section\n\nMore.\n") is None


@pytest.mark.parametrize("after", [
    NOTE.replace("First line.\n", ""),                        # dropped
    NOTE.replace("First line.", "First lane."),               # edited
    NOTE.replace("First line.\n\n- item\n", "- item\n\nFirst line.\n"),  # reordered
    NOTE.replace("  - nested", "- nested"),                   # indentation is nesting
    NOTE.replace("First line.", "First line \u2014 here.").replace("\u2014", "-"),  # folded dash
])
def test_a_dropped_edited_or_moved_body_line_is_a_loss(after):
    why = lost(NOTE, after)
    assert why and why.startswith("body:")


def test_trailing_blanks_and_blank_lines_are_not_compared():
    after = NOTE.replace("First line.\n", "First line.   \n\n\n\n")
    assert lost(NOTE, after) is None
    assert lost(NOTE, NOTE.rstrip("\n")) is None


@pytest.mark.parametrize("a,b", [("```", "```"), ("%%", "%%"), ("<!--", "-->")])
def test_hiding_the_body_in_a_fence_or_a_comment_is_a_loss(a, b):
    after = FM + a + "\n" + BODY + b + "\n"
    assert lost(NOTE, after)


def test_a_line_already_inside_a_fence_may_stay_there():
    note = FM + "```\ncode\n```\n"
    assert lost(note, note + "\nAfter.\n") is None
    assert lost(note, FM + "code\n")  # out of the fence: a different line


@pytest.mark.parametrize("before,after", [
    ("See Machine learning here.\n", "See [[Machine learning]] here.\n"),
    ("See ML here.\n", "See [[Machine learning|ML]] here.\n"),
    ("| ML | x |\n", "| [[Machine learning\\|ML]] | x |\n"),
    ("See Heading notes.\n", "See [[Heading notes#Part]].\n"),
])
def test_links_reduce_to_what_the_reader_sees(before, after):
    assert lost(FM + before, FM + after) is None
    assert lost(FM + after, FM + before) is None  # the sweep's unlink, too


def test_an_embed_keeps_its_bang():
    assert lost(FM + "![[img.png]]\n", FM + "img.png\n")


def test_crlf_is_never_compared():
    crlf = NOTE.replace("\n", "\r\n")
    assert lost(crlf, NOTE) is None
    assert lost(NOTE, crlf) is None


# ---------------------------------------------------------------- frontmatter

def _fm(**kv) -> str:
    import yaml
    return "---\n" + yaml.safe_dump(kv, sort_keys=False) + "---\n\n" + BODY


def test_a_dropped_or_changed_key_is_a_loss():
    assert "removed" in lost(NOTE, NOTE.replace("status: active\n", ""))
    assert "changed" in lost(NOTE, NOTE.replace("status: active", "status: done"))


def test_system_keys_may_change_or_go():
    before = _fm(title="T", **{"last modified": "2026-01-01", "agent": "x",
                               "review": "soft", "code_ref": "abc"})
    assert lost(before, _fm(title="T", **{"last modified": "2026-10-11"})) is None


def test_free_keys_may_change_or_go_for_this_write_only():
    assert lost(NOTE, NOTE.replace("status: active", "status: done"), free=("status",)) is None
    assert lost(NOTE, NOTE.replace("status: active", "status: done"), free=("title",))


def test_an_empty_old_value_may_take_any_value():
    for empty in (None, "", [], {}):
        assert lost(_fm(title="T", related=empty), _fm(title="T", related=["[[Hub]]"])) is None


def test_a_list_may_grow_and_reorder_but_keeps_every_item():
    assert lost(NOTE, NOTE.replace("  - a\n  - b\n", "  - b\n  - c\n  - a\n")) is None
    assert lost(NOTE, NOTE.replace("  - a\n  - b\n", "  - b\n"))


def test_a_scalar_may_become_a_list_that_holds_it():
    assert lost(_fm(status="active"), _fm(status=["active", "superseded"])) is None
    assert lost(_fm(status="active"), _fm(status=["superseded"]))


def test_a_singular_alias_folds_into_aliases():
    assert lost(_fm(alias="Old"), _fm(aliases=["Old", "New"])) is None
    assert lost(_fm(aliases="A, B"), _fm(aliases=["A", "B", "C"])) is None
    assert lost(_fm(alias="Old"), _fm(aliases=["New"]))


def test_a_new_duplicate_key_is_a_loss_an_old_one_is_not():
    dup = "---\nrelated: null\nrelated:\n  - x\n---\n\nBody\n"
    assert "twice" in lost("---\nrelated: null\n---\n\nBody\n", dup)
    assert lost(dup, dup + "More\n") is None


def test_frontmatter_that_stops_parsing_or_goes_is_a_loss():
    assert lost(NOTE, NOTE.replace("title: T", "title: [T"))
    assert lost(NOTE, BODY)


def test_an_unparseable_block_keeps_its_raw_lines_minus_system_keys():
    broken = "---\ntitle: [T\nfoo: bar\nlast modified: 2026-01-01\n---\n\nBody\n"
    assert lost(broken, broken.replace("last modified: 2026-01-01\n", "")) is None
    assert lost(broken, broken.replace("foo: bar\n", ""))
    assert lost(broken, broken.replace("foo: bar\n", "foo: bar\nnew: 1\n")) is None


# ---------------------------------------------------------------- declared losses

@pytest.mark.parametrize("loss", [Loss.PERSON, Loss.RESTORE, Loss.STAGED,
                                  Loss.DERIVED, Loss.MIGRATE])
def test_a_whole_note_reason_relaxes_everything(loss):
    assert lost(NOTE, "nothing left\n", loss=loss) is None


def test_cite_relaxes_footnote_references_and_definitions():
    before = FM + "A fact.[^src]\n\n[^src]: Source A\n"
    after = FM + "A fact.[^Source-A]\n\n[^Source-A]: [[sources/Source A]]\n"
    assert lost(before, after)
    assert lost(before, after, loss=Loss.CITE) is None
    assert lost(before, FM + "\n[^src]: Source A\n", loss=Loss.CITE)  # the fact still counts


def test_unlink_relaxes_related_entries_parsed_or_raw():
    before = _fm(title="T", related=["[[Hub]]", "[[Gone]]"])
    after = _fm(title="T", related=["[[Hub]]"])
    assert lost(before, after)
    assert lost(before, after, loss=Loss.UNLINK) is None
    raw = '---\ntitle: [T\nrelated:\n  - "[[Hub]]"\n  - "[[Gone]]"\n---\n\nB\n'
    assert lost(raw, raw.replace('  - "[[Gone]]"\n', ""), loss=Loss.UNLINK) is None
    assert lost(before, _fm(title="X", related=["[[Hub]]"]), loss=Loss.UNLINK)


def test_rename_relaxes_link_targets_only():
    before = FM + "See [[Old]], [[Old|it]] and [x](Old.md).\n"
    after = FM + "See [[New]], [[New|it]] and [x](New.md).\n"
    assert lost(before, after)
    assert lost(before, after, loss=Loss.RENAME) is None
    assert lost(before, FM + "See [[New]], [[New|that]] and [x](New.md).\n", loss=Loss.RENAME)
    fm_link = _fm(title="T", up="[[Old]]")
    assert lost(fm_link, _fm(title="T", up="[[New]]"), loss=Loss.RENAME) is None


def test_resolve_relaxes_order_and_the_callout_text():
    before = (FM + "Intro.\n\n> [!warning] Contradiction\n> Says X. Unresolved.\n\nOutro.\n")
    after = ("---\ntitle: T\ntags:\n  - a\n  - b\nstatus: active\n---\n\nIntro.\n\nOutro.\n\n"
             "## Superseded\n\n> [!warning] Contradiction\n> Says X. Resolved 2026-10-11.\n")
    contested = before.replace("status: active\n", "status: active\ncontested: true\n")
    assert lost(contested, after)
    assert lost(contested, after, loss=Loss.RESOLVE) is None
    assert lost(contested, after.replace("Outro.\n", ""), loss=Loss.RESOLVE)


def test_plan_relaxes_the_generated_sections_and_the_plan_keys():
    before = ("---\nscope: steps\ntarget: A\ncreated: 2026-10-01\n---\n\n# Plan\n\nGoal.\n\n"
              "## Objectives\n- old objective\n\n## Tree\n- [[A]]\n\n## My notes\nKeep me.\n\n"
              "## Sessions\n- 2026-10-02 one\n")
    after = ("---\nscope: tree\ntarget: B\ncreated: 2026-10-01\n---\n\n# Plan 2\n\nNew goal.\n\n"
             "## Objectives\n- new objective\n\n## Tree\n- [[B]]\n\n## My notes\nKeep me.\n\n"
             "## Sessions\n- 2026-10-02 one\n")
    assert lost(before, after)
    assert lost(before, after, loss=Loss.PLAN) is None
    assert lost(before, after.replace("Keep me.\n", ""), loss=Loss.PLAN)
    assert lost(before, after.replace("created: 2026-10-01\n", ""), loss=Loss.PLAN)


def test_the_plan_sections_are_the_ones_plans_generates():
    from silica.kernel.learn.plans import _GENERATED
    from silica.kernel.write.preserve import PLAN_SECTIONS

    assert PLAN_SECTIONS == frozenset(_GENERATED)


def test_resolve_drops_the_provenance_header_left_empty_by_the_move():
    # contested._extract_contradiction_callouts prunes a pre-2026-09-04 patch
    # header whose only content was the callout it moved (found by the
    # suite's census of would-refuse lines, test_superseded.py).
    from silica.kernel.write.contested import mark_contested, resolve_contested

    note = ("---\nAI: true\n---\n\n# Warfarin\n\nThe dose is 5mg.\n\n"
            "## Additional notes: Warfarin (from a.md)\n\n"
            "> [!warning] Contradiction \u2014 from a.md\n> The dose is 50mg.\n>\n"
            "> Conflicts with this note. Unresolved.\n")
    before = mark_contested(note, "source: a.md")
    after = resolve_contested(before, resolved_by="user", valid_to="2026-10-11")
    assert lost(before, after, loss=Loss.RESOLVE) is None
    assert lost(before, after.replace("The dose is 5mg.\n", ""), loss=Loss.RESOLVE)


def test_the_header_prefixes_are_the_ones_templates_writes():
    from silica.kernel.write.preserve import PROVENANCE_HEADERS
    from silica.kernel.write.templates import PROVENANCE_HEADER_PREFIXES

    assert PROVENANCE_HEADERS == PROVENANCE_HEADER_PREFIXES
