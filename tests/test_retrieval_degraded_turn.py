# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0094's fixture gate: a turn that ran without a leg says so, and only then.

Setting: a 30-note vault in tmp_path, its index built by the real build_index
through the real OpenAIEmbedder against an in-process fake endpoint (stdlib
ThreadingHTTPServer on 127.0.0.1, port 0): deterministic 64-d vectors from
hashed tokens, token-overlap rerank scores. "Stopped" means the test shuts its
own server down, so the port refuses and the SDK raises the real
APIConnectionError; a reranker at 127.0.0.1:9 refuses the same way. No arm
reaches :1234 or :1235, and a socket guard proves it.

Each arm runs 20 turns through the GUI's run_turn and the real run_agent, with
a scripted call_llm: 10 call silica_recall and 10 silica_semantic_search, and
half of each also call silica_search_context with a query that matches nothing
(the mix of 13 of 21 real turns, 2026-10-11). Four surfaces are read per turn:
the tool result, RecallWatch.degraded, the done frame's notes and
silica_turn_notes on the final assistant message.
"""
from __future__ import annotations

import dataclasses
import json
import re
import socket
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from silica.agent import turn_notes  # noqa: E402
from silica.agent.recall_watch import RecallWatch  # noqa: E402

TURNS = 20
WIDTH = 64
_DEGRADED_PREFIXES = ("lexical only:", "not reranked:")

_TOPICS = [
    ("Photosynthesis", "chlorophyll captures light energy and plants turn carbon dioxide into glucose"),
    ("Mitochondria", "the mitochondria produce atp through cellular respiration in the cell"),
    ("Plate tectonics", "continental plates drift and collide raising mountains and earthquakes"),
    ("Volcanoes", "magma rises through the crust and erupts as lava and volcanic ash"),
    ("Black holes", "gravity so strong that light cannot escape past the event horizon"),
    ("Neural networks", "layers of neurons learn weights by backpropagation and gradient descent"),
    ("Sourdough", "wild yeast starter ferments flour and water for a sour bread loaf"),
    ("Espresso", "hot water forced through finely ground coffee beans under pressure"),
    ("Roman roads", "legions built paved roads across the empire for trade and armies"),
    ("Printing press", "movable type let gutenberg print books and spread literacy"),
    ("Bayes theorem", "posterior probability equals likelihood times prior over evidence"),
    ("Fourier transform", "a signal decomposes into sine waves of different frequencies"),
    ("Git rebase", "rebase replays commits on a new base branch rewriting history"),
    ("TCP handshake", "syn synack ack opens a reliable connection between two hosts"),
    ("Hash tables", "keys hash to buckets for constant time lookup with collisions chained"),
    ("Binary search", "halve the sorted array each step to find the target in log time"),
    ("Climbing knots", "the figure eight follow through ties the rope to the harness"),
    ("Marathon training", "long slow runs build aerobic base before the race taper"),
    ("Chess openings", "the sicilian defence answers e4 with c5 for an unbalanced game"),
    ("Jazz harmony", "ii v i progressions and seventh chords colour the standards"),
    ("Watercolour", "pigment in water washes across paper with wet on wet blooms"),
    ("Beekeeping", "the hive keeps a queen workers and drones and stores honey in combs"),
    ("Composting", "microbes break down kitchen scraps and leaves into rich humus"),
    ("Tides", "the moon pulls the oceans creating high and low tides twice daily"),
    ("Vaccines", "an antigen trains the immune system to make antibodies before infection"),
    ("Inflation", "rising prices erode purchasing power when money supply outgrows goods"),
    ("Supply and demand", "price settles where the supply curve meets the demand curve"),
    ("Haiku", "three lines of five seven five syllables capture a seasonal moment"),
    ("Origami", "folding a single paper square into cranes without cuts or glue"),
    ("Sleep cycles", "rem and deep sleep alternate in ninety minute cycles each night"),
]
_QUERIES = [
    "how do plants make glucose from light", "where does the cell make atp",
    "why do earthquakes happen at plate boundaries", "what comes out when a volcano erupts",
    "can light escape a black hole", "how does backpropagation train a network",
    "how to feed a sourdough starter", "how is espresso brewed under pressure",
    "why did rome build roads", "what did gutenberg's press change",
    "posterior equals likelihood times prior", "decompose a signal into frequencies",
    "rewrite history with git rebase", "the three way tcp handshake",
    "constant time lookup with hashing", "search a sorted array in log time",
    "tie into the harness with a figure eight", "taper before the marathon race",
    "answer e4 with the sicilian", "seventh chords in jazz standards",
]
_NOTHING = "zzqx qwvj nonexistentterm"
ANSWER = "Here is what the vault says."


# --- the fake endpoint -----------------------------------------------------

def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def _vector(text: str, width: int) -> list[float]:
    v = [0.0] * width
    for t in _tokens(text):
        v[zlib.crc32(t.encode()) % width] += 1.0
    v[0] += 0.01  # never the zero vector: search skips a zero-norm query
    return v


class FakeEndpoint:
    """POST /v1/embeddings and POST /v1/rerank, deterministic; can sleep or
    answer half the rerank indices; stop() makes its port refuse."""

    def __init__(self) -> None:
        self.sleep = 0.0
        self.half = False
        self.width = WIDTH
        self.hits = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # the suite's output is not a request log
                pass

            def do_POST(self):
                outer.hits += 1
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if outer.sleep:
                    time.sleep(outer.sleep)
                if self.path.endswith("/embeddings"):
                    texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
                    reply = {"object": "list", "model": body.get("model", ""),
                             "usage": {"prompt_tokens": 0, "total_tokens": 0},
                             "data": [{"object": "embedding", "index": i,
                                       "embedding": _vector(t, outer.width)}
                                      for i, t in enumerate(texts)]}
                else:
                    q = set(_tokens(body["query"]))
                    docs = body["documents"]
                    keep = range(len(docs) // 2) if outer.half else range(len(docs))
                    reply = {"results": [{"index": i, "relevance_score":
                                          len(q & set(_tokens(docs[i]))) / (len(q) or 1)}
                                         for i in keep]}
                raw = json.dumps(reply).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client timed out first: the slow arms want exactly that

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def no_owner_servers(monkeypatch):
    """Fail any connection to the owner's model servers."""
    real = socket.create_connection
    tried: list[int] = []

    def guarded(address, *a, **k):
        if address[1] in (1234, 1235):
            tried.append(address[1])
            raise ConnectionRefusedError("test guard: the owner's servers are off limits")
        return real(address, *a, **k)

    monkeypatch.setattr(socket, "create_connection", guarded)
    yield tried
    assert tried == []


