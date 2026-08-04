"""Alias consolidation: the gate mirrors build_alias_map, writes are bounded.

The pass proposes {canonical: [variants]} via one LLM call; everything
safety-critical is in gate_alias_groups, which must reject at write time
exactly what build_alias_map would drop at read time — otherwise a rejected
surface sits in frontmatter forever, silently dead.
"""
from __future__ import annotations

from unittest.mock import patch

from silica.driver.base import NoteContent, NoteRef
from silica.tools.aliases import gate_alias_groups, silica_aliases


TITLES = ["Artificial Intelligence", "Graph Theory", "RAG"]
TITLE_LOWERS = {t.lower() for t in TITLES}


def test_happy_path_groups_survive():
    out = gate_alias_groups(
        {"Artificial Intelligence": ["AI system", "machine intelligence"]},
        TITLES, TITLE_LOWERS, set(),
    )
    assert out == {"Artificial Intelligence": ["AI system", "machine intelligence"]}


def test_unknown_canonical_is_dropped():
    out = gate_alias_groups({"Nonexistent Note": ["whatever"]}, TITLES, TITLE_LOWERS, set())
    assert out == {}


def test_variant_colliding_with_any_real_title_is_dropped():
    # "RAG" is a real note: it can never become another note's alias, even when
    # the collision lives outside the scoped folder.
    out = gate_alias_groups(
        {"Artificial Intelligence": ["RAG", "AI system"]},
        TITLES, TITLE_LOWERS | {"out of scope note"}, set(),
    )
    assert out == {"Artificial Intelligence": ["AI system"]}


def test_variant_claimed_by_two_canonicals_is_dropped_from_both():
    out = gate_alias_groups(
        {"Artificial Intelligence": ["smart systems"], "Graph Theory": ["smart systems"]},
        TITLES, TITLE_LOWERS, set(),
    )
    assert out == {}


def test_already_registered_surface_is_dropped():
    out = gate_alias_groups(
        {"Artificial Intelligence": ["AI system"]},
        TITLES, TITLE_LOWERS, {"ai system"},
    )
    assert out == {}


def test_noise_floor_and_self_alias():
    out = gate_alias_groups(
        {"Artificial Intelligence": ["x", "", "artificial intelligence"]},
        TITLES, TITLE_LOWERS, set(),
    )
    assert out == {}


def test_malformed_proposal_values_are_ignored():
    out = gate_alias_groups(
        {"Artificial Intelligence": "not a list", "Graph Theory": [None, 42]},
        TITLES, TITLE_LOWERS, set(),
    )
    assert out == {}


def test_duplicate_variant_within_group_kept_once_first_casing():
    out = gate_alias_groups(
        {"Graph Theory": ["Network Theory", "network theory"]},
        TITLES, TITLE_LOWERS, set(),
    )
    assert out == {"Graph Theory": ["Network Theory"]}


# ---------------------------------------------------------------------------
# Tool paths (LLM and driver faked; add_alias and the gate run for real)
# ---------------------------------------------------------------------------

_REFS = [
    NoteRef(name="Artificial Intelligence", path="Concepts/Artificial Intelligence.md"),
    NoteRef(name="Graph Theory", path="Concepts/Graph Theory.md"),
]


def test_dry_run_returns_groups_and_writes_nothing():
    with patch("silica.driver.DRIVER.list_files", return_value=_REFS), \
         patch("silica.driver.DRIVER.alias_index", return_value=[]), \
         patch("silica.tools.aliases._propose_groups",
               return_value={"Artificial Intelligence": ["AI system"]}), \
         patch("silica.agent.commit.commit_ops") as commit:
        res = silica_aliases(apply=False)
    assert res["groups"] == {"Artificial Intelligence": ["AI system"]}
    assert res["accepted"] == 1
    commit.assert_not_called()


def test_apply_writes_aliases_through_the_gate():
    committed = []

    def _fake_commit(ops, **kwargs):
        committed.append((ops, kwargs))
        return {"status": "committed"}

    with patch("silica.driver.DRIVER.list_files", return_value=_REFS), \
         patch("silica.driver.DRIVER.alias_index", return_value=[]), \
         patch("silica.tools.aliases._propose_groups",
               return_value={"Artificial Intelligence": ["AI system"]}), \
         patch("silica.driver.DRIVER.read_note",
               return_value=NoteContent(ref=_REFS[0], content="---\ntags: [x]\n---\n\nbody\n")), \
         patch("silica.agent.commit.commit_ops", side_effect=_fake_commit):
        res = silica_aliases(apply=True)

    assert res["written"] == {"Concepts/Artificial Intelligence.md": 1}
    (ops, kwargs), = committed
    assert "AI system" in (ops[0].content or "")
    assert kwargs["bounds"].name == "alias_consolidation"


def test_apply_is_idempotent_when_alias_already_declared():
    with patch("silica.driver.DRIVER.list_files", return_value=_REFS), \
         patch("silica.driver.DRIVER.alias_index", return_value=[]), \
         patch("silica.tools.aliases._propose_groups",
               return_value={"Artificial Intelligence": ["AI system"]}), \
         patch("silica.driver.DRIVER.read_note",
               return_value=NoteContent(ref=_REFS[0], content="---\naliases: [AI system]\n---\n\nbody\n")), \
         patch("silica.agent.commit.commit_ops") as commit:
        res = silica_aliases(apply=True)
    # note untouched: add_alias found the surface already declared
    assert res["written"] == {}
    commit.assert_not_called()
