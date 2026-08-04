# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two axes that used to be one boolean: which folder is the vault (adopted
as-is) and where inside it Silica may write (`write_dir` in vault.yaml).

Codebase detection only picks the *default* for that declaration, so it is
allowed to be a heuristic; the boundary itself is enforced mechanically in
validate_operations, and never widens on a malformed manifest.
"""
import pytest

from silica.cli import resolve_vault_switch
from silica.kernel.recall.paths import looks_like_code
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


def test_declare_write_dir_stages_prose_in_the_mirror(tmp_path):
    # Safe mode is the default: prose is confined to `silica/` rather than
    # filed in place, and the declaration says so out loud.
    (tmp_path / "nota.md").write_text("# nota")
    assert declare_write_dir(tmp_path) == "silica"
    assert (tmp_path / "vault.yaml").read_text(encoding="utf-8") == "write_dir: silica\n"
    assert not (tmp_path / "silica").exists()  # declaring is not creating


def test_declare_write_dir_never_overrules_an_existing_manifest(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "vault.yaml").write_text("write_dir: notes\n", encoding="utf-8")

    assert declare_write_dir(tmp_path) is None
    assert load_manifest(str(tmp_path)).write_dir == "notes"


def test_an_obsidian_vault_is_confined_like_any_prose_vault(tmp_path):
    # The early-return that used to force "" here is gone: being an Obsidian
    # vault is exactly the case safe mode exists for — someone's own notes.
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / "nota.md").write_text("# nota")
    assert declare_write_dir(tmp_path) == "silica"


def test_safe_mode_never_overrules_a_code_vaults_own_boundary(tmp_path):
    # Toggling safe mode on re-derives; a source tree keeps docs/silica, which
    # is Silica's own folder in that repo and not a mirror of it.
    from silica.onboarding.adopt import write_dir_for

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert write_dir_for(tmp_path) == "docs/silica"


# --- the safe-mode toggle ----------------------------------------------------

def test_toggle_round_trip_keeps_the_rest_of_the_manifest(tmp_path):
    from silica.kernel.vault_manifest import set_write_dir

    (tmp_path / "vault.yaml").write_text(
        "# hand-written\nsources: [prose]\nwrite_dir: silica\n", encoding="utf-8"
    )

    set_write_dir(tmp_path, "")
    off = load_manifest(str(tmp_path))
    assert off.write_dir == ""
    assert off.sources == ("prose",)
    assert "# hand-written" in (tmp_path / "vault.yaml").read_text(encoding="utf-8")

    set_write_dir(tmp_path, "silica")
    assert load_manifest(str(tmp_path)).write_dir == "silica"
    assert (tmp_path / "vault.yaml").read_text(encoding="utf-8").count("write_dir") == 1


def test_toggle_declares_the_field_on_a_manifest_that_lacks_it(tmp_path):
    from silica.kernel.vault_manifest import set_write_dir

    (tmp_path / "vault.yaml").write_text("sources: [prose]\n", encoding="utf-8")
    set_write_dir(tmp_path, "silica")

    assert load_manifest(str(tmp_path)).write_dir == "silica"
    assert load_manifest(str(tmp_path)).sources == ("prose",)


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
    from silica.kernel.write.validate import validate_operations

    return validate_operations(ops, [], "")


def _op(op, **kw):
    # heading/source_basename are required on every Op, payload gate or not.
    return {"op": op, "heading": "Concetto", "source_basename": "src.md", **kw}


def _write(path, **kw):
    return _op("write", path=path, snippet="corpo della nota " * 10, **kw)


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


# --- safe mode: the mirror and its new-folder rule ---------------------------

@pytest.fixture
def mirror_vault(tmp_vault, monkeypatch):
    """A prose vault under safe mode: writes stage in `silica/`."""
    from pathlib import Path

    from silica.config import CONFIG

    monkeypatch.setenv("SILICA_MIN_WRITE_SNIPPET_CHARS", "10")
    root = Path(CONFIG.vault_path)
    (root / "Progetti").mkdir(exist_ok=True)
    (root / "vault.yaml").write_text("write_dir: silica\n", encoding="utf-8")
    reset_manifest_cache()
    return tmp_vault


def test_a_forgotten_prefix_still_lands_in_the_mirror(mirror_vault):
    # The safety net that makes the model's job "replicate the tree" and not
    # "remember a prefix": the rebase produces exactly the mirror path.
    validated, rejected = _validate([_write("Progetti/Foo.md")])

    assert rejected == []
    assert [o.path for o in validated] == ["silica/Progetti/Foo.md"]


def test_the_mirror_root_needs_no_reason(mirror_vault):
    # Its parent is the vault root, which exists by construction.
    validated, rejected = _validate([_write("silica/Foo.md")])

    assert rejected == []
    assert [o.path for o in validated] == ["silica/Foo.md"]


def test_an_invented_folder_without_a_reason_is_rejected(mirror_vault):
    validated, rejected = _validate([_write("silica/Inventata/Foo.md")])

    assert validated == []
    assert "'Inventata/' does not exist in the vault" in rejected[0].reason
    assert "reason" in rejected[0].reason  # the retry the steering loop needs


def test_an_invented_folder_with_a_reason_is_accepted(mirror_vault):
    validated, rejected = _validate(
        [_write("silica/Inventata/Foo.md", reason="no existing folder covers this")]
    )

    assert rejected == []
    assert [o.path for o in validated] == ["silica/Inventata/Foo.md"]


def test_a_folder_an_earlier_run_created_is_not_re_challenged(mirror_vault):
    # The mirror is the vault as it will be after the paste: a folder already
    # staged there was justified once, at creation. Asking again for every note
    # filed into it would make the gate per-note instead of per-folder.
    from pathlib import Path

    from silica.config import CONFIG

    (Path(CONFIG.vault_path) / "silica" / "Inventata").mkdir(parents=True)

    validated, rejected = _validate([_write("silica/Inventata/Seconda.md")])

    assert rejected == []
    assert [o.path for o in validated] == ["silica/Inventata/Seconda.md"]


def test_the_landing_folder_the_user_typed_owes_no_reason(mirror_vault):
    # target_dir is where the user said to file this run. Rejecting it would ask
    # the model to justify a decision it did not make. The injected hub follows
    # the boundary too — target_dir is rebased once, so everything derived from
    # it lands inside the mirror instead of beside it.
    from silica.kernel.write.validate import validate_operations

    validated, rejected = validate_operations(
        [_write("silica/Chimica/Foo.md")], [], "Chimica"
    )

    assert rejected == []
    assert sorted(o.path for o in validated) == [
        "silica/Chimica/Chimica.md",  # the hub
        "silica/Chimica/Foo.md",
    ]


def test_a_landing_folder_outside_the_boundary_carries_the_batch_inside(bounded_vault):
    # The defect the rebase closes, on the vault shape that had it before safe
    # mode: the op moves into docs/silica while target_dir names a folder
    # outside it, and every write is then "not in target folder".
    from silica.kernel.write.validate import validate_operations

    validated, rejected = validate_operations([_write("Concepts/Foo.md")], [], "Concepts")

    assert rejected == []
    assert "docs/silica/Concepts/Foo.md" in [o.path for o in validated]


def test_a_subtree_improvised_under_the_landing_folder_still_needs_one(mirror_vault):
    # The user chose Chimica, not Chimica/Organica — the exemption is an exact
    # match, so the deeper folder is still the model's own invention.
    from silica.kernel.write.validate import validate_operations

    validated, rejected = validate_operations(
        [_write("silica/Chimica/Organica/Foo.md")], [], "Chimica"
    )

    assert validated == []
    assert "'Chimica/Organica/' does not exist" in rejected[0].reason


def test_one_reason_covers_every_note_in_the_batch(mirror_vault):
    # A batch is one filing proposal. Per-op, this rejected three of four — and
    # which three depended on where the model put the sentence.
    ops = [_write(f"silica/Inventata/N{i}.md") for i in range(4)]
    ops[2]["reason"] = "no existing folder covers this material"

    validated, rejected = _validate(ops)

    assert rejected == []
    assert len(validated) == 4


def test_a_batch_with_no_reason_at_all_is_still_rejected(mirror_vault):
    ops = [_write(f"silica/Inventata/N{i}.md") for i in range(3)]

    validated, rejected = _validate(ops)

    assert validated == []
    assert len(rejected) == 3


def test_a_code_vault_never_asks_for_a_reason(bounded_vault):
    # docs/silica is Silica's own tree in a repo, not a mirror of the repo, so
    # "this folder does not exist at the root" says nothing there.
    validated, rejected = _validate([_write("docs/silica/Inventata/Foo.md")])

    assert rejected == []
    assert [o.path for o in validated] == ["docs/silica/Inventata/Foo.md"]


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
    from silica.kernel.link.autolink import backlink_pass

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
