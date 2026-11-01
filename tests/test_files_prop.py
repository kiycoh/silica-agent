"""silica_files(prop=) names a set by its frontmatter (ADR-0109 point 1): the
set's size is `total`, a safe-mode copy is not a member, and a malformed
selector is an error rather than the whole vault."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from silica.tools.atomic import silica_files

PROPS = {
    "Contracts/Executed/A.md": {"type": "contract", "status": "in force"},
    "Contracts/Executed/B.md": {"type": "Contract", "status": "terminated"},
    "Clienti/C/Appalto.md": {"type": "contract", "status": "in vigore"},
    "Research/Note.md": {"tags": ["statistica", "ml"]},
    "Research/Other.md": {"Tags": "Statistica"},
    "Research/Bare.md": {},
    "silica/Contracts/Executed/A.md": {"type": "contract", "status": "in force"},
}


def _call(write_dir: str = "silica", **kw):
    with patch("silica.tools.atomic.DRIVER") as drv, \
         patch("silica.kernel.vault_manifest.active_write_dir", return_value=write_dir):
        drv.list_files.return_value = [SimpleNamespace(path=p) for p in PROPS]
        drv.props_of.side_effect = lambda p: PROPS[p]
        return silica_files(**kw)


def test_set_counts_every_note_with_the_value_wherever_it_is_filed():
    res = _call(prop="type: contract")
    assert res["total"] == 3  # the copy under silica/ is not a fourth
    assert "Clienti/C/Appalto.md" in res["files"]
    assert "silica/Contracts/Executed/A.md" not in res["files"]


def test_values_compare_as_written_apart_from_case():
    assert _call(prop="status: in force")["files"] == ["Contracts/Executed/A.md"]
    assert _call(prop='status: "In Vigore"')["files"] == ["Clienti/C/Appalto.md"]


def test_a_list_matches_on_any_element_and_tags_is_the_tag_selector():
    assert _call(prop="tags: statistica")["files"] == ["Research/Note.md", "Research/Other.md"]


def test_folder_narrows_the_set():
    assert _call(folder="Contracts", prop="type: contract")["total"] == 2


def test_copies_count_when_safe_mode_is_off_or_the_staging_folder_is_asked():
    assert _call(write_dir="", prop="type: contract")["total"] == 4
    assert _call(folder="silica", prop="type: contract")["files"] == ["silica/Contracts/Executed/A.md"]


def test_an_empty_set_says_values_match_as_written():
    res = _call(prop="type: contratto")
    assert res["total"] == 0
    assert "as written" in res["hint"]


def test_malformed_selector_is_an_error_not_the_vault():
    assert "error" in _call(prop="contract")
    assert "error" in _call(prop="type:")
