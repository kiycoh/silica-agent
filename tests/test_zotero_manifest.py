# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0055 decision 2: the `zotero:` block in vault.yaml is the switch and the
settings. Turning Zotero on must never touch `sources:`, whose absence means
the derived defaults."""
from __future__ import annotations

from pathlib import Path

from silica.kernel.vault_manifest import (
    MANIFEST_REL,
    load_manifest,
    set_zotero,
    unlink_zotero,
)


def _yaml(vault: Path, text: str) -> Path:
    p = vault / MANIFEST_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_a_zotero_block_adds_zotero_to_the_derived_sources(tmp_path):
    _yaml(tmp_path, "zotero:\n  library: dc8XI1gaCC7j\n  inbox: 00_inbox\n")

    m = load_manifest(tmp_path)

    assert m.sources == ("prose", "zotero")
    assert m.zotero.library == "dc8XI1gaCC7j"
    assert m.zotero.inbox == "00_inbox"
    assert m.zotero.backlinks is True


def test_no_block_means_no_zotero(tmp_path):
    _yaml(tmp_path, "write_dir: silica\n")

    m = load_manifest(tmp_path)

    assert m.zotero is None
    assert "zotero" not in m.sources


def test_a_block_without_a_library_is_ignored(tmp_path):
    _yaml(tmp_path, "zotero:\n  inbox: 00_inbox\n")

    assert load_manifest(tmp_path).zotero is None


def test_set_zotero_appends_the_block_and_keeps_the_file(tmp_path):
    p = _yaml(tmp_path, "# my vault\nwrite_dir: silica  # confined\n")

    set_zotero(tmp_path, "library", "dc8XI1gaCC7j")

    text = p.read_text(encoding="utf-8")
    assert text.startswith("# my vault\nwrite_dir: silica  # confined\n")
    m = load_manifest(tmp_path)
    assert m.zotero.library == "dc8XI1gaCC7j"
    assert m.write_dir == "silica"


def test_set_zotero_replaces_one_key_and_leaves_the_others(tmp_path):
    _yaml(tmp_path, "zotero:\n  library: dc8XI1gaCC7j  # linked 2026-10-05\n"
                    "  inbox: 00_inbox\nwrite_dir: silica\n")

    set_zotero(tmp_path, "inbox", "20_thesis")
    set_zotero(tmp_path, "backlinks", False)

    m = load_manifest(tmp_path)
    assert (m.zotero.library, m.zotero.inbox, m.zotero.backlinks) == (
        "dc8XI1gaCC7j", "20_thesis", False)
    assert m.write_dir == "silica"
    assert "# linked 2026-10-05" in (tmp_path / MANIFEST_REL).read_text(encoding="utf-8")


def test_unlink_removes_only_the_block(tmp_path):
    _yaml(tmp_path, "zotero:\n  library: dc8XI1gaCC7j\n\n  inbox: 00_inbox\n"
                    "write_dir: silica\n")

    unlink_zotero(tmp_path)

    m = load_manifest(tmp_path)
    assert m.zotero is None
    assert m.write_dir == "silica"
    assert "inbox" not in (tmp_path / MANIFEST_REL).read_text(encoding="utf-8")
