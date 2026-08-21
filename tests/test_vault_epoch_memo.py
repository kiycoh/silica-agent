# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Vault-wide derivatives are memoized on a file-state epoch, never recomputed
for free questions.

silica_graph_explain paid a full analytics pass (every body + PageRank +
betweenness) on every call; silica_related rebuilt the whole decorated graph
payload per distance query; the timeline re-parsed every note's YAML per
query. The epoch is one stat pass: any create/edit/delete/move changes it,
nothing else does — the graphify/supermemory/UA rule that a derived artifact
carries a validity signature and is recomputed only when it changes.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def bound_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("# A\n\n[[B]]\n", encoding="utf-8")
    (vault / "B.md").write_text("# B\n\nbody\n", encoding="utf-8")
    monkeypatch.setattr("silica.config.CONFIG.vault_path", str(vault))
    monkeypatch.setattr("silica.driver._driver", None)
    yield vault
    monkeypatch.setattr("silica.driver._driver", None)


def _touch(path, offset: float) -> None:
    """Force a distinct mtime so the epoch cannot alias on a same-tick write."""
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + offset))


class TestTheEpoch:
    def test_stable_when_nothing_changed(self, bound_vault):
        from silica.kernel.recall.paths import vault_epoch

        assert vault_epoch() == vault_epoch() != ""

    def test_an_edit_changes_it(self, bound_vault):
        from silica.kernel.recall.paths import vault_epoch

        before = vault_epoch()
        _touch(bound_vault / "A.md", 5.0)
        assert vault_epoch() != before

    def test_unbound_vault_means_do_not_memoize(self, monkeypatch):
        from silica.kernel.recall.paths import vault_epoch

        monkeypatch.setattr("silica.config.CONFIG.vault_path", "")
        assert vault_epoch() == ""


class TestTheReportMemo:
    def test_a_second_identical_call_replays(self, bound_vault):
        from silica.kernel.report.graph_report.compute import compute_report

        first = compute_report(analytics=True)
        second = compute_report(analytics=True)
        assert second is first                 # replayed, not recomputed

    def test_a_write_through_the_driver_busts_the_memo(self, bound_vault):
        """The epoch observes the driver's view: a note landing through the
        write path (what every MCP write does) must invalidate the report.
        Purely out-of-band edits are the index sweep's domain, not this memo's."""
        from silica.driver import DRIVER
        from silica.kernel.report.graph_report.compute import compute_report

        first = compute_report(analytics=True)
        DRIVER.create("C.md", "# C\n\nnew note\n")

        second = compute_report(analytics=True)
        assert second is not first
        assert any("C" in n for n in second.pagerank_map)

    def test_override_seams_bypass_the_memo(self, bound_vault):
        from silica.kernel.report.graph_report.compute import compute_report

        nodes = [{"id": "X.md", "type": "note", "label": "X"}]
        first = compute_report(_nodes_edges_override=(nodes, []))
        second = compute_report(_nodes_edges_override=(nodes, []))
        assert second is not first


class TestTheTimelineMemo:
    def test_rows_replay_on_the_same_epoch(self, bound_vault):
        from silica.kernel.write.timeline import _all_rows

        (bound_vault / "D.md").write_text(
            "---\ndate: 2026-01-02\n---\nbody\n", encoding="utf-8")
        first = _all_rows(bound_vault)
        assert _all_rows(bound_vault) is first

        _touch(bound_vault / "D.md", 5.0)
        assert _all_rows(bound_vault) is not first

    def test_ignored_directories_are_not_walked(self, tmp_path):
        from silica.kernel.write.timeline import timeline

        (tmp_path / "note.md").write_text(
            "---\ndate: 2026-01-02\n---\nbody\n", encoding="utf-8")
        junk = tmp_path / "node_modules" / "pkg"
        junk.mkdir(parents=True)
        (junk / "readme.md").write_text(
            "---\ndate: 2026-01-03\n---\nvendor\n", encoding="utf-8")

        rows = timeline(tmp_path)["rows"]
        assert [r[2] for r in rows] == ["note"]
