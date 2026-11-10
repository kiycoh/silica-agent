"""Every script the GUI loads parses. The scripts share one global scope and
none is bundled, so one syntax error stops its whole file in the browser and
no Python test notices: on 2026-11-08 a parameter named like a `const` below
it left the rail with no tree, found only by opening the app."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_every_static_script_parses():
    bad = {}
    for js in sorted(STATIC.glob("*.js")):
        r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True, timeout=60)
        if r.returncode:
            bad[js.name] = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "?"
    assert not bad, bad
