"""The page's own scripts are classic scripts sharing one global scope, so a
top-level name declared in two of them is one name, and the file loaded last
wins without an error. Found 2026-10-13: the composer's new pickNote() was
silently replaced by app-notes.js's pickNote(path), loaded after it, and the @
list showed notes that Enter could not pick. No unit test saw it; the browser did."""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "silica" / "ui" / "web" / "static"

# A declaration at column 0: `function name(`, or `let|const|var|class name` followed
# by `=` or `{`. The `=`/`(` is what keeps GLSL out: shader sources sit in template
# strings at column 0 too, as `const float NAME = ...`.
_DECL = re.compile(r"^(?:async\s+)?(?:function\*?\s+([A-Za-z_$][\w$]*)\s*\(|"
                   r"(?:let|const|var|class)\s+([A-Za-z_$][\w$]*)\s*[={])", re.M)


def _scripts() -> list[str]:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return [s for s in re.findall(r'<script src="/static/([\w.-]+\.js)"', html) if not s.endswith(".min.js")]


def _declared() -> dict[str, list[str]]:
    where: dict[str, list[str]] = defaultdict(list)
    for name in _scripts():
        for m in _DECL.finditer((STATIC / name).read_text(encoding="utf-8")):
            where[m.group(1) or m.group(2)].append(name)
    return where


def test_no_top_level_name_is_declared_by_two_page_scripts():
    assert "app-chat.js" in _scripts() and "app-notes.js" in _scripts()
    clashes = {k: v for k, v in _declared().items() if len(set(v)) > 1}
    assert clashes == {}, f"declared in more than one script: {clashes}"


def test_a_typeof_guard_only_ever_points_at_a_later_script():
    """A guard on a name from an earlier script can never be false: that script
    has already run, so the guard reads as a dependency that may be missing and
    tests nothing. 18 of them were found 2026-11-06. One on a later script says
    the call may run while the page is still loading, which is its only use."""
    order = {name: i for i, name in enumerate(_scripts())}
    home = {k: v[0] for k, v in _declared().items()}
    wrong = []
    for name, at in order.items():
        for m in re.finditer(r"typeof\s+([A-Za-z_$][\w$]*)", (STATIC / name).read_text(encoding="utf-8")):
            target = home.get(m.group(1))
            if target and target != name and order[target] < at:
                wrong.append(f"{name}: typeof {m.group(1)} ({target} loads first)")
    assert wrong == []


def test_the_shared_helpers_load_first_and_lean_on_nothing():
    """app-core.js is what everything may depend on, which only holds while it
    depends on nothing: a name from a later script would be a load-order bug that
    waits for the first call. Comments and strings are out of the scan because
    they may say a script's name; what is left is code."""
    assert _scripts()[0] == "app-core.js"
    core = (STATIC / "app-core.js").read_text(encoding="utf-8")
    code = re.sub(r"//[^\n]*|`(?:[^`\\]|\\.)*`|\"(?:[^\"\\\n]|\\.)*\"", " ", core)
    words = set(re.findall(r"(?<![\w$.])[A-Za-z_$][\w$]*", code))
    others = {k for k, v in _declared().items() if "app-core.js" not in v}
    assert words & others == set()
