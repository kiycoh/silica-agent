"""`silica prune`: the index dirs whose folder is gone are listed, and removed
only with --apply; a folder on a drive that is not mounted is never one of them."""
from __future__ import annotations

import json

import pytest

from silica.kernel.recall import vault_registry as reg


@pytest.fixture
def index(tmp_path, monkeypatch):
    """~/.silica/index under the suite's sandboxed home, one dir per case."""
    from silica.kernel.recall.paths import _SILICA_HOME

    media = tmp_path / "media"
    (media / "me").mkdir(parents=True)
    (media / "me" / "Mounted").mkdir()
    monkeypatch.setattr(reg, "_MEDIA_ROOTS", (media,))
    real_ismount = reg.os.path.ismount
    monkeypatch.setattr(reg.os.path, "ismount", lambda p: str(p) == str(media / "me" / "Mounted") or real_ismount(p))
    live = tmp_path / "live-vault"
    live.mkdir()
    cases = {
        "live": ("vault.json", live),
        "deleted": ("vault.json", tmp_path / "deleted-vault"),
        "core-root": ("root.json", tmp_path / "deleted-repo"),
        "unplugged": ("vault.json", media / "me" / "Unplugged" / "vault"),
        "off-mounted": ("vault.json", media / "me" / "Mounted" / "removed"),
    }
    base = _SILICA_HOME / "index"
    for name, (marker, folder) in cases.items():
        (base / name).mkdir(parents=True)
        (base / name / marker).write_text(json.dumps({"path": str(folder)}), encoding="utf-8")
        (base / name / "lexical.json").write_bytes(b"x" * 1024)
    (base / "no-marker").mkdir()
    (base / "torn").mkdir()
    (base / "torn" / "vault.json").write_text("{", encoding="utf-8")
    return base


def test_only_dirs_naming_a_missing_folder_are_orphans(index):
    gone, absent = reg.orphan_indexes()
    assert sorted(d.name for d, _f in gone) == ["core-root", "deleted", "off-mounted"]
    assert [d.name for d, _f in absent] == ["unplugged"], "a drive left unplugged is not a deleted vault"


def test_prune_lists_by_default_and_removes_with_apply(index):
    from silica.cli import _dispatch_subcommand

    assert _dispatch_subcommand(["prune"]) == 0
    assert all((index / n).exists() for n in ("deleted", "core-root", "off-mounted")), "the default only lists"
    assert _dispatch_subcommand(["prune", "--apply"]) == 0
    assert sorted(p.name for p in index.iterdir()) == ["live", "no-marker", "torn", "unplugged"]
