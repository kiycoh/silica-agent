"""Tests for export_graph() in silica/ui/web/graph_view.py.

The viewer moved out of the kernel and now vendors its JS bundle instead of
fetching it from a CDN. These lock the new behavior:

1. export_graph reads the vendored bundle and inlines it — the emitted HTML is
   self-contained (no CDN <script src=), i.e. it opens offline.
2. A missing vendored asset is a loud RuntimeError (packaging bug), never a
   silent fall back to the CDN <script src=.
3. Importing the viewer does NOT drag in FastAPI, so the core /graph command
   still works on a base install without the [gui] extra.
"""
from __future__ import annotations

import subprocess
import sys

import pytest


def test_export_graph_inlines_vendored_bundle_no_cdn(monkeypatch, tmp_path):
    import silica.kernel.graph_export as ge
    import silica.ui.web.graph_view as gv

    sentinel = "/*VENDORED_SENTINEL_12345*/"
    monkeypatch.setattr(gv, "_vendored_lib_js", lambda: sentinel)
    monkeypatch.setattr(ge, "build_graph_data", lambda folder="": (
        [{"id": "a", "label": "a", "type": "note", "group": -1,
          "color": {"background": "#4d5575"}, "path": "a", "size": 16}],
        [],
    ))
    monkeypatch.setattr(ge, "detect_communities", lambda nodes, edges: [])

    out = tmp_path / "g.html"
    res = gv.export_graph(output_path=str(out))

    html = out.read_text(encoding="utf-8")
    assert sentinel in html                 # vendored bundle inlined
    assert "<script src=" not in html       # no CDN fallback tag
    assert "cdn.jsdelivr" not in html       # genuinely offline
    assert res["success"] is True


def test_export_graph_raises_when_vendored_asset_missing(monkeypatch, tmp_path):
    """A missing vendored asset is loud — never a silent CDN fallback."""
    import silica.ui.web.graph_view as gv

    def boom() -> str:
        raise RuntimeError("graph_export: vendored 3d-force-graph.min.js is missing")

    monkeypatch.setattr(gv, "_vendored_lib_js", boom)
    with pytest.raises(RuntimeError, match="3d-force-graph"):
        gv.export_graph(output_path=str(tmp_path / "g.html"))


def test_graph_view_import_is_gui_free():
    """Importing the viewer must not require the optional [gui] extra (FastAPI).

    Runs in a clean interpreter so it is immune to other tests having already
    imported fastapi in the shared pytest process.
    """
    code = (
        "import silica.ui.web.graph_view, sys; "
        "assert 'fastapi' not in sys.modules, 'graph_view pulled in fastapi'; "
        "print('ok')"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
