"""Where ingested raw material lands once the vault is the whole repo.

The inbox is Silica's own staging area, so it belongs inside the write boundary
like everything else Silica creates. `CONFIG.inbox_dir` knows nothing about
`write_dir`, so every path built straight from it dropped an `Inbox/` folder at
the root of the user's source tree; `active_inbox_dir()` is what composes the two.
"""
from silica.driver.fs_backend import ObsidianFSBackend


def test_the_inbox_stays_inside_the_write_boundary(tmp_path, monkeypatch):
    from silica.config import CONFIG
    from silica.kernel.vault_manifest import reset_manifest_cache

    (tmp_path / "vault.yaml").write_text("write_dir: docs/silica\n", encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    reset_manifest_cache()

    from silica.kernel.vault_manifest import active_inbox_dir

    assert active_inbox_dir() == f"docs/silica/{CONFIG.inbox_dir}"

    backend = ObsidianFSBackend(str(tmp_path))
    backend.upsert(f"{active_inbox_dir()}/materiale.md", "# grezzo")

    assert [str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.md")] == [
        f"docs/silica/{CONFIG.inbox_dir}/materiale.md"
    ]
    # And the staging area is still recognised as such at its new depth.
    from silica.kernel.recall.paths import is_inbox_path

    assert is_inbox_path(f"docs/silica/{CONFIG.inbox_dir}/materiale.md")


def test_no_boundary_leaves_the_inbox_at_the_vault_root(tmp_path, monkeypatch):
    from silica.config import CONFIG
    from silica.kernel.vault_manifest import active_inbox_dir, reset_manifest_cache

    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    reset_manifest_cache()

    assert active_inbox_dir() == CONFIG.inbox_dir
