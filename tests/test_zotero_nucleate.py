# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0055 decision 4: a Zotero target enters /nucleate's converter queue with
its paper's leaf; a paper with no PDF is written as its leaf on the spot."""
from __future__ import annotations

from pathlib import Path

import pytest

from silica import cli
from silica.config import CONFIG
from silica.sources import registry
from silica.sources import zotero as z
from tests.zotero_fake import SERVER_ID, FakeZotero

_ENABLED = ("prose", "zotero")
_PDF = ("/home/user/Zotero/storage/QH2BR53F/"
        "Packer et al. - 2024 - MemGPT Towards LLMs as Operating Systems.pdf")


@pytest.fixture
def fake(monkeypatch):
    f = FakeZotero()
    import os
    # Like the real client(): the write key comes from the environment at call time.
    monkeypatch.setattr(z, "client", lambda: z.Zotero(
        server_id=SERVER_ID, api_key=os.getenv("SILICA_ZOTERO_API_KEY", ""), transport=f.transport()))
    return f


def test_a_citekey_dispatches_to_zotero_only_where_zotero_is_on():
    assert registry.adapter_for("@packerMemGPTLLMsOperating2024", _ENABLED) is z.ZOTERO
    assert registry.adapter_for("@packerMemGPTLLMsOperating2024", ("prose",)) is None


def test_a_zotero_target_queues_its_pdf_with_its_leaf(tmp_vault, fake):
    fake.add_paper()

    plan = cli._nucleate_plan_units(["@packerMemGPTLLMsOperating2024"], _ENABLED, {}, None)

    assert plan.to_convert == [_PDF]
    name, fields = plan.leaf_for[_PDF]
    assert name == "@packerMemGPTLLMsOperating2024.md"
    assert fields["zotero_key"] == "8H4AC8GW"


def test_a_renamed_citekey_keeps_the_leaf_the_paper_already_has(tmp_vault, fake):
    """The provenance ledger keys sources by name: a new name for the same
    paper would read as a second source."""
    fake.add_paper()
    tmp_vault.note("sources/@oldKey2024.md", "---\nzotero_key: 8H4AC8GW\n---\n\nold text\n")

    plan = cli._nucleate_plan_units(["@packerMemGPTLLMsOperating2024"], _ENABLED, {}, None)

    assert plan.leaf_for[_PDF][0] == "@oldKey2024.md"


def test_a_paper_without_a_pdf_is_written_as_its_leaf(tmp_vault, fake):
    item, _ = fake.add_paper()
    del item["links"]["attachment"]

    plan = cli._nucleate_plan_units(["zotero://select/library/items/8H4AC8GW"], _ENABLED, {}, None)

    assert plan.to_convert == []
    assert plan.staged == 1
    leaf = Path(CONFIG.vault_path) / "sources/@packerMemGPTLLMsOperating2024.md"
    assert "zotero_key: 8H4AC8GW" in leaf.read_text(encoding="utf-8")


def test_a_closed_zotero_skips_the_target_and_says_why(tmp_vault, fake, capsys):
    fake.refuse = True

    plan = cli._nucleate_plan_units(["@packerMemGPTLLMsOperating2024"], _ENABLED, {}, None)

    assert plan.to_convert == [] and plan.ready_units == []
    assert "not running" in capsys.readouterr().out


def test_the_pipeline_hands_the_leaf_to_the_converter(tmp_vault, fake, monkeypatch):
    from silica.kernel.vault_manifest import set_zotero
    set_zotero(CONFIG.vault_path, "library", SERVER_ID)
    fake.add_paper()
    seen: list = []

    def _convert(target, dest_dir="", leaf=None):
        seen.append((target, leaf[0] if leaf else None))
        return []

    monkeypatch.setattr("silica.sources.convert.convert", _convert)

    cli._sc_nucleate(["@packerMemGPTLLMsOperating2024"])

    assert seen == [(_PDF, "@packerMemGPTLLMsOperating2024.md")]


def test_an_imported_paper_gets_its_backlink_note_in_zotero(tmp_vault, fake, monkeypatch):
    """ADR-0055 decision 7: one `silica`-tagged child note per imported paper,
    written after the run, so it can list the notes the run produced."""
    from silica.kernel.vault_manifest import set_zotero
    import silica.router.coordinator as coord_mod

    set_zotero(CONFIG.vault_path, "library", SERVER_ID)
    monkeypatch.setenv("SILICA_ZOTERO_API_KEY", fake.api_key)
    fake.add_paper()
    seg = "Inbox/@packerMemGPTLLMsOperating2024.md"

    def _convert(target, dest_dir="", leaf=None):
        tmp_vault.note(f"sources/{leaf[0]}", "---\nzotero_key: 8H4AC8GW\n---\n\npaper\n")
        tmp_vault.note(seg, f'---\nsource_leaf: "{leaf[0]}"\n---\nwords\n')
        return [seg]

    class _Run:
        def __init__(self, **kw):
            pass

        def run(self):
            return {"final_status": "Success"}

    monkeypatch.setattr("silica.sources.convert.convert", _convert)
    monkeypatch.setattr(coord_mod, "Coordinator", _Run)
    monkeypatch.setattr(cli, "_pick_target_folder", lambda files: "Papers")

    cli._sc_nucleate(["@packerMemGPTLLMsOperating2024", "--target=Papers"])

    notes = [i for i in fake.items.values() if i["data"]["itemType"] == "note"]
    assert [n["data"]["parentItem"] for n in notes] == ["8H4AC8GW"]
