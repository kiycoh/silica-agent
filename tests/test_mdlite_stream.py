# tests/test_mdlite_stream.py
"""mdLite() must terminate on every prefix of a streamed answer.

A table header arrives one delta before its delimiter row, and mdLite() re-parses
the whole segment on each delta. A half-arrived block used to consume no line and
spin the parser forever, until the tab threw RangeError and the SSE reader died
with it, truncating the answer.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static" / "app.js"

STREAMED = (
    "### Panoramica\n\n"
    "| Settimana | Date | Cosa studi |\n"
    "|:---------:|:----:|-----------|\n"
    "| **1** | 3/8 | Fondamenti |\n\n"
    "- una lista\n"
    "```\nfence\n```\n"
    "chiusura.\n"
)


def _md_lite_source() -> str:
    m = re.search(r"^function mdLite\(src\) \{.*?^\}", APP_JS.read_text(), re.S | re.M)
    assert m, "mdLite() not found in app.js"
    return m.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run app.js")
def test_every_streaming_prefix_terminates(tmp_path):
    script = tmp_path / "sweep.js"
    script.write_text(
        _md_lite_source()
        + "\nconst src = JSON.parse(process.argv[2]);\n"
        + "for (let n = 0; n <= src.length; n++) mdLite(src.slice(0, n));\n"
        + "console.log(mdLite(src).includes('<table>') ? 'TABLE' : 'NO-TABLE');\n"
    )
    out = subprocess.run(
        ["node", str(script), json.dumps(STREAMED)],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "TABLE"  # the finished table still renders as one
