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


# What stays typed on both sides, on purpose. A branch key reads like a wire key,
# and a lookup in its place buys a longer line, so these are held by test: each
# literal a script compares must be one the Python side can write. The header and
# postMessage tables have a second reason: chat_routes.py and the graph frame, which
# stays self-contained for file:// (ADR-0026), cannot read the island.
def _py(*names: str) -> str:
    root = WEB.parents[2]
    return "".join((root / name).read_text(encoding="utf-8") for name in names)


def _js(*names: str) -> str:
    return "".join((WEB / name).read_text(encoding="utf-8") for name in names)


def test_every_job_state_a_script_compares_is_a_state_jobs_writes():
    from silica.ui.web import jobs

    read = set(re.findall(r'\b(?:j|o|before)\.state [!=]== "(\w+)"', _js("app-jobs.js", "app-tour.js")))
    written = set(re.findall(r'\bstate="(\w+)"|"state": "(\w+)"', _py("ui/web/jobs.py")))
    assert read and read <= set(jobs.STATES)
    assert {w for pair in written for w in pair if w} <= set(jobs.STATES)


def test_every_check_state_a_script_compares_is_one_the_check_writes():
    read = set(re.findall(r'\.state === "([^"]+)"', _js("app-notes.js", "app-editor.js")))
    read -= {"done", "recording", "busy"}  # a job's and the mic's, in the same scripts
    read -= {"vault"}  # the page's own: what a web check marks a block the vault backs
    # a vault file's state (ADR-0129, amended 2026-11-08) and the page vectors'
    # build, held to the modules that write them
    files = {"read", "unread", "unindexed"}
    assert all(f'"{s}"' in _py("sources/library.py") for s in files)
    assert 'state="failed"' in _py("ui/web/vectors.py")
    read -= files | {"failed"}
    assert read
    src = "".join(p.read_text(encoding="utf-8") for p in (WEB.parents[2] / "kernel/check").glob("*.py"))
    assert not {s for s in read if f'"{s}"' not in src}


def test_every_check_event_a_script_reads_is_one_the_stream_sends():
    """H-NDJSON: three consumers of one stream, and a renamed event is a branch
    that stops firing with no error."""
    read = set(re.findall(r'ev\.event === "(\w+)"', _js("app-notes.js")))
    src = _py("ui/web/notes_routes.py", "kernel/check/run.py")
    sent = set(re.findall(r'"event": "(\w+)"', src))
    assert read and read <= sent


def test_every_response_header_a_script_reads_is_one_something_sets():
    """H-HEADERS. The three a door in front of this process sets are silica-hosted's:
    bin/gateway's sign-out and bin/demo's two."""
    door = {"X-Silica-Signed-Out", "X-Silica-Demo-Left", "X-Silica-Demo-State"}
    read = set(re.findall(r'"(X-Silica-[\w-]+)"', "".join(_page_scripts().values())))
    root = WEB.parent
    set_here = set(re.findall(r'"(X-Silica-[\w-]+)"', "".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))))
    assert read and read - door <= set_here


def test_every_message_between_the_page_and_the_frame_has_both_ends():
    """H-POSTMSG: fifteen `silica-*` types across three scripts, and a type sent to
    nobody fails by doing nothing."""
    src = "".join(_page_scripts().values()) + _js("graph-frame.js")
    sent = set(re.findall(r'type:\s+"(silica-[\w-]+)"', src))
    heard = set(re.findall(r'\.type === "(silica-[\w-]+)"', src))
    assert sent and sent == heard


def test_every_call_python_writes_into_the_frames_html_is_a_function_of_the_frame():
    """H-INLINEJS: a JS-only search reports these as dead, and a rename breaks a
    checkbox without a word."""
    called = set(re.findall(r'on(?:change|click)="(\w+)\(', _py("tools/graph_view.py")))
    frame = _js("graph-frame.js")
    assert called and not {f for f in called if not re.search(rf"\bfunction {f}\(|\b{f} = ", frame)}


def test_the_rails_classes_are_ones_the_page_styles_or_reads():
    """H-TREE and H-FINDHTML: Python writes this DOM as strings."""
    from tests.webassets import app_css

    page = app_css() + "".join(_page_scripts().values())
    written = set(re.findall(r'class="((?:tree|find|fm)-[\w-]+)', _py("kernel/graph/file_tree.py", "ui/web/explore_routes.py", "ui/web/render.py")))
    assert {"tree-row", "tree-pin", "find-result", "fm-row"} <= written
    assert not {c for c in written if c not in page}
    # a tree row's address is the hash router's grammar
    assert 'href="#chat?note=' in _py("kernel/graph/file_tree.py") and 'params.get("note")' in page


def test_an_embed_in_a_streaming_answer_draws_what_the_page_can_draw():
    """mdLite's list is its own, the renderer's mirror, and pinned here because the
    stream cannot wait for a read of the table."""
    from silica.kernel.base.formats import SHOWN_IMAGE_EXTS

    m = re.search(r"const IMG = /\\\.\((.*?)\)\$/i", _js("app-chat.js"))
    said = {x for bit in m[1].split("|") for x in ([bit.replace("e?", ""), bit.replace("e?", "e")] if "e?" in bit else [bit])}
    assert {f".{x}" for x in said} == set(SHOWN_IMAGE_EXTS)


def test_every_command_the_composer_menu_offers_is_one_the_chat_runs(tmp_vault):
    """H-LANDIDEAS: the + menu names commands by hand. A skill is a command too."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    offered = set(re.findall(r'data-action="(/[\w-]+)"', html))
    names = {c["name"] for c in TestClient(server.app).get("/commands").json()}
    assert offered and offered <= names


def test_the_upload_cap_holds_the_longest_take_the_page_allows():
    """H-MIC: 16 kHz mono 16-bit PCM, what toWav16k sends."""
    from silica.ui.web import stt_routes

    assert stt_routes.MIC_EXPLAIN_MAX_MS / 1000 * 16000 * 2 <= stt_routes._STT_MAX_BYTES
    assert stt_routes.MIC_MAX_MS <= stt_routes.MIC_EXPLAIN_MAX_MS
