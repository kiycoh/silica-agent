# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The one table the server and the page share, and what `index()` writes over."""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from silica.ui.web import server, vocabulary
from tests.webassets import WEB


def _page_scripts() -> dict[str, str]:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    return {n: (WEB / n).read_text(encoding="utf-8") for n in re.findall(r'<script src="/static/([\w-]+\.js)"', html)
            if not n.endswith(".min.js")}


def _island(html: str) -> str:
    return re.search(r'<script type="application/json" id="vocab">(.*?)</script>', html, re.S)[1]


def test_the_page_carries_the_table_before_its_first_script(tmp_vault):
    html = TestClient(server.app).get("/").text
    assert json.loads(_island(html)) == json.loads(json.dumps(vocabulary.table()))
    assert html.index('id="vocab"') < html.index("const VOCAB =") < html.index("/static/app-core.js")


def test_a_word_in_the_table_cannot_end_the_element(monkeypatch):
    monkeypatch.setattr(vocabulary, "table", lambda: {"x": "</script><script>alert(1)</script>"})
    assert "<" not in vocabulary.island() and json.loads(vocabulary.island())["x"].startswith("</script>")


def test_every_word_a_script_reads_is_in_the_table():
    """A name read off VOCAB that the table does not hold is undefined, with no
    error, until a tooltip reads `undefined` in front of someone."""
    tree = json.loads(json.dumps(vocabulary.table()))
    missing = []
    for name, src in _page_scripts().items():
        for path in set(re.findall(r"\bVOCAB((?:\.\w+)+)", src)):
            node = tree
            for key in path.split(".")[1:]:
                if not isinstance(node, dict):
                    break  # a method of the word itself: `.includes`, `.replace`
                if key not in node:
                    missing.append(f"{name}: VOCAB{path}")
                    break
                node = node[key]
    assert not missing


@pytest.mark.parametrize("target", [server.ROOT_TAG, server.THEME_PREF, server.ASK_PLACEHOLDER, server.VOCAB_ISLAND])
def test_every_stamp_has_its_target_in_the_page(target):
    """index() writes over literals in index.html, and a replace that finds
    nothing says nothing: an attribute edited there would quietly unstamp it."""
    assert (WEB / "index.html").read_text(encoding="utf-8").count(target) == 1
