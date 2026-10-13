# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The cross-encoder as a local server every silica process shares (ADR-0073).

In-process, the reranker cost each process that reranked 1,420 MB of host RAM
(torch, the CUDA context, the kernel images its first forward pass loads), and
the GUI, the REPL, the MCP server and an eval each paid it. Served once, it is
paid once, and a client is the same `providers.Reranker` that already speaks
`POST <base>/rerank {model, query, documents} -> {results: [{index,
relevance_score}]}` to llama.cpp.

Run as `<python> -m silica.agent.rerank_serve` with the interpreter that has the
[rerank] extra, never as a `silica` subcommand: the `silica` on PATH may be
another install without torch, and `import silica.cli` costs 178 MB and 2.6 s
before the port could be bound. `SILICA_RERANK_SERVE_CMD` carries that line
and `onboarding.serve.ensure` starts it like any other endpoint.

Started twice by a race, the second copy finds the port taken and exits 0:
`serve.ensure` reads a clean exit as "keep waiting" and a failing one as a dead
server, so a failing exit would print a red error while the first copy loads.
"""
from __future__ import annotations

import errno
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import urlsplit

logger = logging.getLogger("silica.rerank_serve")

# Multilingual by design: a vault is whatever language its owner writes in
# (conventions.language), and bge-reranker-base is English/Chinese only.
# mxbai-rerank-base-v2 (Apache-2.0, Qwen2.5-0.5B backbone) over bge-v2-m3 on the
# link/orphan A/B (bench/local_rerank_query_ab.json, 609 masked wikilink pairs on
# a 795-note Italian vault): mrr +0.055 over the fused baseline at p=2.5e-09,
# where bge scored 125 wins / 100 losses at p=0.109, i.e. indistinguishable from
# not reranking at all. Costs ~390ms per 10-doc call against bge's ~140ms served.
# A HuggingFace repo id or a local directory, never a GGUF filename.
LOCAL_RERANK_MODEL = "mixedbread-ai/mxbai-rerank-base-v2"

# Listwise rerankers: one forward pass over the query and ALL documents, loaded
# through their own remote code (AutoModel + .rerank()), never as a CrossEncoder
# (which would bolt a random classification head onto them and score noise
# without raising). Matched by substring so a local clone's directory name counts.
# ponytail: a name list, not a capability probe; another listwise family needs
# its name added here.
_LISTWISE_RERANKERS = ("jina-reranker-v3",)

_LOOPBACK = {"localhost", "127.0.0.1"}

Scorer = Callable[[str, list[str]], list[float]]


def serve_command() -> str:
    """The shell line that starts this server with THIS interpreter, the one in
    which the [rerank] extra was found. Quoted for the platform's shell, since
    `serve.ensure` runs it with shell=True."""
    import shlex
    import subprocess
    import sys

    argv = [sys.executable, "-m", "silica.agent.rerank_serve"]
    return subprocess.list2cmdline(argv) if sys.platform == "win32" else shlex.join(argv)


def _is_listwise(model: str) -> bool:
    return any(name in model for name in _LISTWISE_RERANKERS)


def load_scorer(model: str) -> Scorer:
    """Load the weights; return score(query, documents) -> one float per document,
    in input order. First call downloads to the HF cache."""
    if _is_listwise(model):
        # trust_remote_code runs the model repo's modeling.py: acceptable only
        # because _LISTWISE_RERANKERS names the vendors it is allowed for.
        import torch
        from transformers import AutoModel

        loaded = AutoModel.from_pretrained(model, dtype="auto", trust_remote_code=True)
        encoder = loaded.to("cuda" if torch.cuda.is_available() else "cpu").eval()

        def listwise(query: str, documents: list[str]) -> list[float]:
            scored = [0.0] * len(documents)
            for r in encoder.rerank(query, documents):
                scored[r["index"]] = float(r["relevance_score"])
            return scored

        return listwise
    from sentence_transformers import CrossEncoder

    cross = CrossEncoder(model)
    return lambda query, documents: [float(s) for s in cross.predict([[query, d] for d in documents])]


class _Served:
    """What the handler threads share. `score` stays None until the weights and
    the first forward pass are done, and every path answers 503 meanwhile."""
    model = ""
    score: Scorer | None = None
    lock = threading.Lock()
    # Seconds a request waits for the scoring lock before 503. Half the client's
    # timeout: a request still queued when its client gives up would run anyway,
    # and on CPU (26 s a call, ADR-0054) the abandoned ones would pile up behind
    # the lock without bound. Set from SILICA_RERANK_TIMEOUT in main().
    wait = 2.5


class _Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _loading(self) -> bool:
        # llama.cpp's shape: serve.ready reads 503 as "up but still loading".
        if _Served.score is None:
            self._send(503, {"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}})
            return True
        return False

    def do_GET(self) -> None:
        if self._loading():
            return
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"object": "list", "data": [{"id": _Served.model, "object": "model"}]})
            return
        self._send(404, {"error": {"code": 404, "message": "not found"}})

    def do_POST(self) -> None:
        if self._loading():
            return
        if not self.path.rstrip("/").endswith("/rerank"):
            self._send(404, {"error": {"code": 404, "message": "not found"}})
            return
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            query, documents = req["query"], req["documents"]
            if not isinstance(query, str) or not isinstance(documents, list) or not all(
                isinstance(d, str) for d in documents
            ):
                raise ValueError("query must be a string and documents a list of strings")
        except (ValueError, KeyError, TypeError) as e:
            self._send(400, {"error": {"code": 400, "message": str(e)}})
            return
        # `model` in the request is ignored, as a single-model llama-server
        # ignores it: the weights are named once, by SILICA_LOCAL_RERANK_MODEL.
        if not _Served.lock.acquire(timeout=_Served.wait):
            self._send(503, {"error": {"code": 503, "message": "busy", "type": "unavailable_error"}})
            return
        score = _Served.score
        assert score is not None  # _loading() answered 503 while it was None, and nothing unsets it
        try:
            scores = score(query, documents) if documents else []
        except Exception as e:  # noqa: BLE001 - the client abstains on 500; the log keeps the cause
            logger.exception("scoring failed")
            self._send(500, {"error": {"code": 500, "message": str(e)}})
            return
        finally:
            _Served.lock.release()
        self._send(200, {
            "model": _Served.model,
            "results": [{"index": i, "relevance_score": s} for i, s in enumerate(scores)],
        })

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        logger.debug("%s %s", self.address_string(), format % args)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from silica.config import CONFIG

    url = urlsplit(CONFIG.rerank_base_url if "//" in CONFIG.rerank_base_url else f"//{CONFIG.rerank_base_url}")
    if url.hostname not in _LOOPBACK or not url.port:
        logger.error(
            "SILICA_RERANK_BASE_URL must name a loopback host and a port, e.g. "
            "http://127.0.0.1:1235/v1 (got %r)", CONFIG.rerank_base_url,
        )
        return 2
    _Served.model = CONFIG.local_rerank_model or LOCAL_RERANK_MODEL
    _Served.wait = CONFIG.rerank_timeout / 2
    try:
        server = ThreadingHTTPServer(("127.0.0.1", url.port), _Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            logger.info("port %d already taken: another copy serves it", url.port)
            return 0
        raise
    # Bound before torch is imported, so a racing second copy fails here in
    # milliseconds instead of loading a second set of weights into VRAM.
    serving = threading.Thread(target=server.serve_forever, name="rerank-http", daemon=True)
    serving.start()
    logger.info("listening on 127.0.0.1:%d, loading %s", url.port, _Served.model)
    score = load_scorer(_Served.model)
    # The first forward pass loads the CUDA kernel images (542 MB host, measured
    # 2026-10-07): paid here, not by the first query against a 5 s client timeout.
    score("warm up", ["warm up"])
    _Served.score = score
    logger.info("ready")
    serving.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
