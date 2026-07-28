import subprocess
from pathlib import Path

from silica.kernel.code import codedocs


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def _commit(path: Path, rel: str, text: str, msg: str) -> str:
    f = path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "--", rel], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg, "--", rel], cwd=path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True
    ).stdout.strip()


def _write_note(vault: Path, rel: str, documents: list[str], code_ref: str | None) -> None:
    fm_lines = ["---", "documents:"]
    for d in documents:
        fm_lines.append(f"  - {d}")
    if code_ref is not None:
        fm_lines.append(f"code_ref: {code_ref}")
    fm_lines += ["---", "", "doc body"]
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(fm_lines) + "\n", encoding="utf-8")


def test_stale_docs_flags_changed_file(tmp_path):
    _init_repo(tmp_path)
    ref0 = _commit(tmp_path, "src/m.py", "v1\n", "c1")
    vault = tmp_path / "docs"
    vault.mkdir()
    _write_note(vault, "m.md", ["src/m.py"], ref0)
    _commit(tmp_path, "src/m.py", "v2\n", "c2")  # code moved past ref0

    stale = codedocs.stale_docs(vault, repo_root=tmp_path)
    assert len(stale) == 1
    sd = stale[0]
    assert sd.note_path.endswith("m.md")
    assert sd.code_path == "src/m.py"
    assert sd.recorded_ref == ref0
    assert [c.subject for c in sd.intervening] == ["c2"]


def test_stale_docs_clean_when_ref_current(tmp_path):
    _init_repo(tmp_path)
    ref0 = _commit(tmp_path, "src/m.py", "v1\n", "c1")
    vault = tmp_path / "docs"
    vault.mkdir()
    _write_note(vault, "m.md", ["src/m.py"], ref0)
    assert codedocs.stale_docs(vault, repo_root=tmp_path) == []


def test_stale_docs_ignores_notes_without_documents(tmp_path):
    _init_repo(tmp_path)
    _commit(tmp_path, "src/m.py", "v1\n", "c1")
    vault = tmp_path / "docs"
    vault.mkdir()
    (vault / "plain.md").write_text("---\ntitle: x\n---\n\nhi\n", encoding="utf-8")
    assert codedocs.stale_docs(vault, repo_root=tmp_path) == []


def test_stale_docs_unknown_ref_not_stale(tmp_path):
    _init_repo(tmp_path)
    _commit(tmp_path, "src/m.py", "v1\n", "c1")
    vault = tmp_path / "docs"
    vault.mkdir()
    _write_note(vault, "m.md", ["src/m.py"], None)  # no code_ref
    assert codedocs.stale_docs(vault, repo_root=tmp_path) == []


def test_stale_count_zero_without_git(tmp_path):
    vault = tmp_path / "docs"
    vault.mkdir()
    (vault / "m.md").write_text("---\ndocuments:\n  - x.py\ncode_ref: abc\n---\n\nb\n", encoding="utf-8")
    assert codedocs.stale_count(vault) == 0  # not a repo → soft zero


def test_body_only_change_is_cosmetic(tmp_path):
    _init_repo(tmp_path)
    ref0 = _commit(tmp_path, "src/m.py", "def hi(n: str) -> str:\n    return n\n", "c1")
    vault = tmp_path / "docs"; vault.mkdir()
    _write_note(vault, "m.md", ["src/m.py"], ref0)
    _commit(tmp_path, "src/m.py", "def hi(n: str) -> str:\n    return n.upper()\n", "c2")
    stale = codedocs.stale_docs(vault, repo_root=tmp_path)
    assert len(stale) == 1
    assert stale[0].change_level == codedocs.CHANGE_COSMETIC
    assert stale[0].details == []


def test_signature_change_is_structural_with_detail(tmp_path):
    _init_repo(tmp_path)
    ref0 = _commit(tmp_path, "src/m.py", "def hi(n: str) -> str:\n    return n\n", "c1")
    vault = tmp_path / "docs"; vault.mkdir()
    _write_note(vault, "m.md", ["src/m.py"], ref0)
    _commit(tmp_path, "src/m.py", "def hi(n: str, loud: bool) -> str:\n    return n\n", "c2")
    stale = codedocs.stale_docs(vault, repo_root=tmp_path)
    assert stale[0].change_level == codedocs.CHANGE_STRUCTURAL
    assert any("signature changed: hi" in d for d in stale[0].details)


def test_unresolvable_ref_falls_back_conservative(tmp_path):
    _init_repo(tmp_path)
    _commit(tmp_path, "src/m.py", "v1\n", "c1")
    vault = tmp_path / "docs"; vault.mkdir()
    _write_note(vault, "m.md", ["src/m.py"], "f" * 40)  # unknown sha (hex→str, not YAML int 0)
    _commit(tmp_path, "src/m.py", "v2\n", "c2")
    stale = codedocs.stale_docs(vault, repo_root=tmp_path)
    assert stale[0].change_level == codedocs.CHANGE_STRUCTURAL
    assert any("no structural analysis" in d for d in stale[0].details)


