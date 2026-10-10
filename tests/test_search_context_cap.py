# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""silica_search_context must not hand the whole vault to the model, and must
say when the vault does not answer.

One hit per matching LINE meant a short query returned everything: measured at
529 hits / 170k chars for "OSI" on a 719-note vault. The tool now ranks whole
notes with BM25, scores the sections of the best ones, and serves `k` windows
— with `coverage` and `terms_absent` so a caller can tell a weak answer from
an answer, which the literal leg could never report.
"""
import pytest

from silica.tools import atomic


@pytest.fixture
def vault(tmp_path, monkeypatch):
    from silica.config import CONFIG
    from silica.kernel.recall import lexical, paths

    (tmp_path / "Folder").mkdir()
    for i in range(20):
        (tmp_path / "Folder" / f"Note {i}.md").write_text(
            f"# Note {i}\n\nThe OSI model layers a network stack.\n", encoding="utf-8")
    (tmp_path / "Folder" / "Dense.md").write_text(
        "# Dense\n\n## Compaction\n\nLeveled compaction merges sorted runs; compaction "
        "bounds read amplification, and compaction runs in the background.\n",
        encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    monkeypatch.setattr(paths, "index_dir_for", lambda vault, _d=tmp_path / ".idx": _d)
    lexical.clear()
    from silica_core.kernel.recall import lexical as core_lexical, paths as core_paths
    monkeypatch.setattr(core_paths, "index_dir_for", lambda vault, _d=tmp_path / ".idx": _d)
    core_lexical.clear()
    monkeypatch.setattr(atomic.DRIVER, "search_context", lambda q: [], raising=False)
    return tmp_path


def test_k_caps_the_hits_and_truncated_says_what_was_in_contention(vault):
    out = atomic.silica_search_context("OSI model network", k=3)
    assert len(out["hits"]) == 3
    assert out["notes_matched"] == 20 and "20 notes" in out["truncated"]


def test_the_densest_section_wins_and_carries_its_location(vault):
    out = atomic.silica_search_context("leveled compaction read amplification")
    top = out["hits"][0]
    assert top["name"] == "Dense" and top["path"] == "Folder/Dense.md"
    assert top["section"] == "Compaction" and top["line"] == 3
    assert "compaction" in top["matched_terms"] and 0 < top["coverage"] <= 1
    assert len(top["version"]) == 12  # the guard silica_read checks


def test_a_small_result_is_untruncated(vault):
    out = atomic.silica_search_context("leveled compaction")
    assert "truncated" not in out and out["notes_matched"] == 1


def test_a_word_the_vault_never_uses_is_named_not_guessed(vault):
    out = atomic.silica_search_context("compaction zebra")
    assert out["terms_absent"] == ["zebra"]
    assert out["index"]["state"] == "ready"


def test_no_match_answers_empty_without_claiming_a_cold_index(vault):
    out = atomic.silica_search_context("zebra unicorn")
    assert out["hits"] == [] and out["notes_matched"] == 0
    assert out["terms_absent"] == ["unicorn", "zebra"]
