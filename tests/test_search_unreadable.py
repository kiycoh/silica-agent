# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""silica_search_context names the documents it could not read (survey card C1).

A folder-wide "the documents do not say" is a claim about files the index never
held text for; the reply now says which, so the model can say it instead.
"""
from __future__ import annotations

import pytest

import silica.tools.notes  # noqa: F401  (the write gate registers on import)
from silica.config import CONFIG
from silica.tools import atomic


@pytest.fixture
def vault(tmp_vault):
    from pathlib import Path

    tmp_vault.note("Notes/fees.md", "# Fees\n\nThe rate card was revised in January.\n")
    root = Path(CONFIG.vault_path)
    (root / "Contracts").mkdir()
    (root / "Contracts/rate-card.docx").write_bytes(b"PK\x03\x04 not converted")
    (root / "Contracts/converted.docx").write_bytes(b"PK\x03\x04 converted")
    (root / "Contracts/converted.md").write_text("# Converted\n\nThe sidecar holds its text.\n", encoding="utf-8")
    return tmp_vault


def test_an_office_file_with_no_sidecar_is_named_and_a_converted_one_is_not(vault):
    out = atomic.silica_search_context("rate card")
    assert out["unreadable"] == {"count": 1, "paths": ["Contracts/rate-card.docx"]}


def test_the_literal_scan_names_it_too(vault):
    out = atomic.silica_search_context("rate card", literal=True)
    assert out["unreadable"]["paths"] == ["Contracts/rate-card.docx"]


def test_a_folder_holding_none_carries_no_field(vault):
    out = atomic.silica_search_context("rate card", folder="Notes")
    assert "unreadable" not in out


def test_a_raw_its_notes_hold_the_text_of_is_not_named(vault):
    """An office file brought in again and attached to its notes (ADR-0129,
    amended 2026-11-04) has no sidecar beside it: its parts name it."""
    from pathlib import Path

    root = Path(CONFIG.vault_path)
    data = (root / "Contracts/rate-card.docx").read_bytes()
    (root / "Contracts/rate-card").mkdir()
    (root / "Contracts/rate-card/rate-card-1-fees.md").write_text(
        f'---\nsource_file: "Contracts/rate-card.docx"\nsource_bytes: {len(data)}\n---\n\n'
        "## Fees\n\nThe rate card was revised.\n", encoding="utf-8")

    assert "unreadable" not in atomic.silica_search_context("rate card")


def test_the_engines_public_inventory_is_read_when_it_has_one(vault, monkeypatch):
    """A silica-core with `unreadable()` answers for itself; the private names
    are read only for a release without it."""
    from silica_core import core

    def public(folder=""):
        return {"root": CONFIG.vault_path, "folder": folder,
                "files": [{"path": "Contracts/only-the-engine-knows.docx", "status": "unconverted", "reason": "x"}]}

    monkeypatch.setattr(core, "unreadable", public, raising=False)
    monkeypatch.setattr(core, "_walk", None)  # the private walk is never reached
    out = atomic.silica_search_context("rate card", literal=True)
    assert out["unreadable"] == {"count": 1, "paths": ["Contracts/only-the-engine-knows.docx"]}
