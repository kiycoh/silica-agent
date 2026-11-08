"""Wikis in the app (ADR-0157 point 2): the catalog and a card's cover graph, read by the app's
server from the public host, kept ten minutes, and trusted for their shape only."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

HOST = "https://wikis.silica-agent.dev"
ERROR = {"error": "Wikis could not be read."}


def _wiki(handle="marta", slug="memoria", **over):
    return {"handle": handle, "slug": slug, "title": "Memoria", "line": "How memory works",
            "published": "2026-11-06", "count": {"notes": 12, "areas": 2},
            "areas": [["Sleep", 5], ["Recall", 7]], "license": "CC BY 4.0",
            "url": f"/@{handle}/{slug}/", **over}


@pytest.fixture
def wikis(tmp_vault, monkeypatch):
    """(client, fetched urls, answers by url, clock). An answer that is an exception is raised."""
    from silica.sources import saved_page
    from silica.ui.web import public_routes, server

    for k in ("SILICA_HOSTED", "SILICA_VIEWS", "SILICA_PUBLIC_HOSTS", "SILICA_DEMO"):
        monkeypatch.delenv(k, raising=False)
    fetched: list[str] = []
    answers: dict = {}
    clock = [1000.0]

    def fetch(url):
        fetched.append(url)
        a = answers[url]
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(saved_page, "_fetch_json", fetch)
    monkeypatch.setattr(public_routes, "_now", lambda: clock[0])
    public_routes._WIKIS.clear()
    yield TestClient(server.app), fetched, answers, clock
    public_routes._WIKIS.clear()


def test_the_catalog_is_read_from_the_first_public_host(wikis):
    tc, fetched, answers, _ = wikis
    answers[HOST + "/index.json"] = [_wiki()]
    r = tc.get("/wikis/catalog")
    assert r.status_code == 200
    body = r.json()
    assert fetched == [HOST + "/index.json"]
    assert body["host"] == "wikis.silica-agent.dev" and body["read_at"].endswith("+00:00")
    assert body["wikis"] == [_wiki()]


def test_the_first_host_as_written_is_the_catalogs(wikis, monkeypatch):
    tc, fetched, answers, _ = wikis
    monkeypatch.setenv("SILICA_PUBLIC_HOSTS", " Pages.Example.test , wikis.silica-agent.dev")
    answers["https://pages.example.test/index.json"] = []
    assert tc.get("/wikis/catalog").json()["host"] == "pages.example.test"
    assert fetched == ["https://pages.example.test/index.json"]


def test_every_entry_is_trusted_for_its_shape_only(wikis):
    tc, _, answers, _ = wikis
    answers[HOST + "/index.json"] = [
        _wiki(handle="../etc"), _wiki(handle="Marta"), _wiki(slug=["memoria"]), "not an entry", None,
        _wiki(title="T" * 500, line="L" * 500, license="C" * 500, url="https://evil.test/",
              count={"notes": "12", "areas": True}, areas=[["Sleep", "5"], "x", ["A" * 300, 3]],
              published=20261106),
        _wiki(slug="other", title="\ud800 lone"),
    ]
    out = tc.get("/wikis/catalog").json()["wikis"]
    assert [w["slug"] for w in out] == ["memoria", "other"]
    w = out[0]
    assert (len(w["title"]), len(w["line"]), len(w["license"])) == (80, 160, 80)
    # the address is the one the handle and slug make, never the file's
    assert w["url"] == "/@marta/memoria/"
    assert w["count"] == {"notes": 0, "areas": 0} and w["published"] == ""
    assert w["areas"] == [["Sleep", 0], ["A" * 80, 3]]
    assert "\ud800" not in out[1]["title"]


def test_the_catalog_holds_at_most_500_wikis(wikis):
    tc, _, answers, _ = wikis
    answers[HOST + "/index.json"] = [_wiki(slug=f"w{i}") for i in range(600)]
    assert len(tc.get("/wikis/catalog").json()["wikis"]) == 500


@pytest.mark.parametrize("body", [{"wikis": []}, "garbage", None, 7])
def test_a_body_that_is_not_a_list_is_an_error(wikis, body):
    tc, _, answers, _ = wikis
    answers[HOST + "/index.json"] = body
    r = tc.get("/wikis/catalog")
    assert r.status_code == 200 and r.json() == ERROR


@pytest.mark.parametrize("exc", [OSError("connection refused"), ValueError("that wiki moved to a host"),
                                 RuntimeError("not JSON")])
def test_an_unreachable_host_is_an_error_and_is_asked_again(wikis, exc):
    tc, fetched, answers, _ = wikis
    answers[HOST + "/index.json"] = exc
    r = tc.get("/wikis/catalog")
    assert r.status_code == 200 and r.json() == ERROR
    answers[HOST + "/index.json"] = [_wiki()]
    assert tc.get("/wikis/catalog").json()["wikis"] == [_wiki()]
    assert len(fetched) == 2


def test_the_catalog_is_kept_ten_minutes(wikis):
    tc, fetched, answers, clock = wikis
    answers[HOST + "/index.json"] = [_wiki()]
    first = tc.get("/wikis/catalog").json()
    clock[0] += 599
    assert tc.get("/wikis/catalog").json() == first
    assert len(fetched) == 1
    clock[0] += 1
    answers[HOST + "/index.json"] = []
    assert tc.get("/wikis/catalog").json()["wikis"] == []
    assert len(fetched) == 2


def test_a_wikis_graph_is_its_positions_degrees_and_links(wikis):
    tc, fetched, answers, _ = wikis
    answers[HOST + "/@marta/memoria/vault.json"] = {
        "n": [[0.5, 0.25, 0, 3, "Sleep", "sleep"], [1.5, -2, 1, 1, "Recall", "recall"]],
        "e": [[0, 1]], "title": "Memoria"}
    r = tc.get("/wikis/graph", params={"handle": "marta", "slug": "memoria"})
    assert r.status_code == 200
    assert r.json() == {"n": [[0.5, 0.25, 3], [1, 0, 1]], "e": [[0, 1]]}
    assert fetched == [HOST + "/@marta/memoria/vault.json"]


def test_a_bad_node_is_dropped_and_the_links_still_join_the_same_notes(wikis):
    tc, _, answers, _ = wikis
    answers[HOST + "/@marta/memoria/vault.json"] = {
        "n": [[0.1, 0.1, 0, 2, "a", "a"], [float("nan"), 0.5, 0, 1, "b", "b"], "row",
              [0.2, 0.2, 0, -1, "c", "c"], [0.3, 0.3, 0, True, "d", "d"], [0.4, 0.4, 0, 1, "e", "e"], [0.5, 0.5, 0, 10**400, "f", "f"]],
        "e": [[0, 5], [0, 1], [1, 5], [5, 9], [0, True], [0, 5.0], [[0], 5], [0], "x", [5, 0]]}
    g = tc.get("/wikis/graph", params={"handle": "marta", "slug": "memoria"}).json()
    assert g == {"n": [[0.1, 0.1, 2], [0.4, 0.4, 1]], "e": [[0, 1], [1, 0]]}


def test_a_graph_holds_at_most_4000_notes_and_20000_links(wikis):
    tc, _, answers, _ = wikis
    answers[HOST + "/@marta/memoria/vault.json"] = {
        "n": [[0.5, 0.5, 0, 1, "t", f"s{i}"] for i in range(4100)],
        "e": [[i % 4100, (i + 1) % 4100] for i in range(30000)]}
    g = tc.get("/wikis/graph", params={"handle": "marta", "slug": "memoria"}).json()
    assert len(g["n"]) == 4000 and len(g["e"]) == 20000
    assert all(0 <= a < 4000 and 0 <= b < 4000 for a, b in g["e"])


@pytest.mark.parametrize("body", [[], "garbage", {"n": "x", "e": []}, {"n": []}, {"e": []}])
def test_a_graph_of_the_wrong_shape_is_an_error(wikis, body):
    tc, _, answers, _ = wikis
    answers[HOST + "/@marta/memoria/vault.json"] = body
    r = tc.get("/wikis/graph", params={"handle": "marta", "slug": "memoria"})
    assert r.status_code == 200 and set(r.json()) == {"error"}


def test_an_unreachable_graph_is_an_error_and_is_asked_again(wikis):
    tc, fetched, answers, _ = wikis
    url = HOST + "/@marta/memoria/vault.json"
    answers[url] = OSError("timed out")
    params = {"handle": "marta", "slug": "memoria"}
    assert set(tc.get("/wikis/graph", params=params).json()) == {"error"}
    answers[url] = {"n": [], "e": []}
    assert tc.get("/wikis/graph", params=params).json() == {"n": [], "e": []}
    assert tc.get("/wikis/graph", params=params).json() == {"n": [], "e": []}
    assert fetched == [url, url]


@pytest.mark.parametrize("params", [{"handle": "../x", "slug": "memoria"}, {"handle": "marta", "slug": ""},
                                    {"handle": "Marta", "slug": "memoria"}, {"slug": "memoria"},
                                    {"handle": "marta", "slug": "a/b"}])
def test_a_bad_handle_or_slug_is_refused_before_any_fetch(wikis, params):
    tc, fetched, _, _ = wikis
    assert tc.get("/wikis/graph", params=params).status_code == 400
    assert fetched == []


def test_hosted_without_the_wikis_view_has_no_catalog(wikis, monkeypatch):
    tc, fetched, _, _ = wikis
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "graph")
    assert tc.get("/wikis/catalog").status_code == 404
    assert tc.get("/wikis/graph", params={"handle": "marta", "slug": "memoria"}).status_code == 404
    assert fetched == []
