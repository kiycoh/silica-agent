# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0055 decisions 7 and 8: what Silica writes into Zotero, how each write
is guarded, and how /revert takes it back by state."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from silica.config import CONFIG
from silica.sources import zotero as z
from silica.sources import zotero_write as zw
from tests.zotero_fake import SERVER_ID, FakeZotero

LEAF = "sources/@packerMemGPTLLMsOperating2024.md"
INBOX = "INBOX001"
TARGET = "X7DB9QVX"


@pytest.fixture
def fake(tmp_vault):
    from silica.kernel.vault_manifest import set_zotero
    f = FakeZotero()
    f.add_paper()
    set_zotero(CONFIG.vault_path, "library", SERVER_ID)
    tmp_vault.note(LEAF, "---\nzotero_key: 8H4AC8GW\n---\n\npaper\n")
    tmp_vault.note("Concepts/Virtual context.md",
                   "# Virtual context\nclaim\n\n## Sources\n\n[[sources/@packerMemGPTLLMsOperating2024]]\n")
    tmp_vault.note("Concepts/Paging.md",
                   "# Paging\nclaim\n\n## Sources\n\n[[sources/@packerMemGPTLLMsOperating2024]]\n")
    return f


def _zot(fake: FakeZotero, key: str | None = None) -> z.Zotero:
    return z.Zotero(server_id=SERVER_ID, api_key=fake.api_key if key is None else key,
                    transport=fake.transport())


def _notes(fake: FakeZotero) -> list[dict]:
    return [i for i in fake.items.values() if i["data"]["itemType"] == "note"]


def _ledger() -> list[dict]:
    p = Path(CONFIG.vault_path) / "zotero.jsonl"
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]


# --- back-link notes -----------------------------------------------------------

def test_a_backlink_note_lists_the_notes_that_cite_the_leaf(fake):
    status = zw.backlink(_zot(fake), "8H4AC8GW", LEAF, run_id="run1")

    [note] = _notes(fake)
    html = note["data"]["note"]
    assert status == "created"
    assert note["data"]["parentItem"] == "8H4AC8GW"
    assert [t["tag"] for t in note["data"]["tags"]] == ["silica"]
    assert "Silica · MemGPT: Towards LLMs as Operating Systems" in html
    assert "Paging" in html and "Virtual context" in html
    vault = Path(CONFIG.vault_path).name
    assert f"obsidian://open?vault={vault}&amp;file=sources%2F%40packerMemGPTLLMsOperating2024.md" in html
    assert _ledger()[-1]["action"] == "create_note"


def test_a_title_cannot_inject_html_into_the_note(fake):
    fake.items["8H4AC8GW"]["data"]["title"] = "<script>x</script> & co"

    zw.backlink(_zot(fake), "8H4AC8GW", LEAF, run_id="run1")

    [note] = _notes(fake)
    assert "<script>" not in note["data"]["note"]
    assert "&lt;script&gt;" in note["data"]["note"]


def test_a_backlink_note_untouched_since_silica_wrote_it_is_updated(fake):
    zw.backlink(_zot(fake), "8H4AC8GW", LEAF, run_id="run1")
    from silica.driver import DRIVER     # a later run's note, written as a run writes it
    DRIVER.create("Concepts/Memory tiers.md",
                  "# Memory tiers\n\n## Sources\n\n[[sources/@packerMemGPTLLMsOperating2024]]\n")

    status = zw.backlink(_zot(fake), "8H4AC8GW", LEAF, run_id="run2")

    [note] = _notes(fake)
    assert status == "updated"
    assert "Memory tiers" in note["data"]["note"]


def test_a_backlink_note_the_user_edited_is_never_rewritten(fake):
    zw.backlink(_zot(fake), "8H4AC8GW", LEAF, run_id="run1")
    [note] = _notes(fake)
    note["data"]["note"] = "<p>my own thoughts</p>"
    fake._bump(note)

    status = zw.backlink(_zot(fake), "8H4AC8GW", LEAF, run_id="run2")

    assert status == "edited"
    assert note["data"]["note"] == "<p>my own thoughts</p>"


