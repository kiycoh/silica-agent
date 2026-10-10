# tests/test_shape_load_shared.py
"""Two surfaces asking for /shape at once get the same answer.

loadShape() used to return null to a second caller while the first fetch was
in flight. Clicking Explore starts one (the scope picker's), so clicking a
surface inside that window drew nothing: the pane stayed empty until the next
redraw (found while verifying the Bridges morph, 2026-10-06). The function is
lifted out of app-explore.js and run under node with the DOM stubbed, so this
pins the behaviour and not the spelling.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

EXPLORE = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static" / "app-explore.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_a_second_caller_waits_for_the_first_fetch(tmp_path):
    src = EXPLORE.read_text(encoding="utf-8")
    fn = re.search(r"^function loadShape\(\) \{.*?^\}", src, re.M | re.S).group(0)
    script = tmp_path / "run.js"
    script.write_text(
        "let shapeData = null, shapeLoading = null, fetches = 0;\n"
        "const $ = () => ({ hidden: true });\n"
        "const notify = () => {};\n"
        "const fetch = async () => { fetches++; await new Promise(r => setTimeout(r, 20));"
        " return { json: async () => ({ areas: [1] }) }; };\n"
        + fn + "\n"
        "(async () => {\n"
        "  const [a, b] = await Promise.all([loadShape(), loadShape()]);\n"
        "  const c = await loadShape();\n"
        "  console.log(JSON.stringify({ fetches, a, b, same: a === c }));\n"
        "})();\n",
        encoding="utf-8",
    )
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == '{"fetches":1,"a":{"areas":[1]},"b":{"areas":[1]},"same":true}'
