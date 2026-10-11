"""The driver's index stays true after a delete and a create (ADR-0092 point 1).

Measured 2026-10-11: a delete dropped the note's node and its incoming edges
without putting the referrers' links into the unresolved set, so rule 2 of the
graph gate passed every delete; a create patched only the new note, so a link
waiting for that name stayed unresolved and the new note stayed an orphan.
The oracle here is a full rebuild over the same files.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from silica.driver.fs_backend import ObsidianFSBackend


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _state(be) -> tuple:
    return (set(be._notes),
            {(s, t, bool(d.get("scaffold"))) for s, t, d in be._graph.edges(data=True)},
            set(be._unresolved_links))


def _rebuilt(root: Path) -> tuple:
    fresh = ObsidianFSBackend(vault_path=str(root))
    fresh._ensure_index()
    return _state(fresh)


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "v"
    _write(root, "A.md", "# A\n\nsee [[B]] and [[Gone]]\n")
    _write(root, "Sub/C.md", "# C\n\nsee [[B]]\n")
    _write(root, "B.md", "# B\n\nsee [[D]]\n")
    _write(root, "D.md", "# D\n")
    return root


@pytest.fixture
def be(vault):
    b = ObsidianFSBackend(vault_path=str(vault))
    b._ensure_index()
    return b


def test_a_delete_puts_every_referrers_link_into_the_unresolved_set(be, vault):
    be.delete("B.md")
    assert {("A.md", "B"), ("Sub/C.md", "B")} <= be._unresolved_links
    assert _state(be) == _rebuilt(vault)


def test_a_delete_lets_a_namesake_take_the_links(tmp_path):
    root = tmp_path / "v"
    _write(root, "Sub/C.md", "see [[B]]\n")
    _write(root, "Sub/B.md", "# B here\n")
    _write(root, "Far/Deep/B.md", "# B there\n")
    be = ObsidianFSBackend(vault_path=str(root))
    be._ensure_index()
    assert be._graph.has_edge("Sub/C.md", "Sub/B.md")

    be.delete("Sub/B.md")
    assert be._graph.has_edge("Sub/C.md", "Far/Deep/B.md")
    assert _state(be) == _rebuilt(root)


def test_a_create_resolves_the_links_waiting_for_its_name(be, vault):
    be.create("Gone.md", "# Gone\n")
    assert be._graph.has_edge("A.md", "Gone.md")
    assert ("A.md", "Gone") not in be._unresolved_links
    assert _state(be) == _rebuilt(vault)


def test_a_create_resolves_a_waiting_path_link(tmp_path):
    root = tmp_path / "v"
    _write(root, "A.md", "see [[Topic/New]]\n")
    be = ObsidianFSBackend(vault_path=str(root))
    be._ensure_index()
    be.create("Area/Topic/New.md", "# New\n")
    assert be._graph.has_edge("A.md", "Area/Topic/New.md")
    assert _state(be) == _rebuilt(root)


def test_a_create_resolves_a_waiting_adr_reference(tmp_path):
    root = tmp_path / "v"
    _write(root, "A.md", "As ADR-0003 says.\n")
    be = ObsidianFSBackend(vault_path=str(root))
    be._ensure_index()
    be.create("adr/0003-the-choice.md", "# the choice\n")
    assert be._graph.has_edge("A.md", "adr/0003-the-choice.md")
    assert _state(be) == _rebuilt(root)


def test_a_new_namesake_takes_the_links_it_wins(tmp_path):
    root = tmp_path / "v"
    _write(root, "Sub/C.md", "see [[B]]\n")
    _write(root, "Far/Deep/B.md", "# B there\n")
    be = ObsidianFSBackend(vault_path=str(root))
    be._ensure_index()
    assert be._graph.has_edge("Sub/C.md", "Far/Deep/B.md")

    be.create("Sub/B.md", "# B here\n")  # same folder as the referrer wins
    assert be._graph.has_edge("Sub/C.md", "Sub/B.md")
    assert not be._graph.has_edge("Sub/C.md", "Far/Deep/B.md")
    assert _state(be) == _rebuilt(root)


def test_upsert_of_a_missing_note_repairs_like_a_create(be, vault):
    be.upsert("Gone.md", "# Gone\n")
    assert be._graph.has_edge("A.md", "Gone.md")
    assert _state(be) == _rebuilt(vault)


def test_a_restore_after_a_delete_brings_back_the_incoming_edges(be, vault):
    body = (vault / "B.md").read_text(encoding="utf-8")
    be.delete("B.md")
    be.upsert("B.md", body)  # what /undo's recreate_deleted does
    assert {"A.md", "Sub/C.md"} <= set(be._graph.predecessors("B.md"))
    assert _state(be) == _rebuilt(vault)