def test_writing_without_write_access_says_how_to_grant_it(fake):
    with pytest.raises(z.ZoteroError, match="write access"):
        zw.backlink(_zot(fake, key=""), "8H4AC8GW", LEAF, run_id="run1")
    assert _notes(fake) == []


def test_every_write_names_the_linked_library(fake):
    zw.backlink(_zot(fake), "8H4AC8GW", LEAF, run_id="run1")

    writes = [r for r in fake.requests if r.method != "GET"]
    assert writes and all(r.headers["Zotero-Server-ID"] == SERVER_ID for r in writes)


# --- filing --------------------------------------------------------------------

def _inbox_item(fake: FakeZotero) -> dict:
    return fake.clone(fake.items["8H4AC8GW"], "NEWPAPER", collections=[INBOX], citationKey="new2026")


def test_filing_moves_an_item_out_of_the_inbox_and_records_where_it_was(fake):
    item = _inbox_item(fake)

    done = zw.file_items(_zot(fake), [("NEWPAPER", TARGET)], INBOX, run_id="run1")

    assert done == 1
    assert item["data"]["collections"] == [TARGET]
    line = _ledger()[-1]
    assert (line["action"], line["key"], line["before"]) == ("move", "NEWPAPER", [INBOX])


def test_revert_moves_a_filed_item_back(fake):
    item = _inbox_item(fake)
    zw.file_items(_zot(fake), [("NEWPAPER", TARGET)], INBOX, run_id="run1")

    res = zw.revert(_zot(fake), "run1")

    assert item["data"]["collections"] == [INBOX]
    assert res["undone"] == 1 and res["left"] == []


def test_revert_leaves_an_item_the_user_moved_since(fake):
    item = _inbox_item(fake)
    zw.file_items(_zot(fake), [("NEWPAPER", TARGET)], INBOX, run_id="run1")
    item["data"]["collections"] = ["ELSEWHER"]
    fake._bump(item)

    res = zw.revert(_zot(fake), "run1")

    assert item["data"]["collections"] == ["ELSEWHER"]
    assert [k for k, _ in res["left"]] == ["NEWPAPER"]


def test_revert_deletes_a_backlink_note_only_while_untouched(fake):
    zw.backlink(_zot(fake), "8H4AC8GW", LEAF, run_id="run1")

    res = zw.revert(_zot(fake), "run1")

    assert _notes(fake) == [] and res["undone"] == 1


def test_a_reverted_run_is_not_reverted_twice(fake):
    _inbox_item(fake)
    zw.file_items(_zot(fake), [("NEWPAPER", TARGET)], INBOX, run_id="run1")
    zw.revert(_zot(fake), "run1")

    assert zw.revert(_zot(fake), "run1")["undone"] == 0
    assert zw.last_run() is None


def test_filing_suggestions_vote_among_the_three_nearest_filed_items(fake):
    base = fake.items["8H4AC8GW"]
    fake.clone(base, "MEM00001", title="memory a", collections=["MEMORY01"])
    fake.clone(base, "MEM00002", title="memory b", collections=["MEMORY01"])
    fake.clone(base, "GRAPH001", title="graph a", collections=["GRAPHS01"])
    fake.clone(base, "GRAPH002", title="graph b", collections=["GRAPHS01"])
    fake.clone(base, "INBOXNEW", title="memory new", collections=[INBOX])
    del fake.items["8H4AC8GW"]
    vecs = {"memory a": [1, 0], "memory b": [0.9, 0.1], "graph a": [0, 1],
            "graph b": [0.1, 0.9], "memory new": [0.95, 0.2]}

    def embed(texts):
        return [vecs[t.split("\n")[0]] for t in texts]

    [s] = zw.suggest_filing(_zot(fake), INBOX, embed=embed)

    assert (s["key"], s["first"], s["votes"], s["second"]) == (
        "INBOXNEW", "MEMORY01", 2, "GRAPHS01")


