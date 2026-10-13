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


def test_no_top_level_name_is_declared_by_two_page_scripts():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    scripts = [s for s in re.findall(r'<script src="/static/([\w.-]+\.js)"', html) if not s.endswith(".min.js")]
    assert "app-chat.js" in scripts and "app-notes.js" in scripts
    where: dict[str, list[str]] = defaultdict(list)
    for name in scripts:
        for m in _DECL.finditer((STATIC / name).read_text(encoding="utf-8")):
            where[m.group(1) or m.group(2)].append(name)
    clashes = {k: v for k, v in where.items() if len(set(v)) > 1}
    assert clashes == {}, f"declared in more than one script: {clashes}"
