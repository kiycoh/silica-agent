"""The ws index stays true after a delete and a create (ADR-0092 point 1).

The ws backend reads a referrer's body from disk, the way
session_changes.touched_from_disk does, so the repair costs no RPC and does
not depend on when Obsidian's metadata cache settles after the plugin's
delete. Against the stub plugin of test_ws_backend; the oracle is a fresh
index built from the stub after a full rebuild of its own.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("websockets")

from tests.test_ws_backend import StubPluginServer  # noqa: E402


def _write(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def _state(be) -> tuple:
    be._ensure_graph()
    return set(be._notes), set(be._graph.edges()), set(be._unresolved_links)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    from silica.config import CONFIG

    root = tmp_path / "v"
    _write(root, "A.md", "# A\n\nsee [[B]] and [[Gone]]\n")
    _write(root, "Sub/C.md", "# C\n\nsee [[B]]\n")
    _write(root, "B.md", "# B\n\nsee [[D]]\n")
    _write(root, "D.md", "# D\n")
    monkeypatch.setattr(CONFIG, "vault_path", str(root))
    return root


@pytest.fixture
def stub(vault):
    srv = StubPluginServer(vault)
    yield srv
    srv.stop()


@pytest.fixture
def ws(stub):
    from silica.driver.ws_backend import ObsidianWSBackend

    be = ObsidianWSBackend(url=stub.url, token=stub.token)
    be._ensure_graph()
    yield be
    be.close()


@pytest.fixture
def rebuilt(stub):
    from silica.driver.ws_backend import ObsidianWSBackend

    opened = []

    def _build() -> tuple:
        stub._fs._rebuild_index()
        be = ObsidianWSBackend(url=stub.url, token=stub.token)
        opened.append(be)
        return _state(be)

    yield _build
    for be in opened:
        be.close()


def test_a_ws_delete_puts_every_referrers_link_into_the_unresolved_set(ws, rebuilt):
    ws.delete("B.md")
    assert {("A.md", "B"), ("Sub/C.md", "B")} <= ws._unresolved_links
    assert _state(ws) == rebuilt()


def test_a_ws_create_resolves_the_links_waiting_for_its_name(ws, rebuilt):
    ws.create("Gone.md", "# Gone\n")
    assert ws._graph.has_edge("A.md", "Gone.md")
    assert ("A.md", "Gone") not in ws._unresolved_links
    assert _state(ws) == rebuilt()


def test_a_ws_restore_after_a_delete_brings_back_the_incoming_edges(ws, vault, rebuilt):
    body = (vault / "B.md").read_text(encoding="utf-8")
    ws.delete("B.md")
    ws.upsert("B.md", body)
    assert {"A.md", "Sub/C.md"} <= set(ws._graph.predecessors("B.md"))
    assert _state(ws) == rebuilt()


def test_a_referrer_the_disk_cannot_give_drops_the_index_for_a_rebuild(ws, vault, rebuilt, monkeypatch):
    from silica.config import CONFIG

    monkeypatch.setattr(CONFIG, "vault_path", str(vault / "elsewhere"))
    ws.delete("B.md")
    assert ws._is_graph_built is False  # the next read asks the plugin again
    assert _state(ws) == rebuilt()
