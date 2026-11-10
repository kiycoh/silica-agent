"""The room's pure parts in pages.js, run under node as in test_graph_arrange: what the address Save
opens carries, and the line a link is written on. The glue around them is driven in a browser."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = (Path(__file__).resolve().parents[1] / "silica/ui/web/static/pages/pages.js").read_text(encoding="utf-8")
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
URL = "https://pages.test/@marta/memory/"
ROWS = [{"slug": "a", "area": 0}, {"slug": "b", "area": 0}, {"slug": "c", "area": 1}, {"slug": "d", "area": 1},
        {"slug": "e", "area": -1}]
PLAIN = "?save=https%3A%2F%2Fpages.test%2F%40marta%2Fmemory%2F"


def _run(tmp_path, body: str):
    pure = JS[JS.index("// pages-pure:start"):JS.index("// pages-pure:end")]
    script = tmp_path / "pure.js"
    script.write_text(pure + "\n" + body + "\n", encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _query(tmp_path, picks: list[str]) -> str:
    return _run(tmp_path, f"console.log(JSON.stringify(saveQuery({json.dumps(URL)}, {json.dumps(ROWS)}, new Set({json.dumps(picks)}))));")


def test_nothing_chosen_and_everything_chosen_are_the_plain_vault(tmp_path):
    assert _query(tmp_path, []) == PLAIN
    assert _query(tmp_path, ["a", "b", "c", "d", "e"]) == PLAIN


def test_whole_areas_travel_as_numbers_and_the_rest_by_name(tmp_path):
    assert _query(tmp_path, ["a", "b"]) == PLAIN + "&areas=0"
    assert _query(tmp_path, ["e"]) == PLAIN + "&areas=-1"
    assert _query(tmp_path, ["a", "b", "c"]) == PLAIN + "&areas=0&notes=c"


def test_notes_alone_name_an_area_nothing_is_in_for_a_silica_that_reads_only_areas(tmp_path):
    assert _query(tmp_path, ["a", "c"]) == PLAIN + "&areas=-2&notes=a,c"


def _line(tmp_path, text: str, slug: str) -> str:
    return _run(tmp_path, f"console.log(JSON.stringify(writtenLine({json.dumps(text)}, {json.dumps(slug)})));")


def test_a_links_line_is_read_without_its_markup(tmp_path):
    text = "---\ntitle: A\n---\n\nNothing here.\n- It follows **from** the [Curve](https://pages.test/@m/v/curve) of memory.\n"
    assert _line(tmp_path, text, "curve") == "It follows from the Curve of memory."


def test_a_page_built_without_a_host_links_by_file_and_no_other_slug_matches(tmp_path):
    text = "See [Curve](curve.html) and [Long](curve-long.html).\n"
    assert _line(tmp_path, text, "curve") == "See Curve and Long."
    assert _line(tmp_path, "See [Long](curve-long.html).\n", "curve") == ""


def test_a_long_line_is_cut_around_its_link(tmp_path):
    text = "x " * 200 + "[Curve](https://pages.test/@m/v/curve) " + "y " * 200
    line = _line(tmp_path, text, "curve")
    assert line.startswith("…") and line.endswith("…") and "Curve" in line and len(line) <= 232


def test_from_data_reads_a_file_by_its_last_segment(tmp_path):
    got = _run(tmp_path, 'console.log(JSON.stringify([fromData({"vault.json": 1}, "x/vault.json"),'
                         ' fromData({"vault.json": 1}, "x/notes.json") === undefined, fromData(undefined, "vault.json") === undefined]));')
    assert got == [1, True, True]