@pytest.fixture
def rig(tmp_vault, monkeypatch, no_owner_servers):
    """The vault, both fake endpoints, the index built through them."""
    import silica.driver
    import silica.kernel.recall.cooccurrence as cooc_mod
    import silica.kernel.recall.embed as embed_mod
    from silica.config import CONFIG
    from silica.tools.graph import silica_cooccurrence_refresh, silica_embed_refresh

    embed, rerank = FakeEndpoint(), FakeEndpoint()
    vault = CONFIG.vault_path
    for key, value in {
        "embedding_base_url": embed.url, "embedding_model": "fake-embed",
        "embedding_api_key": "x", "embedding_query_prefix": "", "embedding_document_prefix": "",
        "embedding_timeout": 60.0, "rerank_base_url": rerank.url, "rerank_model": "fake-rerank",
        "rerank_api_key": "", "rerank_timeout": 5.0, "memory_vault": vault,
    }.items():
        monkeypatch.setattr(CONFIG, key, value)
    # silica-core's own embedder reads the environment, which names the owner's
    # server; its vectors are never built here, but nothing may even try.
    from silica_core import config as core_config

    monkeypatch.setattr(core_config.CONFIG, "embedding_base_url", "")
    monkeypatch.setattr(silica.driver, "_driver", None)
    embed_mod.clear()
    cooc_mod.clear()
    for name, body in _TOPICS:
        tmp_vault.note(f"{name}.md", f"# {name}\n\n{body}.\n")
    silica_embed_refresh(force=True)
    silica_cooccurrence_refresh(force=True)
    assert len(embed_mod.get_store()) == 30
    yield SimpleNamespace(embed=embed, rerank=rerank, config=CONFIG, monkeypatch=monkeypatch)
    for srv in (embed, rerank):
        try:
            srv.stop()
        except OSError:
            pass  # already stopped by the arm


