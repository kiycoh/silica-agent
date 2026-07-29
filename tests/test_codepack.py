# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""kernel/codepack - deterministic context pack for one file (spec-code-recall)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from silica.kernel.code import codegraph, codepack

GAME_MODEL = """package game;

import java.util.List;
import util.Vec2;

public class GameModel extends Entity {
    private Vec2 pos;

    public void tick() {
        pos = new Vec2(1, 2);
    }
}
"""

ENTITY = """package game;

public class Entity {
    public void update() {
    }
}
"""

HUD = """package game;

public class Hud {
    public void draw() {
    }
}
"""

VEC2 = """package util;

public class Vec2 {
    public Vec2(int x, int y) {
    }

    public int len() {
        return 0;
    }
}
"""

LAUNCHER = """package app;

import game.GameModel;

public class Launcher {
    public static void main(String[] args) {
        GameModel m = new GameModel();
    }
}
"""

TARGET = "src/main/java/game/GameModel.java"


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A four-package Java repo, git-committed, with an isolated graph store."""
    from silica.kernel.recall import paths

    paths.clear_repo_root_cache()
    monkeypatch.setattr(codegraph, "store_path", lambda: tmp_path / "cg.json")
    _init_repo(tmp_path)
    _write(tmp_path, TARGET, GAME_MODEL)
    _write(tmp_path, "src/main/java/game/Entity.java", ENTITY)
    _write(tmp_path, "src/main/java/game/Hud.java", HUD)
    _write(tmp_path, "src/main/java/util/Vec2.java", VEC2)
    _write(tmp_path, "src/main/java/app/Launcher.java", LAUNCHER)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=tmp_path, check=True)
    yield tmp_path
    paths.clear_repo_root_cache()


def test_target_is_verbatim_when_it_fits(repo):
    pack = codepack.code_pack(repo, TARGET)
    assert pack["target_mode"] == "verbatim"
    assert pack["truncated"] is False
    assert GAME_MODEL.rstrip("\n") in pack["text"]
    assert pack["text"].startswith(f"## target {TARGET} @ {pack['head_ref']} mode: verbatim\n")
    assert pack["sections"]["target"] == [TARGET]
    assert pack["head_ref"]


def test_degrades_outside_a_git_repo(tmp_path, monkeypatch):
    from silica.kernel.recall import paths

    paths.clear_repo_root_cache()
    monkeypatch.setattr(codegraph, "store_path", lambda: tmp_path / "cg.json")
    _write(tmp_path, "lonely.py", "def f():\n    return 1\n")
    try:
        pack = codepack.code_pack(tmp_path, "lonely.py")
    finally:
        paths.clear_repo_root_cache()
    assert pack["target_mode"] == "verbatim"
    assert "def f():" in pack["text"]
    assert pack["head_ref"] == ""
    assert any("no code graph" in d for d in pack["dropped"])


def test_unreadable_target_raises(repo):
    with pytest.raises(ValueError):
        codepack.code_pack(repo, "src/main/java/game/Ghost.java")


def test_target_over_budget_falls_back_to_outline(repo):
    pack = codepack.code_pack(repo, TARGET, budget_chars=120)
    assert pack["target_mode"] == "outline"
    assert pack["truncated"] is True
    assert "public class GameModel extends Entity" in pack["text"]
    assert "public void tick()" in pack["text"]
    assert "pos = new Vec2(1, 2);" not in pack["text"]  # bodies are gone


def test_outline_is_served_even_when_it_busts_the_budget(repo):
    pack = codepack.code_pack(repo, TARGET, budget_chars=10)
    assert pack["target_mode"] == "outline"
    assert "public class GameModel extends Entity" in pack["text"]  # never less than the target


def test_no_symbols_means_no_outline_to_fall_back_to(repo, monkeypatch):
    _write(repo, "notes.txt", "x" * 500)
    pack = codepack.code_pack(repo, "notes.txt", budget_chars=50)
    assert pack["target_mode"] == "verbatim"  # an empty outline is worse than a long file
    assert "xxx" in pack["text"]


def test_outline_skip_bare_name_only_drops_the_addressed_symbol():
    # skip="Foo" must drop the top-level symbol named Foo and its own
    # members, but an unrelated method that merely shares the name Foo
    # (here, a member of a different class, Bar.Foo) must survive.
    entry = {
        "symbols": [
            {"kind": "class", "name": "Foo", "parent": "", "signature": "class Foo", "doc": ""},
            {"kind": "method", "name": "bar", "parent": "Foo", "signature": "void bar()", "doc": ""},
            {"kind": "class", "name": "Bar", "parent": "", "signature": "class Bar", "doc": ""},
            {"kind": "method", "name": "Foo", "parent": "Bar", "signature": "void Foo()", "doc": ""},
        ]
    }
    outline = codepack._outline(entry, skip="Foo")
    assert "class Foo" not in outline
    assert "void bar()" not in outline
    assert "class Bar" in outline
    assert "void Foo()" in outline  # unrelated Bar.Foo(), not a member of Foo


def test_selector_serves_one_method_verbatim_and_the_rest_as_outline(repo):
    pack = codepack.code_pack(repo, f"{TARGET}#GameModel.tick")
    assert pack["target_mode"] == "symbol"
    assert pack["truncated"] is True
    assert "pos = new Vec2(1, 2);" in pack["text"]          # the selected body, verbatim
    assert "-- rest of file, outline --" in pack["text"]
    assert "public class GameModel extends Entity" in pack["text"]
    assert pack["text"].count("public void tick()") == 1    # not repeated in the outline


def test_selector_on_a_class_serves_the_whole_class(repo):
    pack = codepack.code_pack(repo, "src/main/java/util/Vec2.java#Vec2")
    assert pack["target_mode"] == "symbol"
    assert "public int len()" in pack["text"]
    assert "return 0;" in pack["text"]


def test_unknown_selector_degrades_to_the_whole_file(repo):
    pack = codepack.code_pack(repo, f"{TARGET}#GameModel.nosuch")
    assert pack["target_mode"] == "verbatim"
    assert any("nosuch" in d for d in pack["dropped"])


def test_python_top_level_function_selector(repo):
    _write(repo, "tool.py", "import os\n\n\ndef alpha():\n    return os.sep\n\n\ndef beta():\n    return 2\n")
    pack = codepack.code_pack(repo, "tool.py#alpha")
    assert pack["target_mode"] == "symbol"
    assert "return os.sep" in pack["text"]
    assert "return 2" not in pack["text"]


def test_hierarchy_lists_declared_bases_and_repo_subtypes(repo):
    pack = codepack.code_pack(repo, TARGET)
    assert "GameModel extends Entity" in pack["text"]
    assert pack["sections"]["hierarchy"] == ["GameModel"]

    sub = codepack.code_pack(repo, "src/main/java/game/Entity.java")
    assert f"Entity <- {TARGET}#GameModel" in sub["text"]
    assert sub["sections"]["hierarchy"] == [f"{TARGET}#GameModel"]


def test_supertypes_across_families():
    assert codepack._supertypes("public class A extends B implements C, D<E>") == ["B", "C", "D"]
    assert codepack._supertypes("class A(B, C, metaclass=M)") == ["B", "C"]
    assert codepack._supertypes("class A : public B, private C") == ["B", "C"]
    assert codepack._supertypes("public class A") == []


def test_hierarchy_section_is_absent_when_empty(repo):
    pack = codepack.code_pack(repo, "src/main/java/game/Hud.java")
    assert "## hierarchy" not in pack["text"]
    assert "hierarchy" not in pack["sections"]


def test_selector_prefers_the_shallowest_match_over_a_nested_shadow():
    # A nested class named GameModel (inside Wrapper) shadows the real,
    # top-level GameModel in document order. The selector must resolve to
    # the shallowest (top-level) declaration, never the nested one.
    src = (
        "class Wrapper {\n"
        "    class GameModel {\n"
        "        void tick() { int decoy = 999; }\n"
        "    }\n"
        "}\n"
        "\n"
        "public class GameModel extends Entity {\n"
        "    public void tick() { int real = 1; }\n"
        "}\n"
    )
    picked = codepack._symbol_source(src, "java", "GameModel.tick")
    assert picked is not None
    assert "real = 1" in picked
    assert "decoy" not in picked
