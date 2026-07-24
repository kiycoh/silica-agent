"""Overwrite producers must snapshot base_content at READ time, not rely on
validate's choke-point fallback.

The fallback (validate.py) reads the note AT VALIDATE TIME — after the LLM
call. A concurrent edit landing during the LLM window (the wide, real window)
would then become the base itself: detect_conflict sees base == current and
the edit is silently stomped. Charter UC6 requires the conflict callout.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from silica.capabilities._base import NoteContent
from silica.capabilities.enrich import run_enrich
from silica.capabilities.refine import run_refine
from silica.config import CONFIG
from silica.driver import DRIVER
from silica.kernel.merge import CONFLICT_CALLOUT_HEADER
from silica.kernel.workqueue import WorkItem


def _item(kind: str) -> WorkItem:
    return WorkItem(kind=kind, target_path="Notes/Target.md", context={"hub": "Concepts"})


ORIGINAL = "# Target\n\nOld body with [[Link]].\n"
REFINED = NoteContent(content="# Target\n\nPolished body with [[Link]].\n")


def test_refine_op_carries_base_content_from_read_time():
    with patch("silica.driver.DRIVER.read_note", return_value=MagicMock(content=ORIGINAL)), \
         patch("silica.capabilities.refine._refine_note", return_value=REFINED), \
         patch("silica.capabilities.refine.commit_ops", return_value={"status": "committed"}) as commit:
        run_refine(_item("refine"), CONFIG)
    assert commit.call_args.args[0][0].base_content == ORIGINAL


def test_enrich_op_carries_base_content_from_read_time():
    with patch("silica.driver.DRIVER.read_note", return_value=MagicMock(content=ORIGINAL)), \
         patch("silica.capabilities.enrich._enrich_note", return_value=REFINED), \
         patch("silica.capabilities.enrich.commit_ops", return_value={"status": "committed"}) as commit:
        run_enrich(_item("enrich"), CONFIG)
    assert commit.call_args.args[0][0].base_content == ORIGINAL


# --- end-to-end: concurrent edit during the LLM window -> conflict callout --


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Isolated fs vault (same pattern as test_bulk.vault)."""
    vault_dir = tmp_path / "vault"
    (vault_dir / "Notes").mkdir(parents=True)
    monkeypatch.setenv("SILICA_BACKEND", "fs")
    monkeypatch.setattr("silica.config.CONFIG.backend", "fs")
    monkeypatch.setattr("silica.config.CONFIG.vault_path", str(vault_dir))
    monkeypatch.setattr("silica.driver._driver", None)
    yield vault_dir
    monkeypatch.setattr("silica.driver._driver", None)


def test_concurrent_edit_during_llm_window_surfaces_conflict(vault):
    """The note changes while the refiner's LLM call is in flight: the commit
    must carry the conflict callout, never silently stomp the edit."""
    target = "Notes/Target.md"
    DRIVER.create(target, ORIGINAL)

    concurrent_edit = "# Target\n\nHuman edit with [[Link]] and more.\n"

    def llm_and_concurrent_edit(config, path, original):
        DRIVER.overwrite(target, concurrent_edit)  # editor lands mid-call
        return NoteContent(content="# Target\n\nPolished body with [[Link]] kept long enough.\n")

    with patch("silica.capabilities.refine._refine_note", side_effect=llm_and_concurrent_edit):
        res = run_refine(_item("refine"), CONFIG)

    assert res["status"] == "committed", res
    final = DRIVER.read_note(target).content
    assert CONFLICT_CALLOUT_HEADER in final, "concurrent edit stomped without a conflict trace"