# --- the scripted model ------------------------------------------------------

def _resp(text: str = "", calls: list[tuple[str, dict]] | None = None):
    calls = calls or []
    ids = [f"t{time.monotonic_ns()}{i}" for i in range(len(calls))]
    msg: dict = {"role": "assistant", "content": text}
    if calls:
        msg["tool_calls"] = [{"id": i, "type": "function",
                              "function": {"name": n, "arguments": json.dumps(a)}}
                             for i, (n, a) in zip(ids, calls)]
    return SimpleNamespace(
        assistant_message=msg, text=text, reasoning=None, usage={},
        tool_calls=[SimpleNamespace(name=n, args=a, id=i) for i, (n, a) in zip(ids, calls)])


def scripted_llm(sent: list):
    """The turn's user text names the plan: `<tool>|<query>|<ctx>|<notes>`."""
    from silica.agent.providers import _to_wire

    def call(model, messages, tools=None, **kw):
        # What llm.call_llm hands the provider: the same transform, here.
        sent.append([_to_wire(m) for m in messages])
        last_user = max(i for i, m in enumerate(messages) if m.get("role") == "user")
        if tools is None or any(m.get("role") == "tool" for m in messages[last_user:]):
            return _resp(text=ANSWER)
        tool, query, ctx, named = str(messages[last_user]["content"]).split("|")
        args: dict = {"query": query, "k": 5, "memory": False}
        if named:
            args["notes"] = named.split(",")
        calls = [(tool, args)]
        if ctx:
            calls.append(("silica_search_context", {"query": _NOTHING}))
        return _resp(calls=calls)

    return call


def _plans(named: str = "") -> list[str]:
    plans = []
    for i, q in enumerate(_QUERIES[:TURNS]):
        tool = "silica_recall" if i < TURNS // 2 else "silica_semantic_search"
        ctx = "ctx" if i % 2 == 0 else ""
        if named:
            tool = "silica_recall"
        plans.append(f"{tool}|{q}|{ctx}|{named}")
    return plans


@pytest.fixture
def gui(rig, monkeypatch):
    """The GUI's run_turn over the real run_agent, each RecallWatch kept."""
    from silica.agent.loop import run_agent
    from silica.ui.web import server

    watches: list = []

    class Spy(RecallWatch):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.results: list[dict] = []
            watches.append(self)

        def _inspect(self, event):
            if event.name in ("silica_recall", "silica_semantic_search"):
                self.results.append(json.loads(event.result))
            super()._inspect(event)

    max_iter = {"n": None}

    def chat_turn(messages, model, cb, cancel_token=None, constraints=None):
        # silica_semantic_search is a loadable tool, not in the chat core.
        constraints = dataclasses.replace(
            constraints, tools=constraints.tools + ("silica_semantic_search",),
            **({"max_iterations": max_iter["n"]} if max_iter["n"] else {}))
        return run_agent(messages, model, cb, cancel_token=cancel_token, constraints=constraints)

    monkeypatch.setattr(server, "RecallWatch", Spy)
    monkeypatch.setattr(server, "run_agent", chat_turn)
    server._reset_session()
    sent: list = []
    yield SimpleNamespace(tc=TestClient(server.app), server=server, watches=watches,
                          sent=sent, llm=scripted_llm(sent), max_iter=max_iter)
    server._reset_session()


def _read_sse(response) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in response.text.splitlines()
            if line.startswith("data: ")]


