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
