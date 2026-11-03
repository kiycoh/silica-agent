# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The vault import's batches, from the pure half of app-notes.js run under
node: a folder goes up in requests capped by count and by bytes, so none of
them outlasts the edge's 125 s on a slow uplink (t18, 2026-11-02)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static" / "app-notes.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
MB = 1024 * 1024


def _sizes(tmp_path, sizes: list[int], max_files: int, max_bytes: int) -> list[list[int]]:
    src = JS.read_text(encoding="utf-8")
    pure = src[src.index("// import-batches:start"):src.index("// import-batches:end")]
    files = [{"name": f"f{i}", "size": s} for i, s in enumerate(sizes)]
    script = tmp_path / "b.js"
    script.write_text(pure + f"\nconsole.log(JSON.stringify(importBatches({json.dumps(files)}, "
                      f"{max_files}, {max_bytes}).map((b) => b.map((f) => f.size))));\n", encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_t18s_folder_no_longer_goes_up_as_one_request(tmp_path):
    # 32 files, 53.4 MB: the one batch that died at 125.0 s
    sizes = [53_418_541 // 32] * 32
    batches = _sizes(tmp_path, sizes, 32, 8 * MB)
    assert len(batches) == 7 and all(sum(b) <= 8 * MB for b in batches)
    assert [s for b in batches for s in b] == sizes  # every file, in order


def test_the_count_cap_still_holds_for_small_files(tmp_path):
    assert [len(b) for b in _sizes(tmp_path, [10] * 70, 32, 8 * MB)] == [32, 32, 6]


def test_a_file_over_the_byte_cap_goes_alone(tmp_path):
    assert _sizes(tmp_path, [1 * MB, 20 * MB, 1 * MB], 32, 8 * MB) == [[1 * MB], [20 * MB], [1 * MB]]


def test_nothing_to_send_is_no_request(tmp_path):
    assert _sizes(tmp_path, [], 32, 8 * MB) == []
