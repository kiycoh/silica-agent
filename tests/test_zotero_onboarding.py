# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0055 "Turning it on": `silica init` asks one question when Zotero
answers, and one library links to one vault (decision 10)."""
from __future__ import annotations


import pytest

from silica.kernel.vault_manifest import load_manifest, set_zotero
from silica.onboarding import wizard
from silica.sources import zotero as z
from tests.zotero_fake import SERVER_ID, FakeZotero


@pytest.fixture
def fake(monkeypatch):
    f = FakeZotero()
    monkeypatch.setattr(z, "Zotero", _Bound(f))
    monkeypatch.setattr("silica.kernel.recall.vault_registry.known_vaults", lambda: [])
    return f


class _Bound:
    """z.Zotero bound to the fake's transport, whatever else the caller passes."""

    def __init__(self, fake):
        self.fake = fake
        self.real = z.Zotero

    def __call__(self, *a, **kw):
        kw["transport"] = self.fake.transport()
        return self.real(*a, **kw)


def test_init_links_the_vault_when_the_user_says_yes(fake, tmp_path):
    wizard._offer_zotero(lambda prompt: "y", tmp_path)

    assert load_manifest(tmp_path).zotero.library == SERVER_ID


def test_init_asks_nothing_when_zotero_is_closed(fake, tmp_path):
    fake.refuse = True
    asked = []

    wizard._offer_zotero(lambda prompt: asked.append(prompt) or "y", tmp_path)

    assert asked == [] and load_manifest(tmp_path).zotero is None


def test_a_library_linked_to_another_vault_is_not_linked_twice(fake, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    set_zotero(other, "library", SERVER_ID)
    monkeypatch.setattr("silica.kernel.recall.vault_registry.known_vaults", lambda: [other])
    here = tmp_path / "here"
    here.mkdir()

    wizard._offer_zotero(lambda prompt: "y", here)

    assert load_manifest(here).zotero is None
    assert z.linked_elsewhere(SERVER_ID, here) == other
