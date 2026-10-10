# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0055 "Chat and commands": /zotero and its verbs, and /revert reaching
the Zotero writes of a run through zotero.jsonl."""
from __future__ import annotations

import os

import pytest

from silica.cli import _handle_direct_shortcut
from silica.config import CONFIG
from silica.sources import zotero as z
from silica.sources import zotero_write as zw
from silica.ui.commands import command_names
from tests.zotero_fake import SERVER_ID, FakeZotero

INBOX = "INBOX001"


@pytest.fixture
def fake(tmp_vault, monkeypatch):
    from silica.kernel.vault_manifest import set_zotero

    f = FakeZotero()
    f.add_paper()
    set_zotero(CONFIG.vault_path, "library", SERVER_ID)
    set_zotero(CONFIG.vault_path, "inbox", INBOX)
    monkeypatch.setenv("SILICA_ZOTERO_API_KEY", f.api_key)
    monkeypatch.setattr(z, "client", lambda: z.Zotero(
        server_id=SERVER_ID, api_key=os.getenv("SILICA_ZOTERO_API_KEY", ""), transport=f.transport()))
    return f


def _inbox(fake, key, title):
    return fake.clone(fake.items["8H4AC8GW"], key, title=title, collections=[INBOX],
                      citationKey=key.lower())


def _filed(fake, key, title, col):
    return fake.clone(fake.items["8H4AC8GW"], key, title=title, collections=[col],
                      citationKey=key.lower())


@pytest.fixture
def library(fake, monkeypatch):
    """Two filed memory papers, two graph papers, and two inbox items: one all
    three neighbours agree on, one they split on."""
    fake.items["8H4AC8GW"]["data"].update(title="memory c", collections=["MEMORY01"])
    _filed(fake, "MEM00001", "memory a", "MEMORY01")
    _filed(fake, "MEM00002", "memory b", "MEMORY01")
    _filed(fake, "GRAPH001", "graph a", "GRAPHS01")
    _filed(fake, "GRAPH002", "graph b", "GRAPHS01")
    _inbox(fake, "SURE0001", "memory new")
    _inbox(fake, "SPLIT001", "between")
    vecs = {"memory a": [1, 0], "memory b": [0.9, 0.1], "memory c": [0.95, 0.05],
            "graph a": [0, 1], "graph b": [0.1, 0.9],
            "memory new": [0.97, 0.03], "between": [0.6, 0.55]}
    monkeypatch.setattr(zw, "_default_embed", lambda texts: [vecs[t.split("\n")[0]] for t in texts])
    return fake


def test_zotero_is_a_command():
    assert "/zotero" in command_names()


def test_zotero_file_lists_two_choices_and_the_vote(library, capsys):
    assert _handle_direct_shortcut("/zotero file", []) is True

    out = capsys.readouterr().out
    assert "memory new" in out and "MEMORY01" in out and "3/3" in out
    assert library.items["SURE0001"]["data"]["collections"] == [INBOX]   # nothing moved


def test_zotero_file_apply_files_only_what_all_three_neighbours_agree_on(library):
    _handle_direct_shortcut("/zotero file --apply", [])

    assert library.items["SURE0001"]["data"]["collections"] == ["MEMORY01"]
    assert library.items["SPLIT001"]["data"]["collections"] == [INBOX]


def test_bare_revert_undoes_the_newest_zotero_run(library):
    _handle_direct_shortcut("/zotero file --apply", [])

    _handle_direct_shortcut("/revert", [])

    assert library.items["SURE0001"]["data"]["collections"] == [INBOX]


def test_revert_of_a_run_undoes_its_backlink_note(fake, tmp_vault):
    tmp_vault.note("sources/@packerMemGPTLLMsOperating2024.md", "---\nzotero_key: 8H4AC8GW\n---\n")
    from silica.kernel.write.undo_journal import get_undo_journal
    run = get_undo_journal().start_run(source="nucleate", vault=CONFIG.vault_path)
    zw.backlink(z.client(), "8H4AC8GW", "sources/@packerMemGPTLLMsOperating2024.md", run_id=run)

    _handle_direct_shortcut(f"/revert {run}", [])

    assert [i for i in fake.items.values() if i["data"]["itemType"] == "note"] == []


def test_zotero_group_previews_and_apply_writes(fake, monkeypatch, capsys):
    monkeypatch.setattr(zw, "compute_group_plan", lambda: {"memory · agents": ["8H4AC8GW"]})

    _handle_direct_shortcut("/zotero group", [])
    assert "memory · agents" in capsys.readouterr().out
    assert fake.collections == {}

    _handle_direct_shortcut("/zotero group --apply", [])
    names = {c["data"]["name"] for c in fake.collections.values()}
    assert names == {"Silica groups", "memory · agents"}


def test_zotero_unlink_asks_before_it_deletes(fake, tmp_vault, capsys):
    tmp_vault.note("sources/@packerMemGPTLLMsOperating2024.md", "---\nzotero_key: 8H4AC8GW\n---\n")
    zw.backlink(z.client(), "8H4AC8GW", "sources/@packerMemGPTLLMsOperating2024.md", run_id="r")

    _handle_direct_shortcut("/zotero unlink", [])
    assert "--yes" in capsys.readouterr().out
    assert any(i["data"]["itemType"] == "note" for i in fake.items.values())

    _handle_direct_shortcut("/zotero unlink --yes", [])
    assert not any(i["data"]["itemType"] == "note" for i in fake.items.values())


def test_zotero_grant_says_whether_the_key_was_kept(fake, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("silica.onboarding.wizard.resolve_env_path", lambda: tmp_path / ".env")
    fake.authorize_answer = {"key": "s" * 32, "remember": False}

    _handle_direct_shortcut("/zotero grant", [])

    assert "not kept" in capsys.readouterr().out


def test_zotero_status_shows_what_changed_since_the_last_session(fake, monkeypatch, capsys):
    monkeypatch.setattr(z, "_client_for", lambda vault, link: z.client())
    z.detect(CONFIG.vault_path)
    item = fake.clone(fake.items["8H4AC8GW"], "NEWPAPER", title="Memory in the Age of AI Agents",
                      citationKey="hu2026", dateAdded="2099-01-01T00:00:00Z")
    fake._bump(item)

    _handle_direct_shortcut("/zotero", [])

    out = capsys.readouterr().out
    assert "1 new since the last session" in out
    assert "Memory in the Age of AI Agents" in out
