# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Who signs a write: the identity is inherited from the launcher, never
asserted by the model calling the tool.

SILICA_AGENT_ID (an explicit fleet name), else nothing, which the REPL reads
as a person at the keyboard. The second link of this chain, the MCP client's
own name from the initialize handshake, left with the server (ADR-0130).
"""
from __future__ import annotations

import pytest

from silica.kernel.write import notetype, templates
from silica.kernel.write.contested import contested_by_agents_only, contested_refs
from tests.test_flag_note import NOTE


@pytest.fixture(autouse=True)
def _no_identity(monkeypatch):
    monkeypatch.delenv("SILICA_AGENT_ID", raising=False)


def test_agent_id_is_empty_for_a_person_at_the_repl():
    assert notetype.agent_id() == ""


def test_agent_id_is_the_fleet_name(monkeypatch):
    monkeypatch.setenv("SILICA_AGENT_ID", "coder-7")
    assert notetype.agent_id() == "coder-7"


def test_stamp_agent_uses_the_inherited_identity(monkeypatch):
    monkeypatch.setenv("SILICA_AGENT_ID", "coder-7")
    assert 'agent: "coder-7"' in templates.ensure_system_floor("some text")


def test_stamp_agent_stays_silent_at_the_repl():
    assert "agent:" not in templates.ensure_system_floor("some text")


def test_flag_note_signs_with_the_inherited_identity(tmp_vault, monkeypatch):
    from silica.tools.notes import silica_flag_note

    monkeypatch.setenv("SILICA_AGENT_ID", "claude-code:abc")
    path = tmp_vault.note("area/T.md", NOTE)
    silica_flag_note("area/T.md", reason="stale")
    refs = contested_refs(tmp_vault.read(path))
    assert refs == ["flagged: stale (by claude-code:abc, %s)" % _today()]
    assert contested_by_agents_only(refs)


def _today() -> str:
    import datetime
    return datetime.date.today().isoformat()
