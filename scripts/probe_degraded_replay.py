# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0094 gate 2: replay real retrieval calls and count the degraded marks.

Run once by the owner, with the embedder and the reranker up:

    uv run python scripts/probe_degraded_replay.py --arm running
    uv run python scripts/probe_degraded_replay.py --arm stopped

Queries: the N most recent `silica_recall` and `silica_semantic_search` calls in
~/.silica/narration from GUI and TUI sessions (read only). Each is replayed with
its recorded query, k, memory and folder; notes= and vault= are dropped, since a
named-notes call retrieves nothing and a peek reads another vault's stores.

Setting: silica's home, and silica-core's copy of it, point at a temp directory
holding a copy of the vault's index directory, so the sweep and the fact-use
marks write to the copy. The .env and the vault are read in place.

- running: both endpoints must answer `serve.ready` first, or the run is void
  (exit 2). Expected 0 marked; any mark is a false alarm that blocks the merge
  until its cause is named.
- stopped: SILICA_EMBEDDING_BASE_URL=http://127.0.0.1:9/v1 in this process
  only, a port that refuses. Expected every call marked `embed`.

A false alarm is also counted the way the kill criterion counts it: a recall
result marked `embed` whose own context carries `embed:` evidence.
Exit 0 when the arm meets its expectation, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TOOLS_REPLAYED = ("silica_recall", "silica_semantic_search")
KEPT_ARGS = ("query", "k", "memory", "folder")


def recent_calls(narration: Path, n: int) -> list[tuple[float, str, dict, str]]:
    """(ts, tool, args, driver) of the n most recent replayable calls."""
    calls = []
    for path in narration.glob("*.jsonl"):
        driver = None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue  # a file rotated or removed mid-scan holds nothing to replay
        for line in lines:
            try:
                beat = json.loads(line)
            except ValueError:
                continue  # a torn last line from a live session
            payload = beat.get("payload") or {}
            if beat.get("kind") == "session":
                driver = payload.get("driver")
                continue
            if driver not in ("gui", "tui") or beat.get("kind") != "turn":
                continue
            for tc in (payload.get("message") or {}).get("tool_calls") or []:
                fn = tc.get("function") or {}
                if fn.get("name") not in TOOLS_REPLAYED:
                    continue
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    continue
                if not isinstance(args, dict) or not args.get("query"):
                    continue
                kept = {k: args[k] for k in KEPT_ARGS if k in args}
                calls.append((beat.get("ts") or 0.0, fn["name"], kept, driver))
    calls.sort(key=lambda c: c[0], reverse=True)
    return calls[:n]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--arm", choices=("running", "stopped"), required=True)
    ap.add_argument("--vault", default=str(Path.home() / "Documents" / "Obsidian" / "test"))
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--out", default="", help="write the per-call rows here as JSON")
    a = ap.parse_args()

    real_home = Path.home() / ".silica"
    calls = recent_calls(real_home / "narration", a.n)
    if len(calls) < a.n:
        print(f"only {len(calls)} replayable GUI/TUI calls in the narration", file=sys.stderr)

    # Before silica is imported: CONFIG reads the environment once.
    os.environ["SILICA_VAULT"] = a.vault
    if a.arm == "stopped":
        os.environ["SILICA_EMBEDDING_BASE_URL"] = "http://127.0.0.1:9/v1"

    import silica.kernel.recall.embed as embed_mod
    import silica.kernel.recall.paths as paths_mod
    import silica_core.kernel.recall.paths as core_paths_mod
    from silica.config import CONFIG
    from silica.kernel.recall.paths import vault_digest
    from silica.onboarding.serve import ready

    sandbox = Path(tempfile.mkdtemp(prefix="silica-0094-replay-"))
    digest = vault_digest(a.vault)
    src = real_home / "index" / digest
    if not src.is_dir():
        print(f"no index for {a.vault} under {src}", file=sys.stderr)
        return 2
    shutil.copytree(src, sandbox / "index" / digest)
    paths_mod._SILICA_HOME = sandbox
    core_paths_mod._SILICA_HOME = sandbox
    embed_mod._LEGACY_INDEX_PATH = sandbox / "index" / "embeddings.json"
    CONFIG.vault_path = a.vault

    if a.arm == "running":
        down = [u for u in (CONFIG.embedding_base_url, CONFIG.rerank_base_url) if not ready(u)]
        if down:
            print(f"VOID: not ready: {', '.join(down)}", file=sys.stderr)
            return 2

    import silica.tools.atomic  # noqa: F401  (registers the tools)
    import silica.tools.composed  # noqa: F401
    import silica.tools.graph  # noqa: F401
    from silica.tools import TOOLS

    rows = []
    for ts, name, args, driver in calls:
        reply = json.loads(TOOLS[name].run(**args))
        degraded = reply.get("degraded") or {}
        false_alarm = (name == "silica_recall" and "embed" in degraded
                       and "embed:" in str(reply.get("context") or ""))
        rows.append({"ts": ts, "tool": name, "driver": driver, "query": args["query"][:80],
                     "degraded": degraded, "hint": reply.get("hint", ""),
                     "false_alarm": false_alarm})
        print(f"{name:24} {json.dumps(degraded):34} {args['query'][:60]}")

    # /tmp is RAM on this machine: the copied index goes as soon as it is read.
    shutil.rmtree(sandbox, ignore_errors=True)
    marked = sum(bool(r["degraded"]) for r in rows)
    embed_marked = sum("embed" in r["degraded"] for r in rows)
    false_alarms = sum(r["false_alarm"] for r in rows)
    print(f"\narm={a.arm} calls={len(rows)} marked={marked} embed={embed_marked} "
          f"false_alarms={false_alarms}")
    if a.out:
        Path(a.out).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    if a.arm == "running":
        return 0 if marked == 0 else 1
    return 0 if embed_marked == len(rows) and false_alarms == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