def run_turns(gui, plans: list[str]) -> list[SimpleNamespace]:
    out = []
    with patch("silica.agent.loop.call_llm", gui.llm):
        for plan in plans:
            before = len(gui.watches)
            frames = _read_sse(gui.tc.post("/chat", json={"text": plan}))
            done = frames[-1]
            assert done["type"] == "done", done
            watch = gui.watches[before]
            out.append(SimpleNamespace(
                result=watch.results[0], watch=watch, done=done,
                stored=gui.server.messages[-1].get(turn_notes.KEY, [])))
    return out


def _degraded_notes(notes: list[dict]) -> list[str]:
    return [n["text"] for n in notes if n["text"].startswith(_DEGRADED_PREFIXES)]


def _tally(turns, leg: str, cause: str) -> dict[str, int]:
    """How many turns each surface marked with exactly this leg and cause."""
    want = {leg: cause}
    return {
        "result": sum(t.result.get("degraded") == want for t in turns),
        "watch": sum(t.watch.degraded == want for t in turns),
        "done": sum(len(_degraded_notes(t.done.get("notes", []))) == 1 for t in turns),
        "stored": sum(len(_degraded_notes(t.stored)) == 1 and t.stored == t.done["notes"]
                      for t in turns),
    }


def _unmarked(turns) -> dict[str, int]:
    return {
        "result": sum("degraded" in t.result for t in turns),
        "watch": sum(bool(t.watch.degraded) for t in turns),
        "done": sum(bool(_degraded_notes(t.done.get("notes", []))) for t in turns),
        "stored": sum(bool(_degraded_notes(t.stored)) for t in turns),
    }


ALL = {"result": TURNS, "watch": TURNS, "done": TURNS, "stored": TURNS}
NONE = {"result": 0, "watch": 0, "done": 0, "stored": 0}


def _reload_holds(gui, turns) -> None:
    """The reload arm over a marked arm: /messages, both compactions forced,
    then /session/load, each giving every turn the notes its frame carried."""
    from silica.agent import relevance
    from silica.agent.compaction import compact_read_history
    from silica.tools import TOOLS

    tc, server = gui.tc, gui.server
    live = [t.done["notes"] for t in turns]

    def replayed() -> list:
        return [m["notes"] for m in tc.get("/messages").json()
                if m["role"] == "assistant" and m["content"] == ANSWER]

    assert replayed() == live
    with patch.object(relevance, "ENABLED", True), \
            patch.object(server.CONFIG, "decision_model", "stub/model"), \
            patch.object(relevance, "decide", lambda state, q: {"needed": {"noul": 0.0}}):
        relevance.compact_by_relevance(server.messages, TOOLS)
    compact_read_history(server.messages, set(), prompt_tokens=10**7, budget=1,
                         floor_turns=1, tools=TOOLS)
    assert replayed() == live
    sid = server.current_session_id
    tc.post("/reset")
    assert tc.post("/session/load", json={"id": sid}).status_code == 200
    assert replayed() == live
    # Nothing the model is sent carries a note.
    assert not any(turn_notes.KEY in m for req in gui.sent for m in req)


# --- the arms ---------------------------------------------------------------

def test_both_endpoints_running_marks_nothing(gui):
    turns = run_turns(gui, _plans())
    assert _unmarked(turns) == NONE
    assert sum(t.result.get("reranked", True) is True for t in turns) == TURNS


def test_embedder_stopped(gui, rig):
    rig.embed.stop()
    turns = run_turns(gui, _plans())
    assert _tally(turns, "embed", "unreachable") == ALL
    assert sum(t.watch.thin is False for t in turns) == TURNS
    assert {_degraded_notes(t.done["notes"])[0] for t in turns} == {
        "lexical only: the embedder is unreachable · start it in Settings, Endpoints"}
    _reload_holds(gui, turns)


def test_embedder_slow(gui, rig):
    rig.monkeypatch.setattr(rig.config, "embedding_timeout", 0.2)
    rig.embed.sleep = 1.0
    turns = run_turns(gui, _plans())
    assert _tally(turns, "embed", "slow") == ALL
    assert all("did not answer in 0.2 s" in _degraded_notes(t.done["notes"])[0] for t in turns)
    _reload_holds(gui, turns)


