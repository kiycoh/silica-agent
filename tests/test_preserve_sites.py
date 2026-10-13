# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Declared sites end to end: what the person types, what /undo puts back and
what a flag clear files away land without a would-refuse line, while an
undeclared tool write that drops a line still logs one (ADR-0093)."""
from __future__ import annotations

import logging

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _warnings(caplog):
    return [r for r in caplog.records
            if r.name == "silica.kernel.write.preserve" and r.levelno == logging.WARNING]


@pytest.fixture
def client(tmp_vault):
    from silica.ui.web import server

    server._reset_session()
    return TestClient(server.app)


def test_the_editor_save_is_the_persons(client, tmp_vault, caplog):
    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    tmp_vault.note("Foo.md", "# Foo\n\nline one\nline two\n")
    raw = client.get("/note/raw", params={"path": "Foo.md"}).json()
    r = client.post("/note/save", json={"path": "Foo.md", "text": "# Foo\n", "etag": raw["etag"]})
    assert r.status_code == 200, r.text
    assert not _warnings(caplog)


def test_undo_restores_without_a_warning(tmp_vault, caplog):
    from silica.cli import _handle_direct_shortcut
    from silica.tools.notes import silica_patch_note

    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    tmp_vault.note("Other.md", "# Other\n\nbody\n")
    silica_patch_note("Other", "Extra", "more text", "chat")
    assert _handle_direct_shortcut("/undo", []) is True
    assert not _warnings(caplog)


def test_a_flag_and_its_clear_land_clean(tmp_vault, caplog):
    from silica.tools.notes import silica_flag_note

    caplog.set_level(logging.WARNING, logger="silica.kernel.write.preserve")
    tmp_vault.note("N.md", "---\nAI: true\n---\n\n# N\n\nA claim.\n")
    assert "error" not in silica_flag_note(name="N.md", reason="wrong year")
    assert "error" not in silica_flag_note(name="N.md", clear=True)
    assert not _warnings(caplog)
