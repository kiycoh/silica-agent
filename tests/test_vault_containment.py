"""Vault containment at the driver choke point.

`Path(vault) / rel` silently DISCARDS the vault root when `rel` is absolute and
joins a `../..` verbatim, so the FS backend used to write anywhere on the
filesystem for a create/overwrite/move whose path merely looked vault-relative.
Containment now lives in one helper (`contain_in_vault`) that every write goes
through, and it is decided on the resolved paths so a symlink out of the vault
escapes too.
"""
import pytest

from silica.driver.base import NoteRef
from silica.driver.fs_backend import ObsidianFSBackend
from silica.kernel.recall.paths import contain_in_vault


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    return v


@pytest.fixture
def outside(tmp_path):
    o = tmp_path / "outside"
    o.mkdir()
    return o


@pytest.fixture
def backend(vault):
    return ObsidianFSBackend(str(vault))


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------

def test_contain_accepts_relative_and_absolute_under_vault(vault):
    assert contain_in_vault("Sub/N.md", vault) == "Sub/N.md"
    assert contain_in_vault(str(vault / "Sub" / "N.md"), vault) == "Sub/N.md"


def test_contain_rejects_relative_escape(vault):
    with pytest.raises(ValueError):
        contain_in_vault("../escape.md", vault)
    with pytest.raises(ValueError):
        contain_in_vault("a/../../escape.md", vault)


def test_contain_rejects_absolute_outside(vault, outside):
    with pytest.raises(ValueError):
        contain_in_vault(str(outside / "evil.md"), vault)


def test_contain_rejects_symlink_escape(vault, outside):
    (vault / "link.md").symlink_to(outside / "evil.md")
    with pytest.raises(ValueError):
        contain_in_vault("link.md", vault)


def test_contain_rejects_the_vault_root_itself(vault):
    with pytest.raises(ValueError):
        contain_in_vault(str(vault), vault)
    with pytest.raises(ValueError):
        contain_in_vault("", vault)


def test_contain_does_not_require_existence(vault):
    assert contain_in_vault("never/created/N.md", vault) == "never/created/N.md"


def test_contain_keeps_an_intra_vault_symlinked_folder_as_the_key(vault):
    (vault / "Sub").mkdir()
    (vault / "Alias").symlink_to(vault / "Sub")
    assert contain_in_vault("Alias/N.md", vault) == "Alias/N.md"


@pytest.fixture
def hop(vault, outside):
    """A symlink out of the vault and one pointing back in — the pair that makes
    a lexical `..` collapse disagree with where the path really lands."""
    (vault / "Sub").mkdir()
    (vault / "out").symlink_to(outside)
    (outside / "back").symlink_to(vault / "Sub")
    return "out/back/../victim.md"


def test_contain_returns_the_path_it_validated(vault, outside, hop):
    # normpath cancels `back/..` textually, so the lexical form is `out/victim.md`
    # — rejoined onto the vault that writes through `out`, outside it.
    rel = contain_in_vault(hop, vault)
    assert (vault / rel).resolve() == (vault / "victim.md")
    assert not str((vault / rel).resolve()).startswith(str(outside))


def test_create_through_a_symlink_hop_stays_in_the_vault(backend, vault, outside, hop):
    backend.create(hop, "body")
    assert (vault / "victim.md").read_text(encoding="utf-8") == "body"
    assert not (outside / "victim.md").exists()


def test_move_destination_through_a_symlink_hop_stays_in_the_vault(
    backend, vault, outside, hop
):
    backend.create("N.md", "body")
    backend.move("N.md", hop)
    assert (vault / "victim.md").read_text(encoding="utf-8") == "body"
    assert not (outside / "victim.md").exists()


# ---------------------------------------------------------------------------
# The writes that do not take a path argument: append / set_prop / move sweep
# ---------------------------------------------------------------------------

def test_append_rejects_a_symlink_out_of_the_vault(backend, vault, outside):
    victim = outside / "evil.md"
    victim.write_text("original", encoding="utf-8")
    (vault / "link.md").symlink_to(victim)
    with pytest.raises(ValueError):
        backend.append(NoteRef(name="link", path="link.md"), "pwned")
    assert victim.read_text(encoding="utf-8") == "original"


def test_set_prop_rejects_a_symlink_out_of_the_vault(backend, vault, outside):
    victim = outside / "evil.md"
    victim.write_text("original", encoding="utf-8")
    (vault / "link.md").symlink_to(victim)
    with pytest.raises(ValueError):
        backend.set_prop(NoteRef(name="link", path="link.md"), "status", "done")
    assert victim.read_text(encoding="utf-8") == "original"


def test_move_does_not_rewrite_a_referrer_that_leaves_the_vault(
    backend, vault, outside
):
    foreign = outside / "referrer.md"
    foreign.write_text("see [[N]]", encoding="utf-8")
    backend.create("N.md", "body")
    (vault / "R.md").symlink_to(foreign)
    backend._rebuild_index()
    backend.move("N.md", "Renamed.md")
    assert foreign.read_text(encoding="utf-8") == "see [[N]]"
    assert (vault / "Renamed.md").exists()


