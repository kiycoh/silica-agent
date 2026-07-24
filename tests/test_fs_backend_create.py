"""create() must honor the base contract: "Raises if file exists".

The FS backend used to write_text unconditionally, so a write op validated
while the note was absent would silently clobber a note created in the
validate->execute window (footgun: TOCTOU data loss with no conflict trace).
The WS backend already raises (Obsidian vault.create throws on existing).
"""
import pytest
from silica.driver.fs_backend import ObsidianFSBackend


@pytest.fixture
def backend(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return ObsidianFSBackend(str(vault))


def test_create_raises_on_existing_path(backend):
    backend.create("N.md", "original")
    with pytest.raises(FileExistsError):
        backend.create("N.md", "clobber")
    assert backend.read_note("N.md").content == "original"


def test_create_still_creates_missing_note(backend):
    ref = backend.create("Sub/New.md", "body")
    assert ref.path == "Sub/New.md"
    assert backend.read_note("Sub/New.md").content == "body"


def test_upsert_creates_then_overwrites(backend):
    backend.upsert("U.md", "v1")
    assert backend.read_note("U.md").content == "v1"
    backend.upsert("U.md", "v2")
    assert backend.read_note("U.md").content == "v2"
