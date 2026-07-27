# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two axes that used to be one boolean: which folder is the vault (adopted
as-is) and where inside it Silica may write (`write_dir` in vault.yaml).

Codebase detection only picks the *default* for that declaration, so it is
allowed to be a heuristic; the boundary itself is enforced mechanically in
validate_operations, and never widens on a malformed manifest.
"""
import pytest

from silica.cli import resolve_vault_switch
from silica.kernel.paths import looks_like_code
from silica.kernel.vault_manifest import (
    active_write_dir,
    load_manifest,
    reset_manifest_cache,
    within,
)
from silica.onboarding.adopt import declare_write_dir


def _manifest(path, text):
    (path / "vault.yaml").write_text(text, encoding="utf-8")
    return load_manifest(str(path))


# --- codebase detection ------------------------------------------------------

def test_marker_file_alone_marks_a_source_tree(tmp_path):
    # Sources may all live under src/; the root manifest is the reliable signal.
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert looks_like_code(tmp_path) is True


def test_code_wins_on_ratio(tmp_path):
    for i in range(5):
        (tmp_path / f"mod{i}.py").write_text("x = 1\n")
    (tmp_path / "README.md").write_text("# readme")
    assert looks_like_code(tmp_path) is True


def test_prose_folder_with_a_stray_snippet_is_not_code(tmp_path):
    for i in range(5):
        (tmp_path / f"nota{i}.md").write_text("# nota")
    (tmp_path / "snippet.py").write_text("print(1)")
    assert looks_like_code(tmp_path) is False


def test_docx_folder_is_not_code(tmp_path):
    for i in range(3):
        (tmp_path / f"doc{i}.docx").write_bytes(b"PK\x03\x04")
    assert looks_like_code(tmp_path) is False


def test_vendored_javascript_does_not_make_a_notes_folder_a_codebase(tmp_path):
    deep = tmp_path / "node_modules" / "left-pad"
    deep.mkdir(parents=True)
    for i in range(50):
        (deep / f"index{i}.js").write_text("module.exports = 1")
    (tmp_path / "nota.md").write_text("# nota")
    assert looks_like_code(tmp_path) is False


def test_empty_and_missing_dirs_are_not_code(tmp_path):
    assert looks_like_code(tmp_path) is False
    assert looks_like_code(tmp_path / "nope") is False


# --- the declaration --------------------------------------------------------

def test_absent_write_dir_means_the_vault_root(tmp_path):
    assert load_manifest(str(tmp_path)).write_dir == ""
    assert _manifest(tmp_path, "sources: [prose]\n").write_dir == ""


@pytest.mark.parametrize("declared,expected", [
    ("docs/silica", "docs/silica"),
    ("  docs/silica  ", "docs/silica"),
    ("/docs/silica", None),
    ("../outside", None),
    ("docs/../../outside", None),
    ("C:/windows", None),
    ("42", None),
    ("[a, b]", None),
    (".", ""),
    ('""', ""),
])
def test_write_dir_parse(tmp_path, declared, expected):
    assert _manifest(tmp_path, f"write_dir: {declared}\n").write_dir == expected


def test_invalid_write_dir_never_degrades_to_the_whole_vault(tmp_path, monkeypatch):
    # The reason this field is top-level and not folded into a default: the
    # default IS the widest scope, so a broken declaration must not reach it.
    from silica.config import CONFIG

    _manifest(tmp_path, "write_dir: /etc\n")
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    reset_manifest_cache()
    assert active_write_dir() not in ("", ".")


def test_malformed_conventions_block_cannot_widen_the_boundary(tmp_path):
    # `conventions` collapses to defaults wholesale when it is not a mapping.
    # write_dir must survive that, which it only does by living outside it.
    m = _manifest(tmp_path, "write_dir: docs/silica\nconventions: nonsense\n")
    assert m.write_dir == "docs/silica"
    assert m.conventions.max_tags == 3  # block did fall back


def test_wiki_dir_outside_the_boundary_collapses_into_it(tmp_path):
    m = _manifest(tmp_path, "write_dir: docs/silica\nconventions:\n  wiki_dir: Wiki\n")
    assert m.conventions.wiki_dir == "docs/silica"


def test_undeclared_wiki_dir_inherits_the_boundary(tmp_path):
    # /wiki writes through commit_derived, which never sees validate: without
    # this, derived notes would land in the vault root, outside the boundary.
    assert _manifest(tmp_path, "write_dir: docs/silica\n").conventions.wiki_dir == "docs/silica"
    assert load_manifest(str(tmp_path)).conventions.wiki_dir == "docs/silica"


def test_undeclared_wiki_dir_stays_root_when_writing_in_place(tmp_path):
    assert _manifest(tmp_path, "sources: [prose]\n").conventions.wiki_dir == ""


def test_wiki_dir_inside_the_boundary_is_kept(tmp_path):
    m = _manifest(
        tmp_path, "write_dir: docs/silica\nconventions:\n  wiki_dir: docs/silica/wiki\n"
    )
    assert m.conventions.wiki_dir == "docs/silica/wiki"


def test_within_is_segment_wise():
    assert within("docs/silica/A.md", "docs/silica")
    assert within("anything.md", "")  # "" is the whole vault
    assert not within("docs/silicate/A.md", "docs/silica")
    assert not within("A.md", "docs/silica")


# --- which folder is the vault ----------------------------------------------

@pytest.mark.parametrize("layout", [
    lambda p: None,                                             # bare directory
    lambda p: (p / "vault.yaml").write_text("write_dir: docs/silica\n"),
    lambda p: (p / "docs" / "silica" / "nota.md").parent.mkdir(parents=True),
])
def test_the_vault_is_always_the_path_you_named(tmp_path, layout):
    # No layout under a directory can make Silica open a different one. The
    # resolver that used to answer `docs/silica` is why the vault was something
    # you reconstructed instead of read off the screen.
    layout(tmp_path)
    assert resolve_vault_switch(str(tmp_path)).vault == str(tmp_path.resolve())


# --- adoption ---------------------------------------------------------------

def test_declare_write_dir_confines_a_source_tree(tmp_path):
    (tmp_path / "package.json").write_text("{}")

    assert declare_write_dir(tmp_path) == "docs/silica"
    assert (tmp_path / "vault.yaml").read_text(encoding="utf-8") == "write_dir: docs/silica\n"
    assert not (tmp_path / "docs").exists()  # declaring is not creating


def test_a_pre_write_dir_layout_declares_itself(tmp_path):
    # The migration for a vault created before the split: same declaration every
    # new repo gets, so the notes stay put and the repo becomes readable again.
    (tmp_path / "docs" / "silica").mkdir(parents=True)
    (tmp_path / "docs" / "silica" / "nota.md").write_text("# nota")

    assert declare_write_dir(tmp_path) == "docs/silica"
    assert resolve_vault_switch(str(tmp_path)).vault == str(tmp_path.resolve())


def test_declare_write_dir_leaves_prose_in_place_and_file_free(tmp_path):
    (tmp_path / "nota.md").write_text("# nota")
    assert declare_write_dir(tmp_path) is None
    assert not (tmp_path / "vault.yaml").exists()


def test_declare_write_dir_never_overrules_an_existing_manifest(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "vault.yaml").write_text("write_dir: notes\n", encoding="utf-8")

    assert declare_write_dir(tmp_path) is None
    assert load_manifest(str(tmp_path)).write_dir == "notes"


def test_declare_write_dir_leaves_an_obsidian_vault_alone(tmp_path):
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")  # docs repo
    assert declare_write_dir(tmp_path) is None


# --- enforcement ------------------------------------------------------------

@pytest.fixture
def bounded_vault(tmp_vault, monkeypatch):
    """A vault that writes only under docs/silica."""
    from pathlib import Path

    from silica.config import CONFIG

    monkeypatch.setenv("SILICA_MIN_WRITE_SNIPPET_CHARS", "10")
    root = Path(CONFIG.vault_path)
    (root / "vault.yaml").write_text("write_dir: docs/silica\n", encoding="utf-8")
    reset_manifest_cache()
    return tmp_vault


def _validate(ops):
    from silica.kernel.validate import validate_operations

    return validate_operations(ops, [], "")


def _op(op, **kw):
    # heading/source_basename are required on every Op, payload gate or not.
    return {"op": op, "heading": "Concetto", "source_basename": "src.md", **kw}


def _write(path):
    return _op("write", path=path, snippet="corpo della nota " * 10)


def test_a_new_note_aimed_outside_is_filed_inside_the_boundary(bounded_vault):
    validated, rejected = _validate([_write("Concepts/Foo.md")])

    assert rejected == []
    assert [o.path for o in validated] == ["docs/silica/Concepts/Foo.md"]


def test_a_new_note_already_inside_is_untouched(bounded_vault):
    validated, rejected = _validate([_write("docs/silica/Foo.md")])

    assert rejected == []
    assert [o.path for o in validated] == ["docs/silica/Foo.md"]


def test_patching_a_file_outside_the_boundary_is_rejected(bounded_vault):
    # The whole point on a source tree: README.md is readable context, not ours.
    bounded_vault.note("README.md", "# readme")

    validated, rejected = _validate(
        [_op("patch", path="README.md", snippet="x" * 40)]
    )

    assert validated == []
    assert "outside the vault write boundary" in rejected[0].reason


def test_deleting_a_file_outside_the_boundary_is_rejected(bounded_vault):
    bounded_vault.note("src/main.py", "print(1)")

    validated, rejected = _validate([_op("delete", path="src/main.py")])

    assert validated == []
    assert "outside the vault write boundary" in rejected[0].reason


def test_a_move_may_not_leave_the_boundary(bounded_vault):
    bounded_vault.note("docs/silica/A.md", "# a")

    validated, rejected = _validate(
        [_op("move", from_path="docs/silica/A.md", to_path="elsewhere/A.md")]
    )

    assert validated == []
    assert "outside the vault write boundary" in rejected[0].reason


def test_a_vault_that_declares_nothing_is_unaffected(tmp_vault, monkeypatch):
    monkeypatch.setenv("SILICA_MIN_WRITE_SNIPPET_CHARS", "10")
    reset_manifest_cache()

    validated, rejected = _validate([_write("Concepts/Foo.md")])

    assert rejected == []
    assert [o.path for o in validated] == ["Concepts/Foo.md"]  # no rebase


def test_a_broken_declaration_rejects_every_write(tmp_vault, monkeypatch):
    from pathlib import Path

    from silica.config import CONFIG

    monkeypatch.setenv("SILICA_MIN_WRITE_SNIPPET_CHARS", "10")
    (Path(CONFIG.vault_path) / "vault.yaml").write_text("write_dir: /etc\n", encoding="utf-8")
    reset_manifest_cache()

    validated, rejected = _validate(
        [_op("patch", path="A.md", snippet="x" * 40)]
    )

    assert validated == []
    assert rejected


def test_backlinks_never_edit_a_note_outside_the_boundary(bounded_vault):
    # autolink rewrites existing notes through DRIVER directly, bypassing
    # validate, so it carries the boundary itself.
    from silica.kernel.autolink import backlink_pass

    readme = bounded_vault.note("README.md", "This project uses Kubernetes for scheduling.\n")
    bounded_vault.note("docs/silica/Notes.md", "Kubernetes shows up here too.\n")

    touched = backlink_pass(
        ["Kubernetes"],
        title_index=["Kubernetes"],
        neighbourhood=["README.md", "docs/silica/Notes.md"],
    )

    assert list(touched) == ["docs/silica/Notes.md"]
    assert "[[" not in bounded_vault.read(readme)  # source tree left untouched


# --- reading a vault adopted as-is ------------------------------------------

def test_the_index_skips_vendored_trees(tmp_vault):
    import silica.driver as driver_pkg

    tmp_vault.note("nota.md", "# nota")
    tmp_vault.note("node_modules/left-pad/README.md", "# vendored")
    tmp_vault.note("build/out/GENERATED.md", "# generated")
    driver_pkg._driver = None

    files = driver_pkg.get_driver().list_files()

    paths = [f.path for f in files]
    assert any(p.endswith("nota.md") for p in paths)
    assert not any("node_modules" in p or "build/out" in p for p in paths)