def test_reranker_refused_at_port_9(gui, rig):
    rig.monkeypatch.setattr(rig.config, "rerank_base_url", "http://127.0.0.1:9/v1")
    turns = run_turns(gui, _plans())
    assert _tally(turns, "rerank", "unreachable") == ALL
    _reload_holds(gui, turns)


def test_reranker_slow(gui, rig):
    rig.monkeypatch.setattr(rig.config, "rerank_timeout", 0.2)
    rig.rerank.sleep = 1.0
    turns = run_turns(gui, _plans())
    assert _tally(turns, "rerank", "slow") == ALL
    _reload_holds(gui, turns)


def test_reranker_answering_half_the_indices(gui, rig):
    rig.rerank.half = True
    turns = run_turns(gui, _plans())
    assert _tally(turns, "rerank", "failed") == ALL
    # The fused order stands: nothing was reranked on padded zeros.
    assert sum(t.result.get("reranked", False) is False for t in turns
               if "results" in t.result) == TURNS // 2
    _reload_holds(gui, turns)


def test_reranker_url_empty_marks_nothing(gui, rig):
    rig.monkeypatch.setattr(rig.config, "rerank_base_url", "")
    turns = run_turns(gui, _plans())
    assert _unmarked(turns) == NONE
    assert rig.rerank.hits == 0


def test_gate_2b_marks_nothing(gui, rig):
    from silica.kernel.recall import rerank

    # Every candidate past the window: a factor of 0 makes any body too long
    # for the cross-encoder to read, the condition gate 2b abstains on.
    rig.monkeypatch.setattr(rerank, "_RERANK_WINDOW_FACTOR", 0)
    turns = run_turns(gui, _plans())
    assert _unmarked(turns) == NONE
    assert rig.rerank.hits == 0


def test_named_notes_mark_nothing_even_with_both_endpoints_down(gui, rig):
    """Nothing was retrieved, so no leg was missing; the old flag fired on all."""
    rig.embed.stop()
    rig.monkeypatch.setattr(rig.config, "rerank_base_url", "http://127.0.0.1:9/v1")
    turns = run_turns(gui, _plans(named="Photosynthesis,Tides"))
    assert _unmarked(turns) == NONE
    assert all(t.result["notes"] for t in turns)


def test_landing_turns_keep_their_notes(gui, rig):
    """max_iterations=1: the script calls a tool, so the closing answer runs."""
    rig.embed.stop()
    gui.max_iter["n"] = 1
    turns = run_turns(gui, _plans())
    assert _tally(turns, "embed", "unreachable") == ALL
    assert all(t.done["answer"] == ANSWER for t in turns)
    _reload_holds(gui, turns)


# --- direct calls: MCP, and the path /find takes ----------------------------

def _direct(rig) -> list[dict]:
    from silica.tools import TOOLS

    out = []
    for i, q in enumerate(_QUERIES[:TURNS]):
        name = "silica_recall" if i < TURNS // 2 else "silica_semantic_search"
        out.append(json.loads(TOOLS[name].run(query=q, k=5, memory=False)))
    return out


def test_direct_calls_running_mark_nothing(rig):
    assert sum("degraded" in r for r in _direct(rig)) == 0


def test_direct_calls_embedder_stopped_mark_all(rig):
    rig.embed.stop()
    replies = _direct(rig)
    assert sum(r.get("degraded") == {"embed": "unreachable"} for r in replies) == TURNS
    # Before the context and the results: the head a card or a stub keeps.
    assert all(list(r)[:3] == ["query", "degraded", "hint"] for r in replies)


# --- the doctor ----------------------------------------------------------------

def test_the_doctor_warns_on_an_index_of_another_width(rig):
    from silica.onboarding.checks import check_embeddings

    assert check_embeddings(rig.config).status == "ok"  # 64 and 64
    rig.embed.width = 32
    r = check_embeddings(rig.config)
    assert r.status == "warn"
    assert f"{WIDTH}-dim" in r.detail and "returns 32" in r.detail
