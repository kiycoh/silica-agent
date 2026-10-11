"""POST /map/layout: the graph frame's lit set, arranged as the Map (ADR-0088)."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(tmp_vault):
    from silica.ui.web import server

    server._reset_session()
    tmp_vault.note("root.md", "# Root\n\nSee [[a]] and [[b]] and [[Missing note]].")
    tmp_vault.note("a.md", "# A\n\n[[b]]")
    tmp_vault.note("b.md", "# B")
    tmp_vault.note("c.md", "# C")
    return TestClient(server.app)


def test_every_lit_id_comes_back_placed_with_the_root_at_the_centre(client):
    ghost = "__unresolved__Missing note"
    resp = client.post("/map/layout", json={
        "root": "root.md", "ids": ["a.md", "b.md", "c.md", ghost],
        "groups": {"root.md": 0, "a.md": 1, "b.md": 1, "c.md": 2},
    })
    assert resp.status_code == 200
    out = resp.json()
    assert set(out["nodes"]) == {"root.md", "a.md", "b.md", "c.md", ghost}
    assert out["nodes"]["root.md"][:2] == [0, 0]
    # c is linked to nothing and the test vault has no vectors: it cannot be
    # priced, so it parks on the rim with the unresolved link.
    cost = {k: v[2] for k, v in out["nodes"].items()}
    assert cost["c.md"] > cost["a.md"] and cost[ghost] > cost["b.md"]


def test_a_wedge_is_the_area_the_frame_painted(client):
    resp = client.post("/map/layout", json={
        "root": "root.md", "ids": ["a.md", "b.md"], "groups": {"a.md": 4, "b.md": 4},
    })
    wedges = resp.json()["wedges"]
    assert [(w["area"], w["n"]) for w in wedges] == [(4, 2)]


def test_a_malformed_body_is_refused(client):
    assert client.post("/map/layout", json={"root": "root.md", "ids": "a.md"}).status_code == 400
    assert client.post("/map/layout", json={"ids": ["a.md"]}).status_code == 400


def test_another_origin_cannot_read_the_vault_through_it(client):
    resp = client.post("/map/layout", json={"root": "root.md", "ids": ["a.md"]},
                       headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403


def test_the_standalone_map_page_is_gone(client):
    """The arrangement replaced it (ADR-0088); the .canvas export stays."""
    assert client.get("/map", params={"note": "root.md"}).status_code == 404