# --- groups ----------------------------------------------------------------------

def test_groups_create_silica_groups_once_and_record_its_key(fake):
    plan = {"memory · agents": ["8H4AC8GW"]}

    zw.write_groups(_zot(fake), plan, run_id="run1")

    from silica.kernel.vault_manifest import load_manifest
    top = [c for c in fake.collections.values() if c["data"]["name"] == "Silica groups"]
    [sub] = [c for c in fake.collections.values() if c["data"]["name"] == "memory · agents"]
    assert len(top) == 1
    assert sub["data"]["parentCollection"] == top[0]["key"]
    assert load_manifest(CONFIG.vault_path).zotero.groups == top[0]["key"]
    assert sub["key"] in fake.items["8H4AC8GW"]["data"]["collections"]
    assert TARGET in fake.items["8H4AC8GW"]["data"]["collections"]   # the user's own stays


def test_rewriting_groups_moves_a_paper_between_silica_groups_only(fake):
    zw.write_groups(_zot(fake), {"old": ["8H4AC8GW"]}, run_id="run1")
    zw.write_groups(_zot(fake), {"new": ["8H4AC8GW"]}, run_id="run2")

    names = {c["key"]: c["data"]["name"] for c in fake.collections.values()}
    held = {names.get(k, k) for k in fake.items["8H4AC8GW"]["data"]["collections"]}
    assert held == {"new", TARGET}


def test_a_papers_group_is_the_zone_most_of_its_notes_hold():
    nodes = [{"id": "sources/@p.md", "type": "paper", "paper": {"zotero_key": "PPPP0001"}},
             {"id": "A.md", "sgroup": 1}, {"id": "B.md", "sgroup": 1}, {"id": "C.md", "sgroup": 2},
             {"id": "sources/@q.md", "type": "paper", "paper": {"zotero_key": "QQQQ0001"}},
             {"id": "D.md", "sgroup": -1}]
    edges = [{"from": n, "to": "sources/@p.md", "type": "EXTRACTED"} for n in ("A.md", "B.md", "C.md")]
    edges.append({"from": "D.md", "to": "sources/@q.md", "type": "EXTRACTED"})
    zones = {1: "memory · agents", 2: "graphs"}

    assert zw.group_plan(nodes, edges, zones) == {"memory · agents": ["PPPP0001"]}


# --- unlink and authorize ----------------------------------------------------------

def test_unlink_removes_silica_notes_and_groups_and_nothing_else(fake):
    zw.backlink(_zot(fake), "8H4AC8GW", LEAF, run_id="run1")
    zw.write_groups(_zot(fake), {"g": ["8H4AC8GW"]}, run_id="run1")
    mine = fake.note("8H4AC8GW", "<p>mine</p>", tags=())

    zw.unlink(_zot(fake))

    assert _notes(fake) == [mine]
    assert fake.collections == {}
    assert "8H4AC8GW" in fake.items


def test_always_allow_keeps_the_key_outside_the_vault(fake, tmp_path, monkeypatch):
    env = tmp_path / "user.env"
    monkeypatch.setattr("silica.onboarding.wizard.resolve_env_path", lambda: env)

    remember = zw.authorize(_zot(fake, key=""))

    assert remember is True
    assert f"SILICA_ZOTERO_API_KEY={fake.api_key}" in env.read_text(encoding="utf-8")
    assert os.stat(env).st_mode & 0o777 == 0o600  # the key opens the user's library


def test_a_single_use_key_is_not_kept(fake, tmp_path, monkeypatch):
    env = tmp_path / "user.env"
    monkeypatch.setattr("silica.onboarding.wizard.resolve_env_path", lambda: env)
    fake.authorize_answer = {"key": "s" * 32, "remember": False}

    assert zw.authorize(_zot(fake, key="")) is False
    assert not env.exists()
