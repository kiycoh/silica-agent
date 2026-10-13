"""graph_snapshot reads the unresolved set once, not once per note (ADR-0092
point 4). Each note used to scan every unresolved pair for its own count:
O(notes x pairs), 6.0 ms on the 730-note test vault and about 18.7 million
pair visits per snapshot at 5,000 notes with this vault's 0.75 pairs a note.
"""
from __future__ import annotations

from pathlib import Path

from silica.driver.base import NoteRef
from silica.driver.fs_backend import ObsidianFSBackend


class _CountingSet(set):
    passes = 0

    def __iter__(self):
        type(self).passes += 1
        return super().__iter__()


def _vault(root: Path, n: int) -> None:
    root.mkdir()
    for i in range(n):
        (root / f"N{i}.md").write_text(f"[[N{(i + 1) % n}]] [[Missing{i}]]\n", encoding="utf-8")


def test_a_whole_vault_snapshot_reads_the_unresolved_set_a_fixed_number_of_times(tmp_path):
    _vault(tmp_path / "v", 40)
    be = ObsidianFSBackend(vault_path=str(tmp_path / "v"))
    be._ensure_index()
    expected = be.graph_snapshot()

    be._unresolved_links = _CountingSet(be._unresolved_links)
    _CountingSet.passes = 0
    snap = be.graph_snapshot()
    assert _CountingSet.passes <= 2  # was 41: one per note, plus unresolved()
    assert snap.link_counts == expected.link_counts
    assert {(lk.source.path, lk.target) for lk in snap.unresolved} == \
        {(lk.source.path, lk.target) for lk in expected.unresolved}


def test_an_incremental_snapshot_reads_it_once(tmp_path):
    _vault(tmp_path / "v", 40)
    be = ObsidianFSBackend(vault_path=str(tmp_path / "v"))
    be._ensure_index()
    refs = [NoteRef(name="N3", path="N3.md")]
    expected = be.graph_snapshot(refs)

    be._unresolved_links = _CountingSet(be._unresolved_links)
    _CountingSet.passes = 0
    snap = be.graph_snapshot(refs)
    assert _CountingSet.passes <= 1  # was two per note of the neighbourhood
    assert snap.link_counts == expected.link_counts
    assert {(lk.source.path, lk.target) for lk in snap.unresolved} == \
        {(lk.source.path, lk.target) for lk in expected.unresolved}


def test_a_ws_snapshot_reads_it_once(tmp_path):
    from silica.driver.ws_backend import ObsidianWSBackend

    be = ObsidianWSBackend(url="ws://unused")
    fs = ObsidianFSBackend(vault_path=str(tmp_path / "v"))
    _vault(tmp_path / "v", 40)
    fs._ensure_index()
    be._notes, be._graph = dict(fs._notes), fs._graph.copy()
    be._unresolved_links = set(fs._unresolved_links)
    be._is_graph_built = True
    expected = be.graph_snapshot()

    be._unresolved_links = _CountingSet(be._unresolved_links)
    _CountingSet.passes = 0
    snap = be.graph_snapshot()
    assert _CountingSet.passes <= 1
    assert snap.link_counts == expected.link_counts
