"""The `documents:` write channel on the single-note tools.

Binding a note to a repo path is the one thing grep cannot do, and until now
only silica_document and /wiki could set it. These tests pin the trust boundary
(no absolute paths, no traversal, no phantom bindings) and the stamping on both
template branches.
"""
from __future__ import annotations

import subprocess

import pytest

import silica.kernel.write.checkpoints as checkpoints
from silica.kernel.recall import paths
from silica.tools.notes import silica_patch_note, silica_write_note


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "c1"], cwd=tmp_path, check=True)

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    monkeypatch.setattr("silica.config.CONFIG.vault_path", str(vault_dir))
    monkeypatch.setattr("silica.driver._driver", None)
    monkeypatch.setattr("silica.kernel.write.checkpoints._store", None)
    checkpoints.get_checkpoint_store(tmp_path / "checkpoints.db")
    paths.clear_repo_root_cache()
    yield vault_dir
    monkeypatch.setattr("silica.driver._driver", None)
    monkeypatch.setattr("silica.kernel.write.checkpoints._store", None)
    paths.clear_repo_root_cache()


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside.py", "C:/win.py"])
def test_validation_rejects_paths_outside_the_repo(vault, bad):
    res = silica_write_note(path="B.md", body="why", documents=[bad])
    assert "error" in res and bad in res["error"]
    assert not (vault / "B.md").exists()      # rejected before any write


def test_validation_rejects_nonexistent_entry(vault):
    res = silica_write_note(path="B.md", body="why", documents=["src/ghost.py"])
    assert "error" in res and "src/ghost.py" in res["error"]
    assert not (vault / "B.md").exists()


def test_documents_and_code_ref_land_on_template_branch(vault):
    res = silica_write_note(path="Why/M.md", body="the rationale",
                            documents=["src/m.py"])
    assert res.get("success"), res
    head = (vault / "Why" / "M.md").read_text(encoding="utf-8").split("\n---\n")[0]
    assert 'documents:\n  - "src/m.py"' in head
    assert "code_ref: " in head               # file binding → staleness tracked


def test_documents_land_on_template_none(vault):
    res = silica_write_note(path="Raw.md", body="# Raw\n\nbody\n",
                            template="none", documents=["src/m.py"])
    assert res.get("success"), res
    landed = (vault / "Raw.md").read_text(encoding="utf-8")
    assert landed.count("---\n") == 2         # exactly one frontmatter block
    assert 'documents:\n  - "src/m.py"' in landed
    assert "# Raw\n\nbody" in landed


def test_directory_binding_gets_no_code_ref(vault):
    # a rationale bound to a package does not expire when a file under it moves
    res = silica_write_note(path="Pkg.md", body="why", documents=["src"])
    assert res.get("success"), res
    head = (vault / "Pkg.md").read_text(encoding="utf-8").split("\n---\n")[0]
    assert 'documents:\n  - "src"' in head
    assert "code_ref:" not in head


def test_patch_adds_binding_without_touching_body(vault):
    silica_write_note(path="N.md", body="original prose")
    before = (vault / "N.md").read_text(encoding="utf-8")
    res = silica_patch_note(name="N.md", heading="Why", snippet="the reason",
                            source_basename="chat", documents=["src/m.py"])
    assert "error" not in res, res
    after = (vault / "N.md").read_text(encoding="utf-8")
    assert "original prose" in after
    assert 'documents:\n  - "src/m.py"' in after
    assert "code_ref: " in after
    assert after.split("\n---\n")[1] != before.split("\n---\n")[1]  # body appended


def test_patch_merges_into_an_existing_binding(vault):
    (vault / "src2").mkdir(parents=True, exist_ok=True)
    silica_write_note(path="N.md", body="prose", documents=["src"])
    silica_patch_note(name="N.md", heading="Why", snippet="more",
                      source_basename="chat", documents=["src/m.py"])
    head = (vault / "N.md").read_text(encoding="utf-8").split("\n---\n")[0]
    assert 'documents:\n  - "src"\n  - "src/m.py"' in head
    assert head.count("documents:") == 1


def test_stamp_write_invalidates_the_snapshot(vault, tmp_path):
    """Re-badging a note must not leave a false stale entry in the cache."""
    from silica.kernel.code import codedocs

    cache = codedocs._snapshot_path(vault)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{"head": "x", "docs": []}', encoding="utf-8")

    res = silica_write_note(path="Plain.md", body="no binding here")
    assert res.get("success"), res
    assert cache.exists()                 # plain writes do not touch the cache

    res = silica_write_note(path="B.md", body="why", documents=["src/m.py"])
    assert res.get("success"), res
    assert not cache.exists()             # the stamp path unlinks it


def test_patch_stamp_invalidates_the_snapshot(vault, tmp_path):
    from silica.kernel.code import codedocs

    silica_write_note(path="N.md", body="prose")
    cache = codedocs._snapshot_path(vault)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{"head": "x", "docs": []}', encoding="utf-8")

    res = silica_patch_note(name="N.md", heading="Why", snippet="the reason",
                            source_basename="chat", documents=["src/m.py"])
    assert "error" not in res, res
    assert not cache.exists()


# --- props: caller-supplied frontmatter keys ---------------------------------
# /learn's contract says "write ONE syllabus note: frontmatter `type: syllabus`
# + `target:`" — and no tool could write either. The syllabus landed as
# `type: Note` and every /learn rebuilt the plan from scratch.

def test_props_land_in_frontmatter(vault):
    res = silica_write_note(
        path="Syllabus - TCP.md", body="- [ ] step 1",
        props={"type": "syllabus", "target": "congestione TCP"},
    )
    assert res.get("success") is True
    text = (vault / "Syllabus - TCP.md").read_text(encoding="utf-8")
    fm = text.split("---")[1]
    assert "type: syllabus" in fm
    assert "target: congestione TCP" in fm


def test_props_reserved_keys_are_rejected(vault):
    """`AI:`, `last modified:` and `verified:` are the system floor and the
    human trust tier — a model writing them would hand the agent a lever on
    its own authority (OKF §5.2)."""
    for key in ("AI", "last modified", "verified"):
        res = silica_write_note(path=f"X-{key}.md", body="b", props={key: "x"})
        assert "error" in res, key
        assert not (vault / f"X-{key}.md").exists()
