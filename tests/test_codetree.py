"""codetree.why_for — the containment roll-up over `documents:` bindings."""
from __future__ import annotations

import subprocess

from silica.kernel import codetree


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def _commit(path, rel, text, msg):
    f = path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "--", rel], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg, "--", rel], cwd=path, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path,
                          capture_output=True, text=True).stdout.strip()


def _note(vault, rel, documents, code_ref=None, body="the rationale"):
    lines = ["---", "documents:"] + [f"  - {d}" for d in documents]
    if code_ref:
        lines.append(f"code_ref: {code_ref}")
    lines += ["---", "", body]
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _tree(tmp_path):
    """A repo with src/pkg/deep/m.py plus a vault under it."""
    _init_repo(tmp_path)
    _commit(tmp_path, "src/pkg/deep/m.py", "x = 1\n", "c1")
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


def test_ordering_across_the_three_relations(tmp_path):
    vault = _tree(tmp_path)
    _note(vault, "container.md", ["src"])                     # above the query
    _note(vault, "exact.md", ["src/pkg"])                     # the query itself
    _note(vault, "member.md", ["src/pkg/deep/m.py"])          # inside the query

    hits, residue = codetree.why_for(vault, "src/pkg", repo_root=tmp_path)
    assert residue == 0
    assert [(h.note_path, h.relation, h.distance) for h in hits] == [
        ("exact.md", "exact", 0),
        ("member.md", "member", 2),
        ("container.md", "container", 1),
    ]


def test_file_binding_rolls_up_to_a_directory_query(tmp_path):
    vault = _tree(tmp_path)
    _note(vault, "m.md", ["src/pkg/deep/m.py"], body="# Why m\n\nprose")
    hits, _ = codetree.why_for(vault, "src", repo_root=tmp_path)
    assert [(h.relation, h.bound_path) for h in hits] == [("member", "src/pkg/deep/m.py")]
    assert hits[0].hook == "# Why m"          # first non-empty body line


def test_empty_query_is_the_repo_root(tmp_path):
    vault = _tree(tmp_path)
    _note(vault, "m.md", ["src/pkg/deep/m.py"])
    hits, _ = codetree.why_for(vault, "", repo_root=tmp_path)
    assert [h.relation for h in hits] == ["member"]


def test_unrelated_binding_does_not_match(tmp_path):
    vault = _tree(tmp_path)
    _commit(tmp_path, "other/z.py", "z = 1\n", "c2")
    _note(vault, "z.md", ["other/z.py"])
    assert codetree.why_for(vault, "src/pkg", repo_root=tmp_path) == ([], 0)


def test_one_entry_per_note_keeps_the_strongest_relation(tmp_path):
    vault = _tree(tmp_path)
    _note(vault, "both.md", ["src", "src/pkg"])
    hits, _ = codetree.why_for(vault, "src/pkg", repo_root=tmp_path)
    assert len(hits) == 1
    assert (hits[0].relation, hits[0].bound_path) == ("exact", "src/pkg")


def test_cap_declares_its_residue(tmp_path):
    vault = _tree(tmp_path)
    for i in range(5):
        _note(vault, f"n{i}.md", ["src/pkg"])
    hits, residue = codetree.why_for(vault, "src/pkg", repo_root=tmp_path, cap=2)
    assert len(hits) == 2 and residue == 3


def test_stale_flag_on_file_bindings(tmp_path):
    vault = _tree(tmp_path)
    ref0 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                          capture_output=True, text=True).stdout.strip()
    ref1 = _commit(tmp_path, "src/pkg/deep/m.py", "x = 2\n", "c2")
    _note(vault, "stale.md", ["src/pkg/deep/m.py"], code_ref=ref0)   # code moved past it
    _note(vault, "fresh.md", ["src/pkg/deep/m.py"], code_ref=ref1)
    _note(vault, "unknown.md", ["src/pkg/deep/m.py"])                # no code_ref → not stale

    hits, _ = codetree.why_for(vault, "src/pkg/deep/m.py", repo_root=tmp_path)
    assert {h.note_path: h.stale for h in hits} == {
        "stale.md": True, "fresh.md": False, "unknown.md": False}


def test_directory_binding_is_never_stale(tmp_path):
    vault = _tree(tmp_path)
    ref0 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path,
                          capture_output=True, text=True).stdout.strip()
    _note(vault, "pkg.md", ["src/pkg"], code_ref=ref0)
    _commit(tmp_path, "src/pkg/deep/m.py", "x = 2\n", "c2")
    hits, _ = codetree.why_for(vault, "src/pkg", repo_root=tmp_path)
    assert [h.stale for h in hits] == [False]


def test_no_repo_returns_empty(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr("silica.kernel.paths.repo_root_for", lambda v: None)
    assert codetree.why_for(vault, "src") == ([], 0)


def test_no_git_degrades_to_not_stale(tmp_path):
    # a repo root with no git history at all: bindings still resolve, staleness
    # is simply unknown and must not be reported as stale
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("x = 1\n", encoding="utf-8")
    _note(vault, "m.md", ["src/m.py"], code_ref="deadbeef" * 5)
    hits, _ = codetree.why_for(vault, "src", repo_root=tmp_path)
    assert [(h.relation, h.stale) for h in hits] == [("member", False)]


def test_silica_code_why_tool_shape(tmp_path, monkeypatch):
    from silica.kernel import paths
    from silica.tools.codedocs_tool import silica_code_why

    vault = _tree(tmp_path)
    _note(vault, "m.md", ["src/pkg/deep/m.py"], body="# Why m")
    monkeypatch.setattr("silica.config.CONFIG.vault_path", str(vault))
    paths.clear_repo_root_cache()
    try:
        res = silica_code_why(path="src/pkg")
    finally:
        paths.clear_repo_root_cache()
    assert res["status"] == "ok" and res["residue"] == 0
    assert res["notes"][0]["note_path"] == "m.md"
    assert res["notes"][0]["hook"] == "# Why m"


def test_silica_code_why_degrades_soft_without_a_repo(monkeypatch):
    from silica.tools.codedocs_tool import silica_code_why

    monkeypatch.setattr("silica.config.CONFIG.vault_path", "")
    assert silica_code_why(path="src") == {
        "status": "ok", "path": "src", "notes": [], "residue": 0}