# ---------------------------------------------------------------------------
# create()
# ---------------------------------------------------------------------------

def test_create_rejects_relative_escape(backend, outside):
    with pytest.raises(ValueError):
        backend.create("../outside/escape.md", "pwned")
    assert not (outside / "escape.md").exists()


def test_create_rejects_absolute_outside(backend, outside):
    with pytest.raises(ValueError):
        backend.create(str(outside / "evil.md"), "pwned")
    assert not (outside / "evil.md").exists()


def test_create_rejects_symlink_escape(backend, vault, outside):
    # Dangling on purpose: create()'s exists() guard follows the link, so a
    # symlink to a not-yet-existing target is the write-through vector.
    (vault / "link.md").symlink_to(outside / "evil.md")
    with pytest.raises(ValueError):
        backend.create("link.md", "pwned")
    assert not (outside / "evil.md").exists()


def test_create_accepts_absolute_under_vault(backend, vault):
    ref = backend.create(str(vault / "Sub" / "N.md"), "body")
    assert ref.path == "Sub/N.md"
    assert backend.read_note("Sub/N.md").content == "body"


def test_create_accepts_plain_relative(backend, vault):
    backend.create("N.md", "body")
    assert (vault / "N.md").read_text(encoding="utf-8") == "body"


# ---------------------------------------------------------------------------
# overwrite()
# ---------------------------------------------------------------------------

def test_overwrite_rejects_relative_escape(backend, outside):
    victim = outside / "escape.md"
    victim.write_text("original", encoding="utf-8")
    with pytest.raises(ValueError):
        backend.overwrite("../outside/escape.md", "pwned")
    assert victim.read_text(encoding="utf-8") == "original"


def test_overwrite_rejects_absolute_outside(backend, outside):
    victim = outside / "evil.md"
    victim.write_text("original", encoding="utf-8")
    with pytest.raises(ValueError):
        backend.overwrite(str(victim), "pwned")
    assert victim.read_text(encoding="utf-8") == "original"


def test_overwrite_rejects_symlink_escape(backend, vault, outside):
    victim = outside / "evil.md"
    victim.write_text("original", encoding="utf-8")
    (vault / "link.md").symlink_to(victim)
    with pytest.raises(ValueError):
        backend.overwrite("link.md", "pwned")
    assert victim.read_text(encoding="utf-8") == "original"


def test_overwrite_accepts_absolute_under_vault(backend, vault):
    backend.create("N.md", "v1")
    backend.overwrite(str(vault / "N.md"), "v2")
    assert backend.read_note("N.md").content == "v2"


# ---------------------------------------------------------------------------
# move()
# ---------------------------------------------------------------------------

def test_move_rejects_escaping_destination(backend, vault, outside):
    backend.create("N.md", "body")
    with pytest.raises(ValueError):
        backend.move("N.md", "../outside/escape.md")
    assert not (outside / "escape.md").exists()
    assert (vault / "N.md").exists()


def test_move_rejects_absolute_destination_outside(backend, vault, outside):
    backend.create("N.md", "body")
    with pytest.raises(ValueError):
        backend.move("N.md", str(outside / "evil.md"))
    assert not (outside / "evil.md").exists()
    assert (vault / "N.md").exists()


def test_move_rejects_source_outside_the_vault(backend, outside):
    victim = outside / "evil.md"
    victim.write_text("original", encoding="utf-8")
    with pytest.raises(ValueError):
        backend.move(NoteRef(name="evil", path=str(victim)), "Moved.md")
    assert victim.exists()


def test_move_rejects_symlinked_source(backend, vault, outside):
    victim = outside / "evil.md"
    victim.write_text("original", encoding="utf-8")
    (vault / "link.md").symlink_to(victim)
    with pytest.raises(ValueError):
        backend.move(NoteRef(name="link", path="link.md"), "Moved.md")
    assert victim.exists()


def test_move_accepts_absolute_destination_under_vault(backend, vault):
    backend.create("N.md", "body")
    backend.move("N.md", str(vault / "Sub" / "N.md"))
    assert (vault / "Sub" / "N.md").read_text(encoding="utf-8") == "body"
    assert not (vault / "N.md").exists()


def test_move_still_rewrites_referrer_links(backend, vault):
    backend.create("N.md", "body")
    backend.create("R.md", "see [[N]]")
    backend.move("N.md", "Renamed.md")
    assert "[[Renamed]]" in (vault / "R.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Atomic writes leave no debris
# ---------------------------------------------------------------------------

def test_note_writes_leave_no_temp_file(backend, vault):
    backend.create("N.md", "v1")
    backend.overwrite("N.md", "v2")
    backend.set_prop("N.md", "status", "done")
    assert sorted(p.name for p in vault.iterdir()) == ["N.md"]
    assert backend.read_note("N.md").content.endswith("v2")
