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
    assert codepack._supertypes("public class A implements C, D") == ["C", "D"]
    assert codepack._supertypes("public class A extends B") == ["B"]
    assert codepack._supertypes("public class Foo extends Base<T>") == ["Base"]


def test_supertypes_does_not_split_nested_generic_commas():
    # A multi-parameter generic base's inner commas are not base separators:
    # `HashMap<K, V>` is one base, not two.
    assert codepack._supertypes("public class Foo extends HashMap<K, V>") == ["HashMap"]
    assert codepack._supertypes("class A extends B<C, D> implements E") == ["B", "E"]
    assert codepack._supertypes("class A(B[C, D])") == ["B"]
    assert codepack._supertypes("struct A : public B<C, D>") == ["B"]


def test_supertypes_uses_the_last_dotted_segment():
    # A qualified base name is a dependency on the class it names, not on its
    # package/enclosing-type prefix.
    assert codepack._supertypes("public class Foo extends com.example.Base") == ["Base"]
    assert codepack._supertypes("public class A extends Map.Entry") == ["Entry"]


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


def test_neighborhood_has_imports_first_then_mentioned_package_siblings(repo):
    pack = codepack.code_pack(repo, TARGET)
    assert pack["sections"]["neighborhood"] == [
        "src/main/java/util/Vec2.java",     # resolved import, crosses a package
        "src/main/java/game/Entity.java",   # package sibling, no import needed
    ]
    assert "src/main/java/game/Hud.java" not in pack["sections"]["neighborhood"]
    assert "public int len()" in pack["text"]      # neighbour signatures are there
    assert "return 0;" not in pack["text"]         # neighbour bodies are not


def test_python_siblings_never_enter_even_when_mentioned(repo):
    # Minor A: pkg/b.py needs a real import too (pkg/c.py), so the
    # neighbourhood section actually exists and is non-empty. Without that,
    # `pack["sections"].get("neighborhood", [])` returns `[]` whether the
    # feature works or does not exist at all, and the assertion below cannot
    # discriminate between the two.
    _write(repo, "pkg/__init__.py", "")
    _write(repo, "pkg/a.py", "def alpha():\n    return 1\n")
    _write(repo, "pkg/c.py", "def gamma():\n    return 3\n")
    _write(repo, "pkg/b.py",
           "from pkg.c import gamma\n"
           "# alpha is named here but never imported\n"
           "def beta():\n    return gamma() + 2\n")
    pack = codepack.code_pack(repo, "pkg/b.py")
    assert pack["sections"]["neighborhood"] == ["pkg/c.py"]  # the real import enters
    assert "pkg/a.py" not in pack["sections"]["neighborhood"]


def test_private_members_stay_out_of_neighbour_signatures(repo):
    _write(repo, "src/main/java/game/Secret.java",
           "package game;\n\npublic class Secret {\n"
           "    private void hidden() {\n    }\n\n    public void shown() {\n    }\n}\n")
    _write(repo, TARGET, GAME_MODEL.replace("private Vec2 pos;", "private Vec2 pos;\n    private Secret s;"))
    pack = codepack.code_pack(repo, TARGET)
    assert "public void shown()" in pack["text"]
    assert "hidden" not in pack["text"]


def test_import_never_used_in_body_is_not_a_neighbor(repo):
    # Finding 1: an import line always names the file it imports, so the
    # mention filter must not count the import line itself as a mention —
    # otherwise every resolved import survives regardless of real use.
    src = GAME_MODEL.replace("import util.Vec2;\n", "import util.Vec2;\nimport app.Launcher;\n")
    _write(repo, TARGET, src)
    _write(repo, "src/main/java/app/Launcher.java", LAUNCHER)
    pack = codepack.code_pack(repo, TARGET)
    # Vec2 is used in the body (not just imported): still a neighbour.
    assert "src/main/java/util/Vec2.java" in pack["sections"]["neighborhood"]
    # Launcher is imported but never named anywhere outside the import line.
    assert "src/main/java/app/Launcher.java" not in pack["sections"]["neighborhood"]


def test_private_modifier_order_does_not_leak_the_member():
    # Finding 2: `private` is a legal Java modifier in any order relative to
    # `static`/`final`; a bare `sig.startswith("private ")` check misses it.
    entry = {
        "symbols": [
            {"kind": "class", "name": "Widget", "parent": "", "signature": "public class Widget", "doc": ""},
            {"kind": "method", "name": "shown", "parent": "Widget", "signature": "public void shown()", "doc": ""},
            {"kind": "method", "name": "reordered", "parent": "Widget",
             "signature": "static private void reordered()", "doc": ""},
        ]
    }
    sigs = codepack._signatures(entry)
    assert "public void shown()" in sigs
    assert "reordered" not in sigs


