"""FS backend create() patches the in-memory index atomically."""
from silica.driver.fs_backend import ObsidianFSBackend


def test_fs_create_patches_index(tmp_path):
    """FS create() atomically patches the index — no settle wait needed."""
    backend = ObsidianFSBackend(vault_path=str(tmp_path))
    # A pre-existing note + rebuild puts the index in the "built" state so
    # create() takes the _patch_index fast path (not a full reindex). The
    # created note must be a NEW path — create raises on existing ones.
    (tmp_path / "other.md").write_text("", encoding="utf-8")
    backend._rebuild_index()

    ref = backend.create("test.md", "some content with [[Missing]]")
    assert ref.path == "test.md"
    # _patch_index registered the new note and its (unresolved) link atomically
    assert "test.md" in backend._notes
    assert ("test.md", "Missing") in backend._unresolved_links


def test_fs_create_into_inbox_is_indexed(tmp_path, monkeypatch):
    """_patch_index indexes inbox paths, mirroring _rebuild_index.

    Staging notes are source material: they belong in the index so the file
    tree, search and recall can reach them. What must never happen to them is
    being TARGETED by an op, and that is `is_inbox_path`'s job, not the index's.
    """
    from silica.config import CONFIG
    from silica.kernel.recall.paths import is_inbox_path
    monkeypatch.setattr(CONFIG, "inbox_dir", "Inbox")
    backend = ObsidianFSBackend(vault_path=str(tmp_path))
    backend._rebuild_index()

    backend.create("Inbox/clip.md", "raw clipped content")
    assert "Inbox/clip.md" in backend._notes
    assert "Inbox/clip.md" in backend._graph
    assert is_inbox_path("Inbox/clip.md")


def test_resolve_path_never_escapes_vault_via_cwd(tmp_path, monkeypatch):
    """A note name colliding with a DIRECTORY in the process cwd must resolve
    inside the vault, not to the cwd path (post-mortem: hub 'memory' resolved
    to the repo's ./memory/ dir and read_text blew up with IsADirectoryError)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    cwd = tmp_path / "cwd"
    (cwd / "memory").mkdir(parents=True)
    monkeypatch.chdir(cwd)

    backend = ObsidianFSBackend(vault_path=str(vault))
    backend._rebuild_index()

    resolved = backend._resolve_path("memory")
    assert resolved == vault / "memory.md"

    # A real FILE in cwd is still a legitimate direct-path read (CLI ingest).
    (cwd / "doc.md").write_text("x", encoding="utf-8")
    assert backend._resolve_path("doc").resolve() == (cwd / "doc.md").resolve()