def test_deleted_path_is_structural(tmp_path):
    import subprocess as sp
    _init_repo(tmp_path)
    ref0 = _commit(tmp_path, "src/m.py", "def hi(): ...\n", "c1")
    vault = tmp_path / "docs"; vault.mkdir()
    _write_note(vault, "m.md", ["src/m.py"], ref0)
    sp.run(["git", "rm", "-q", "src/m.py"], cwd=tmp_path, check=True)
    sp.run(["git", "commit", "-q", "-m", "rm"], cwd=tmp_path, check=True)
    stale = codedocs.stale_docs(vault, repo_root=tmp_path)
    assert stale[0].change_level == codedocs.CHANGE_STRUCTURAL
    assert any("deleted" in d for d in stale[0].details)


def test_note_verdict_aggregates_multi_path():
    from silica.kernel.code.codedocs import CHANGE_COSMETIC, CHANGE_STRUCTURAL, StaleDoc, note_verdict
    a = StaleDoc("n.md", "a.py", "r", "c", change_level=CHANGE_COSMETIC, details=[])
    b = StaleDoc("n.md", "b.py", "r", "c", change_level=CHANGE_STRUCTURAL, details=["b.py: + function f"])
    level, details = note_verdict([a, b])
    assert level == CHANGE_STRUCTURAL          # 1 STRUCTURAL of N → structural
    assert details == ["b.py: + function f"]
    assert note_verdict([a])[0] == CHANGE_COSMETIC


def test_java_change_classified_like_python(tmp_path):
    # confirmation, no new logic: classify_change is language-agnostic over
    # skeleton fields, so Java gets cosmetic/structural verdicts too
    _init_repo(tmp_path)
    v1 = "public class A {\n    public int go() { return 1; }\n}\n"
    ref0 = _commit(tmp_path, "src/A.java", v1, "c1")
    vault = tmp_path / "docs"; vault.mkdir()
    _write_note(vault, "a.md", ["src/A.java"], ref0)
    _commit(tmp_path, "src/A.java",
            "public class A {\n    public int go() { return 2; }\n}\n", "c2")
    stale = codedocs.stale_docs(vault, repo_root=tmp_path)
    assert stale[0].change_level == codedocs.CHANGE_COSMETIC   # body-only edit
    _commit(tmp_path, "src/A.java",
            "public class A {\n    public int go() { return 2; }\n"
            "    public void stop() {}\n}\n", "c3")
    stale = codedocs.stale_docs(vault, repo_root=tmp_path)
    assert stale[0].change_level == codedocs.CHANGE_STRUCTURAL
    assert any("+ method A.stop" in d for d in stale[0].details)


def test_notebook_staleness_classifies_like_code(tmp_path):
    import json as _json
    _init_repo(tmp_path)
    def nb(src):
        return _json.dumps({"nbformat": 4,
                            "metadata": {"kernelspec": {"language": "python"}},
                            "cells": [{"cell_type": "code", "source": src}]})
    ref0 = _commit(tmp_path, "a.ipynb", nb("def f(x):\n    return x\n"), "c1")
    vault = tmp_path / "docs"; vault.mkdir()
    _write_note(vault, "a.md", ["a.ipynb"], ref0)
    _commit(tmp_path, "a.ipynb", nb("def f(x):\n    return x + 1\n"), "c2")
    stale = codedocs.stale_docs(vault, repo_root=tmp_path)
    assert stale[0].change_level == codedocs.CHANGE_COSMETIC  # body-only cell edit
    _commit(tmp_path, "a.ipynb", nb("def f(x, y):\n    return x\n"), "c3")
    stale = codedocs.stale_docs(vault, repo_root=tmp_path)
    assert stale[0].change_level == codedocs.CHANGE_STRUCTURAL


def test_directory_binding_never_stale(tmp_path):
    # a `documents:` entry pointing at a package is a rationale binding, not a
    # behavior binding: it must not expire because some file under it changed
    # (classify_change has no language for a directory and would call it
    # STRUCTURAL forever). Holds today for free — `git log --name-only` emits
    # file paths, so a directory entry never resolves in latest_shas and falls
    # into the "no history → unknown, not stale" branch. This test pins that
    # emergent behavior as intended, so a latest_shas refactor cannot lose it.
    _init_repo(tmp_path)
    ref0 = _commit(tmp_path, "src/m.py", "v1\n", "c1")
    vault = tmp_path / "docs"; vault.mkdir()
    _write_note(vault, "pkg.md", ["src"], ref0)
    _write_note(vault, "file.md", ["src/m.py"], ref0)
    _commit(tmp_path, "src/m.py", "v2\n", "c2")

    stale = codedocs.stale_docs(vault, repo_root=tmp_path)
    assert [d.note_path for d in stale] == ["file.md"]   # the dir binding is gone


def test_head_stamped_ref_is_not_stale_until_the_path_moves(tmp_path):
    # `code_ref` records HEAD when the note was verified, not the bound path's
    # own newest sha. Comparing it against that sha called every note stale the
    # moment ANY other file was committed — a "stale" with zero intervening
    # commits. Staleness must follow the bound path, not the repo.
    _init_repo(tmp_path)
    _commit(tmp_path, "src/m.py", "v1\n", "c1")
    head = _commit(tmp_path, "src/other.py", "v1\n", "c2")   # unrelated commit
    vault = tmp_path / "docs"; vault.mkdir()
    _write_note(vault, "m.md", ["src/m.py"], head)           # stamped at HEAD

    assert codedocs.stale_docs(vault, repo_root=tmp_path) == []
    _commit(tmp_path, "src/m.py", "v2\n", "c3")              # now the path moved
    assert [d.code_path for d in codedocs.stale_docs(vault, repo_root=tmp_path)] == ["src/m.py"]