def test_private_inner_class_hides_its_own_public_members():
    # Minor C: a public method of a class that was itself filtered out (here,
    # a private inner class) must not be reparented onto the outer class —
    # it must be dropped along with its declaring class.
    entry = {
        "symbols": [
            {"kind": "class", "name": "Outer", "parent": "", "signature": "public class Outer", "doc": ""},
            {"kind": "class", "name": "Inner", "parent": "Outer", "signature": "private class Inner", "doc": ""},
            {"kind": "method", "name": "leaked", "parent": "Inner", "signature": "public void leaked()", "doc": ""},
            {"kind": "method", "name": "shown", "parent": "Outer", "signature": "public void shown()", "doc": ""},
        ]
    }
    sigs = codepack._signatures(entry)
    assert "public class Outer" in sigs
    assert "public void shown()" in sigs
    assert "leaked" not in sigs


def test_same_named_class_in_a_different_scope_is_not_poisoned():
    # Round-2 review finding: `hidden` is keyed by bare name (the only key
    # `parent` ever carries), so a filtered `Outer.Builder` must not also
    # hide the unrelated, public `Other.Builder`'s own members. Document
    # order (a class always precedes its own members) is what makes this
    # safe: `Builder` is re-opened the moment the second, surviving `Builder`
    # is itself emitted, before its own children are read.
    entry = {
        "symbols": [
            {"kind": "class", "name": "Outer", "parent": "", "signature": "public class Outer", "doc": ""},
            {"kind": "class", "name": "Builder", "parent": "Outer", "signature": "private class Builder", "doc": ""},
            {"kind": "method", "name": "step1", "parent": "Builder", "signature": "public void step1()", "doc": ""},
            {"kind": "method", "name": "topMethod", "parent": "Outer", "signature": "public void topMethod()", "doc": ""},
            {"kind": "class", "name": "Other", "parent": "", "signature": "public class Other", "doc": ""},
            {"kind": "class", "name": "Builder", "parent": "Other", "signature": "public class Builder", "doc": ""},
            {"kind": "method", "name": "step2", "parent": "Builder", "signature": "public void step2()", "doc": ""},
        ]
    }
    sigs = codepack._signatures(entry)
    assert "step1" not in sigs               # Outer.Builder is private: dropped with its child
    assert "public void step2()" in sigs     # Other.Builder is public: its child must survive


def test_external_and_importers_sections(repo):
    pack = codepack.code_pack(repo, TARGET)
    assert "## external\njava.util" in pack["text"]
    assert pack["sections"]["external"] == ["java.util"]
    assert "## importers (fan-in 1)\nsrc/main/java/app/Launcher.java" in pack["text"]
    assert pack["sections"]["importers"] == ["src/main/java/app/Launcher.java"]


def test_section_order_is_fixed(repo):
    text = codepack.code_pack(repo, TARGET)["text"]
    order = [i for i in (text.index("## target"), text.index("## hierarchy"),
                         text.index("## neighborhood"), text.index("## external"),
                         text.index("## importers"))]
    assert order == sorted(order)


def test_fill_stops_at_the_first_entry_that_does_not_fit(repo):
    full = codepack.code_pack(repo, TARGET)
    budget = len(full["text"]) - 30
    tight = codepack.code_pack(repo, TARGET, budget_chars=budget)

    assert tight["target_mode"] == "verbatim"   # the target still fits whole
    assert tight["truncated"] is False          # truncated is about the target, not the sections
    assert len(tight["text"]) <= budget
    assert tight["dropped"] == ["importers: src/main/java/app/Launcher.java"]
    assert "importers" not in tight["sections"]
    assert tight["sections"]["neighborhood"] == full["sections"]["neighborhood"]


def test_dropped_is_a_suffix_of_the_entry_order(repo):
    full = codepack.code_pack(repo, TARGET)
    order = [f"{sec}: {label}"
             for sec in ("hierarchy", "neighborhood", "external", "importers")
             for label in full["sections"].get(sec, [])]
    tight = codepack.code_pack(repo, TARGET, budget_chars=len(GAME_MODEL) + 60)
    assert tight["dropped"] == order[len(order) - len(tight["dropped"]):]
