# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0055 decision 9: detection at session open, writes on command. It
counts papers new in Zotero since the last session and PDFs that changed under
a paper already in the vault, and it writes nothing to the vault."""
from __future__ import annotations

import pytest

from silica.config import CONFIG
from silica.sources import zotero as z
from tests.zotero_fake import SERVER_ID, FakeZotero

LEAF = "sources/@packerMemGPTLLMsOperating2024.md"


@pytest.fixture
def fake(tmp_vault, monkeypatch):
    from silica.kernel.vault_manifest import set_zotero

    f = FakeZotero()
    f.add_paper()
    set_zotero(CONFIG.vault_path, "library", SERVER_ID)
    tmp_vault.note(LEAF, "---\nzotero_key: 8H4AC8GW\npdf_md5: 16e65f7cc5c1d647eb6acffdf085ce62\n---\n")
    monkeypatch.setattr(z, "_client_for", lambda vault, link: z.Zotero(
        server_id=link.library, transport=f.transport()))
    return f


def _new_paper(fake, key="NEWPAPER", added="2099-01-01T00:00:00Z"):
    item = fake.clone(fake.items["8H4AC8GW"], key, citationKey=key.lower(), dateAdded=added)
    fake._bump(item)
    return item


def test_the_first_session_sets_the_mark_and_reports_nothing(fake):
    assert z.detect(CONFIG.vault_path) == {"new": [], "changed": [], "other": ""}


def test_a_paper_added_since_the_last_session_is_new(fake):
    z.detect(CONFIG.vault_path)
    _new_paper(fake)

    assert z.detect(CONFIG.vault_path)["new"] == ["NEWPAPER"]
    assert z.last_detection(CONFIG.vault_path)["new"] == ["NEWPAPER"]


def test_a_changed_old_item_is_not_new(fake):
    """Silica's own filing bumps an item's version; that is not a new paper."""
    z.detect(CONFIG.vault_path)
    _new_paper(fake, key="OLDPAPER", added="2020-01-01T00:00:00Z")

    assert z.detect(CONFIG.vault_path)["new"] == []


def test_a_pdf_that_changed_under_a_vault_paper_is_reported(fake):
    z.detect(CONFIG.vault_path)
    att = fake.items["QH2BR53F"]
    att["data"]["md5"] = "0" * 32
    fake._bump(att)

    assert z.detect(CONFIG.vault_path)["changed"] == ["8H4AC8GW"]


def test_a_closed_zotero_is_silent(fake):
    fake.refuse = True

    assert z.detect(CONFIG.vault_path) is None


def test_another_library_is_named(fake):
    fake.server_id = "Qm41xTr8pZ0a"

    assert z.detect(CONFIG.vault_path) == {"new": [], "changed": [], "other": "Qm41xTr8pZ0a"}


# --- the Diagnostics row --------------------------------------------------------

def _check():
    from silica.onboarding.checks import check_zotero
    return check_zotero(CONFIG)


def test_diagnostics_warns_when_the_linked_zotero_is_closed(fake):
    fake.refuse = True

    r = _check()

    assert r.status == "warn" and "not running" in r.detail
    assert "Allow other applications" in r.hint


def test_diagnostics_warns_when_the_api_is_off(fake):
    fake.api_off = True

    r = _check()

    assert r.status == "warn" and "API" in r.detail


def test_diagnostics_names_a_running_zotero_and_its_access(fake):
    r = _check()

    assert r.status == "ok" and "10.0.5" in r.detail and "read only" in r.detail


def test_diagnostics_says_an_old_zotero_is_read_only(fake):
    fake.zotero_version = "8.0.1"

    assert "writes need Zotero 10" in _check().detail


def test_diagnostics_lists_zotero_only_for_a_linked_vault(fake):
    from silica.kernel.vault_manifest import unlink_zotero
    from silica.onboarding.checks import run_checks

    assert "zotero" in [r.name for r in run_checks(CONFIG)]
    unlink_zotero(CONFIG.vault_path)
    assert "zotero" not in [r.name for r in run_checks(CONFIG)]
