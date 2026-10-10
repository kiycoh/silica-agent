"""The rerank server (ADR-0073): llama.cpp's loading contract, the protocol the
client already speaks, a bounded queue, and a race that exits clean."""
import socket
import sys
import threading
import types
from http.server import ThreadingHTTPServer

import httpx
import pytest

from silica.agent import rerank_serve as rs
from silica.agent.providers import Reranker


@pytest.fixture
def served(monkeypatch):
    """A real server on an ephemeral port with the model not loaded yet."""
    monkeypatch.setattr(rs._Served, "score", None)
    monkeypatch.setattr(rs._Served, "model", "m")
    monkeypatch.setattr(rs._Served, "lock", threading.Lock())
    monkeypatch.setattr(rs._Served, "wait", 0.05)
    server = ThreadingHTTPServer(("127.0.0.1", 0), rs._Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


def test_loading_answers_503_on_every_path(served):
    # serve.ready reads a 503 as "up but loading", llama.cpp's contract.
    assert httpx.get(f"{served}/models").status_code == 503
    assert httpx.post(f"{served}/rerank", json={"query": "q", "documents": ["d"]}).status_code == 503
    assert Reranker(base_url=served, model="m").scores("q", ["d"]) is None   # client abstains


def test_scores_reach_the_client_in_input_order(served, monkeypatch):
    monkeypatch.setattr(rs._Served, "score", lambda q, docs: [len(d) / 10 for d in docs])
    assert httpx.get(f"{served}/models").json()["data"][0]["id"] == "m"
    assert Reranker(base_url=served, model="ignored").scores("q", ["a", "bbb", "cc"]) == [0.1, 0.3, 0.2]


def test_bad_request_is_400_not_a_crash(served, monkeypatch):
    monkeypatch.setattr(rs._Served, "score", lambda q, docs: [0.0] * len(docs))
    assert httpx.post(f"{served}/rerank", json={"query": "q", "documents": [1]}).status_code == 400


def test_a_queued_request_is_shed_not_left_to_pile_up(served, monkeypatch):
    """A request still waiting for the scorer when its client has given up would
    run anyway; past `wait` it gets 503 and the client abstains."""
    monkeypatch.setattr(rs._Served, "score", lambda q, docs: [0.0] * len(docs))
    rs._Served.lock.acquire()
    try:
        r = httpx.post(f"{served}/rerank", json={"query": "q", "documents": ["d"]})
    finally:
        rs._Served.lock.release()
    assert r.status_code == 503 and r.json()["error"]["message"] == "busy"


def test_a_second_copy_exits_clean_when_the_port_is_taken(monkeypatch):
    """serve.ensure reads a failing exit as a dead server: the loser of a start
    race must exit 0 while the winner loads."""
    from silica.config import CONFIG

    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    monkeypatch.setattr(CONFIG, "rerank_base_url", f"http://127.0.0.1:{held.getsockname()[1]}/v1")
    monkeypatch.setattr(rs, "load_scorer", lambda model: pytest.fail("loaded weights on a taken port"))
    try:
        assert rs.main() == 0
    finally:
        held.close()


def test_refuses_a_non_loopback_url(monkeypatch):
    from silica.config import CONFIG

    monkeypatch.setattr(CONFIG, "rerank_base_url", "http://10.0.0.5:1235/v1")
    assert rs.main() == 2


def test_listwise_scores_come_back_in_input_order(monkeypatch):
    """jina-reranker-v3.x returns its results sorted by relevance through
    .rerank(): the scores must come back in the caller's document order, or the
    reorder would shuffle the wrong notes."""
    class _Encoder:
        def to(self, device):
            return self

        def eval(self):
            return self

        def rerank(self, query, documents):
            return [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}]

    torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False))
    auto = types.SimpleNamespace(from_pretrained=lambda *a, **k: _Encoder())
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", types.SimpleNamespace(AutoModel=auto))
    assert rs.load_scorer("/models/jina-reranker-v3.5")("q", ["a", "b"]) == [0.2, 0.9]


def test_cross_encoder_scores_query_document_pairs(monkeypatch):
    class _Cross:
        def __init__(self, model):
            pass

        def predict(self, pairs):
            assert pairs == [["q", "d0"], ["q", "d1"]]
            return [0.25, 0.75]

    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(CrossEncoder=_Cross))
    assert rs.load_scorer("m")("q", ["d0", "d1"]) == [0.25, 0.75]


def test_serve_command_runs_this_interpreter():
    assert rs.serve_command().split()[0].strip("'\"") == sys.executable
    assert rs.serve_command().endswith("-m silica.agent.rerank_serve")
