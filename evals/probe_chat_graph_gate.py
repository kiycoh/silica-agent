# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0092's deterministic gates, G1 and G2, and the cost numbers.

G1, correctness, on a scratch copy of the test vault (730 notes) and a scratch
HOME. 50 notes drawn with a fixed seed, 10 with no referrer, 30 with 1 to 4,
10 with 5 or more. For each:

  (a) silica_delete deletes exactly the notes with no referrer; a linked note
      is refused under every argument tried (none, confirm=true, an extra
      leave_unresolved list), and `linked_from` equals the referrers a full
      rebuild reports;
  (b) /delete (or the model's delete, when unlinked) lands; its dangling list
      equals rule 2 and its orphaned list rule 1 of the graph gate between full
      rebuilds before and after; the live index equals the rebuild;
  (c) /undo <path> restores the note, the live index equals a full rebuild,
      and the gate between before and after is clean.

Plus four sequences: an unlinked delete, then a create at its path (which
ADR-0093 refuses once merged; on a branch without it the create lands and is
undone first), then /undo; a move, then /undo; a create of a namesake, after
which the live fs index equals a rebuild; a linked delete refused, then
/delete, then /undo. `--ws` runs the cases that apply on the stub plugin of
tests/test_ws_backend.py; the namesake sequence and the move are fs-only (a ws
move drops the index for a rebuild from the plugin, and the ws resolver gives
a bare name to its first note).

G2, no regression on the recorded sessions: the writer calls of the 35 probe
sessions in ~/.silica/narration (read, never written) replayed without a model
on a fresh copy of the vault per session, through this tree and through
--base-ref (exported with `git archive`, no checkout). Every outcome must equal
the base replay except the refused delete of Autoencoders, and after each
session the live index must equal a full rebuild.

Nothing here writes outside --work: each child runs with HOME and SILICA_VAULT
pointed into it.

Usage:
  uv run python -m evals.probe_chat_graph_gate g1 [--ws] [--seed 92] [--work DIR] [--out FILE]
  uv run python -m evals.probe_chat_graph_gate g2 [--base-ref 1583ffd] [--work DIR] [--out FILE]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PRISTINE_VAULT = Path(os.environ.get("PRISTINE_VAULT", "~/Documents/Obsidian/test")).expanduser()
NARRATION = Path("~/.silica/narration").expanduser()
REPO = Path(__file__).resolve().parent.parent
SEED = 92
WRITERS = {
    "silica_write_note", "silica_patch_note", "silica_move", "silica_delete", "silica_flag_note",
    "silica_run_injector", "silica_autolink", "silica_backlink", "silica_curate",
    "silica_event_create", "silica_event_update", "silica_dedup", "silica_enrich_batch",
    "silica_anneal", "silica_bulk_write",
}
# A writer call that needs a model to run: replayed as skipped on both trees.
NEEDS_MODEL = {"silica_run_injector", "silica_curate", "silica_dedup", "silica_enrich_batch"}


# ------------------------------------------------------------- the vault ---

def copy_notes(dst: Path) -> None:
    """The pristine vault's notes and vault.yaml: 4.9 MB, where the whole
    folder with its PDFs and images is 138 MB of RAM under /tmp."""
    shutil.rmtree(dst, ignore_errors=True)
    for p in PRISTINE_VAULT.rglob("*.md"):
        rel = p.relative_to(PRISTINE_VAULT)
        if any(part.startswith(".") for part in rel.parts):
            continue
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst / rel)
    if (PRISTINE_VAULT / "vault.yaml").exists():
        shutil.copy2(PRISTINE_VAULT / "vault.yaml", dst / "vault.yaml")


def vault_hashes(vault: Path) -> dict[str, str]:
    return {str(p.relative_to(vault)): hashlib.sha1(p.read_bytes()).hexdigest()
            for p in vault.rglob("*.md")
            if not any(part.startswith(".") for part in p.relative_to(vault).parts)}


# ----------------------------------------------------- the oracle (child) ---

def rebuilt(vault: Path):
    from silica.driver.fs_backend import ObsidianFSBackend

    be = ObsidianFSBackend(vault_path=str(vault))
    be._ensure_index()
    return be


def state(be) -> tuple:
    return (frozenset(be._notes), frozenset(be._graph.edges()), frozenset(be._unresolved_links))


def state_diff(live: tuple, full: tuple) -> dict:
    names = ("notes", "edges", "unresolved")
    out = {}
    for name, a, b in zip(names, live, full):
        if a != b:
            out[name] = {"live_only": sorted(map(str, a - b))[:8], "rebuild_only": sorted(map(str, b - a))[:8]}
    return out


def rule1(pre, post) -> set[str]:
    """Unplanned orphans as graph_diff computes them, nothing created."""
    from silica.kernel.graph_diff import normalize_path, normalize_ref

    new = {normalize_ref(r) for r in post.orphans} - {normalize_ref(r) for r in pre.orphans}
    return new & {normalize_path(k) for k in pre.link_counts}


def rule2(pre, post) -> set[tuple[str, str]]:
    """New unresolved links as graph_diff computes them, nothing exempt."""
    from silica.kernel.graph_diff import normalize_link, normalize_path

    observed = {normalize_path(k) for k in pre.link_counts}
    new = ({normalize_link(lk.source, lk.target) for lk in post.unresolved}
           - {normalize_link(lk.source, lk.target) for lk in pre.unresolved})
    return {(s, t) for s, t in new if s in observed}


def gate_clean(pre, post) -> tuple[bool, list[str]]:
    from silica.kernel.graph_diff import check_graph_regression

    return check_graph_regression(pre, post, created_paths=[])


def same_note(original: str, now: str | None, path: str) -> bool:
    """The bytes back, or the bytes plus the `type` a create stamps into a
    frontmatter that lacked it: /undo of a delete recreates through upsert,
    which stamps (the accepted divergence named at ws_backend._overwrite_raw)."""
    if now is None:
        return False
    from silica.kernel.write.notetype import stamp_type

    return now == original or now == stamp_type(path, original)


# ------------------------------------------------------------- G1 (child) ---

class Live:
    """The process's driver, with its full rebuilds counted and its repair
    timed: a live index that quietly rebuilt itself would pass the oracle
    comparison by construction."""

    def __init__(self, kind: str, stub=None):
        from silica.driver import get_driver, set_driver

        self.kind = kind
        self.stub = stub
        self.rebuilds = 0
        self.timings: dict[str, list[float]] = {"delete": [], "create": []}
        if kind == "ws":
            from silica.driver.ws_backend import ObsidianWSBackend

            set_driver(ObsidianWSBackend(url=stub.url, token=stub.token))
        self.drv = get_driver()
        build = self.drv._rebuild_index if kind == "fs" else self.drv._ensure_graph

        def counted(*a, **kw):
            if kind == "fs" or not self.drv._is_graph_built:
                self.rebuilds += 1
            return build(*a, **kw)

        if os.environ.get("GATE0092_SABOTAGE") and kind == "fs":
            # The kill test: the index repair off, as before ADR-0092. A gate
            # that still passes like this measures nothing.
            drv = self.drv
            drv._patch_delete = lambda rel: drv._patch_index(rel, None)
            drv._patch_create = lambda rel, content: drv._patch_index(rel, content)
        if kind == "fs":
            self.drv._rebuild_index = counted
            self.drv._ensure_index()
        else:
            self.drv._ensure_graph = counted
            self.drv._ensure_graph()
        for key, name in (("delete", "_patch_delete"), ("create", "_patch_create")):
            self._time(key, name)

    def _time(self, key: str, name: str) -> None:
        inner = getattr(self.drv, name)

        def timed(*a, **kw):
            t0 = time.perf_counter()
            try:
                return inner(*a, **kw)
            finally:
                self.timings[key].append((time.perf_counter() - t0) * 1000)

        setattr(self.drv, name, timed)

    def state(self) -> tuple:
        return state(self.drv)

    def full(self) -> tuple:
        """(state, whole-vault snapshot) of a full rebuild from the files, or,
        on ws, of the plugin's index after the stub rebuilt its own."""
        vault = Path(os.environ["SILICA_VAULT"])
        if self.kind == "fs":
            be = rebuilt(vault)
            return state(be), be.graph_snapshot(None)
        from silica.driver.ws_backend import ObsidianWSBackend

        self.stub._fs._rebuild_index()
        be = ObsidianWSBackend(url=self.stub.url, token=self.stub.token)
        try:
            be._ensure_graph()
            return state(be), be.graph_snapshot(None)
        finally:
            be.close()


def _run_tool(tool: str, /, **args) -> dict:
    """`tool` positional-only: silica_patch_note's own argument is `name`."""
    from silica.tools import TOOLS

    out = TOOLS[tool].run(**args)
    try:
        return json.loads(out)
    except ValueError:
        return {"raw": out}


def _slash(line: str) -> str:
    """Run a direct command; return what it printed."""
    from rich.console import Console

    import silica.cli as cli

    console = Console(record=True, width=400)
    real, cli.CONSOLE = cli.CONSOLE, console
    try:
        cli._handle_direct_shortcut(line, [])
    finally:
        cli.CONSOLE = real
    return console.export_text()


def _slash_delete(path: str) -> tuple[str, dict]:
    """/delete <path>, with the report its helper returned."""
    import silica.tools.wrapped as wrapped

    seen: dict = {}
    real = wrapped.delete_with_restore_point

    def spy(*a, **kw):
        seen.update(real(*a, **kw))
        return dict(seen)

    wrapped.delete_with_restore_point = spy
    try:
        return _slash(f"/delete {path}"), seen
    finally:
        wrapped.delete_with_restore_point = real


def g1_case(live: Live, path: str, refs: list[str], vault: Path) -> dict:
    from silica.kernel.graph_diff import normalize_path

    original = (vault / path).read_text(encoding="utf-8")
    pre_state, pre = live.full()
    out: dict = {"path": path, "referrers": len(refs)}

    # (a)
    if refs:
        tries = [{}, {"confirm": True}, {"leave_unresolved": list(refs)}]
        results = [_run_tool("silica_delete", ref=path, **extra) for extra in tries]
        out["a"] = all(r.get("error", "").startswith("not deleted:") and r.get("linked_from") == refs
                       for r in results) and (vault / path).exists()
        if not out["a"]:
            out["a_detail"] = results
        printed, report = _slash_delete(path)
    else:
        report = _run_tool("silica_delete", ref=path)
        out["a"] = bool(report.get("success")) and not (vault / path).exists()
        report.setdefault("dangling", [])
    # (b)
    post_state, post = live.full()
    landed = not (vault / path).exists()
    orphaned = {normalize_path(p) for p in report.get("orphaned") or []}
    dangling = {(normalize_path(d["note"]), normalize_path(d["link"])) for d in report.get("dangling") or []}
    want1, want2 = rule1(pre, post), rule2(pre, post)
    live_ok = live.state() == post_state
    out["b"] = landed and orphaned == want1 and dangling == want2 and live_ok
    out["orphaned"], out["dangling"] = len(orphaned), len(dangling)
    if not out["b"]:
        out["b_detail"] = {"landed": landed, "orphaned": sorted(orphaned), "rule1": sorted(want1),
                           "dangling": sorted(dangling), "rule2": sorted(want2),
                           "index": state_diff(live.state(), post_state), "report": report}
    # (c)
    _slash(f"/undo {path}")
    now = (vault / path).read_text(encoding="utf-8") if (vault / path).exists() else None
    after_state, after = live.full()
    clean, errors = gate_clean(pre, after)
    out["c"] = same_note(original, now, path) and live.state() == after_state and clean
    if not out["c"]:
        out["c_detail"] = {"restored": now is not None, "same": same_note(original, now, path),
                           "index": state_diff(live.state(), after_state), "gate": errors[:5]}
    out["restored_stamped"] = now is not None and now != original
    _ = pre_state
    return out


def g1_sequences(live: Live, pick: dict[str, str], vault: Path) -> dict:
    seqs: dict = {}

    # S1: an unlinked delete, then a create at its path, then /undo.
    p = pick["unlinked"]
    original = (vault / p).read_text(encoding="utf-8")
    _, pre = live.full()
    r = _run_tool("silica_delete", ref=p)
    create = _run_tool("silica_write_note", path=p, body="A new body.\n")
    landed = bool(create.get("success"))
    if landed:  # no ADR-0093 on this tree: take the create back first
        _slash(f"/undo {p}")
    _slash(f"/undo {p}")
    now = (vault / p).read_text(encoding="utf-8") if (vault / p).exists() else None
    st, after = live.full()
    clean, errors = gate_clean(pre, after)
    seqs["S1_delete_create_undo"] = {
        "ok": bool(r.get("success")) and same_note(original, now, p) and live.state() == st and clean,
        "create_landed": landed, "create": create, "path": p, "gate": errors[:3],
        "rebuilds_so_far": live.rebuilds}

    # S2: a move, then /undo (fs only).
    if live.kind == "fs":
        p, dest = pick["linked_move"].split("\t")
        before = vault_hashes(vault)
        _, pre = live.full()
        # Into a folder that exists: a new one is a roster change the next
        # read answers with a full rebuild, which would hide what follows.
        r = _run_tool("silica_move", ref=p, to=dest)
        _slash(f"/undo {dest}/{Path(p).name}")
        st, after = live.full()
        clean, errors = gate_clean(pre, after)
        seqs["S2_move_undo"] = {"ok": bool(r.get("success")) and vault_hashes(vault) == before
                                and live.state() == st and clean,
                                "path": p, "gate": errors[:3], "rebuilds_so_far": live.rebuilds}

    # S3: a create of a namesake (fs only): the live index equals a rebuild.
    if live.kind == "fs" and pick.get("namesake"):
        p, folder = pick["namesake"].split("\t")
        new = f"{folder}/{Path(p).name}"
        _, pre = live.full()
        built_before = live.rebuilds
        r = _run_tool("silica_write_note", path=new, body="A namesake.\n")
        st, _post = live.full()
        took = sorted(s for s, t in live.drv._graph.edges() if t == new)
        index_after_create = state_diff(live.state(), st)
        ok_create = (bool(r.get("success")) and not index_after_create
                     and live.rebuilds == built_before)
        _slash(f"/undo {new}")
        st2, after = live.full()
        clean, errors = gate_clean(pre, after)
        seqs["S3_namesake"] = {"ok": ok_create and not (vault / new).exists()
                               and live.state() == st2 and clean,
                               "path": new, "create": r, "took_links_from": took,
                               "index_after_create": index_after_create,
                               "gate": errors[:3], "rebuilds_so_far": live.rebuilds}

    # S4: a linked delete refused, then /delete, then /undo.
    p = pick["linked_many"]
    original = (vault / p).read_text(encoding="utf-8")
    _, pre = live.full()
    refused = _run_tool("silica_delete", ref=p)
    _slash_delete(p)
    gone = not (vault / p).exists()
    _slash(f"/undo {p}")
    now = (vault / p).read_text(encoding="utf-8") if (vault / p).exists() else None
    st, after = live.full()
    clean, errors = gate_clean(pre, after)
    seqs["S4_refused_delete_undo"] = {
        "ok": refused.get("error", "").startswith("not deleted:") and gone
              and same_note(original, now, p) and live.state() == st and clean,
        "path": p, "gate": errors[:3], "rebuilds_so_far": live.rebuilds}
    return seqs


def g1_child(seed: int, ws: bool, limit: int = 50) -> dict:
    import silica.cli  # noqa: F401  registers every tool and /delete, /undo

    vault = Path(os.environ["SILICA_VAULT"])
    oracle = rebuilt(vault)
    refs_of = {p: sorted(set(oracle._graph.predecessors(p)) - {p}) for p in oracle._notes}
    zero = sorted(p for p, r in refs_of.items() if not r)
    mid = sorted(p for p, r in refs_of.items() if 1 <= len(r) <= 4)
    high = sorted(p for p, r in refs_of.items() if len(r) >= 5)
    rng = random.Random(seed)
    sample = rng.sample(zero, 10) + rng.sample(mid, 30) + rng.sample(high, 10)
    rest = [p for p in zero + mid + high if p not in sample]
    rng.shuffle(rest)
    # Outside the inbox: a create there is refused as staging, which would
    # stand in for ADR-0093's refusal without being it.
    pick = {"unlinked": next(p for p in rest if not refs_of[p] and not p.startswith("Inbox/")),
            "linked_move": next(p for p in rest if 1 <= len(refs_of[p]) <= 4),
            "folders": sorted({p.rsplit("/", 1)[0] for p in oracle._notes if "/" in p}),
            "linked_many": next(p for p in rest if len(refs_of[p]) >= 5)}
    src_folder = pick["linked_move"].rsplit("/", 1)[0]
    dest = next(f for f in pick.pop("folders") if f != src_folder
                and not (vault / f / Path(pick["linked_move"]).name).exists())
    pick["linked_move"] += f"\t{dest}"
    for p in rest:  # a referrer in another folder, where a namesake would win
        folder = p.rsplit("/", 1)[0] if "/" in p else ""
        other = [r.rsplit("/", 1)[0] for r in refs_of[p] if "/" in r and r.rsplit("/", 1)[0] != folder]
        if other and not (vault / other[0] / Path(p).name).exists():
            pick["namesake"] = f"{p}\t{other[0]}"
            break

    stub = None
    if ws:
        sys.path.insert(0, str(REPO))
        from tests.test_ws_backend import StubPluginServer

        stub = StubPluginServer(vault)
    live = Live("ws" if ws else "fs", stub)
    pristine = vault_hashes(vault)
    cases = []
    for path in sample[:limit]:
        try:
            case = g1_case(live, path, refs_of[path], vault)
        except Exception as exc:  # a crash is a failed case, reported, not a dead run
            case = {"path": path, "a": False, "b": False, "c": False, "crash": repr(exc)}
        case["vault_back"] = vault_hashes(vault) == pristine or _only_stamped(vault, pristine)
        cases.append(case)
    try:
        seqs = g1_sequences(live, pick, vault)
    except Exception as exc:
        seqs = {"crash": repr(exc)}
    if stub is not None:
        stub.stop()
    return {"kind": live.kind, "seed": seed, "cases": cases, "sequences": seqs,
            "rebuilds": live.rebuilds, "timings_ms": live.timings,
            "bins": {"zero": len(zero), "1-4": len(mid), "5+": len(high)}}


def _only_stamped(vault: Path, pristine: dict[str, str]) -> bool:
    """True when every note differing from the pristine copy differs by the
    stamp an /undo of a delete adds, and nothing appeared or disappeared."""
    from silica.kernel.write.notetype import stamp_type

    now = vault_hashes(vault)
    if set(now) != set(pristine):
        return False
    for rel, h in now.items():
        if h != pristine[rel]:
            body = (PRISTINE_VAULT / rel).read_text(encoding="utf-8")
            if hashlib.sha1(stamp_type(rel, body).encode("utf-8")).hexdigest() != h:
                return False
    return True


# ------------------------------------------------------------- G2 (child) ---

def probe_sessions() -> list[Path]:
    """The probe sessions whose narration holds a writer call, oldest first."""
    found = []
    for p in NARRATION.glob("*.jsonl"):
        recs = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        head = next((r for r in recs if r.get("kind") == "session"), None)
        if not head or (head.get("payload") or {}).get("driver") != "probe":
            continue
        if any(r.get("kind") == "tool" and r.get("status") == "running"
               and (r.get("payload") or {}).get("name") in WRITERS for r in recs):
            found.append((recs[0].get("ts") or 0, p))
    return [p for _, p in sorted(found)]


def g2_child(session: Path) -> dict:
    import silica.cli  # noqa: F401
    from silica.driver import get_driver

    vault = Path(os.environ["SILICA_VAULT"])
    drv = get_driver()
    drv._ensure_index()
    rebuilds = {"n": 0}
    inner = drv._rebuild_index

    def counted(*a, **kw):
        rebuilds["n"] += 1
        return inner(*a, **kw)

    drv._rebuild_index = counted
    create_ms: list[float] = []
    sweep_ms: list[float] = []
    if hasattr(drv, "_patch_create"):
        # The sweep alone is the create's total less the new note's own
        # patch, its first _patch_index call: that parse is what any create
        # paid before ADR-0092.
        real_create, real_patch = drv._patch_create, drv._patch_index
        own: list[float] = []

        def timed_patch(*a, **kw):
            t0 = time.perf_counter()
            try:
                return real_patch(*a, **kw)
            finally:
                own.append((time.perf_counter() - t0) * 1000)

        def timed(*a, **kw):
            own.clear()
            t0 = time.perf_counter()
            try:
                return real_create(*a, **kw)
            finally:
                total = (time.perf_counter() - t0) * 1000
                create_ms.append(total)
                sweep_ms.append(total - (own[0] if own else 0.0))

        drv._patch_index = timed_patch
        drv._patch_create = timed
    calls = []
    for line in session.read_text(encoding="utf-8").splitlines():
        r = json.loads(line) if line.strip() else {}
        if r.get("kind") == "tool" and r.get("status") == "running" and r["payload"].get("name") in WRITERS:
            calls.append((r["payload"]["name"], r["payload"].get("args") or {}))
    outcomes = []
    for name, args in calls:
        if name in NEEDS_MODEL:
            outcomes.append({"tool": name, "outcome": "skipped"})
            continue
        res = _run_tool(name, **args)
        err = res.get("error") if isinstance(res, dict) else None
        outcomes.append({"tool": name, "args": args,
                         "outcome": "error" if err else "ok", "error": (err or "")[:160]})
    live = state(drv)
    full = state(rebuilt(vault))
    return {"session": session.stem, "outcomes": outcomes, "index_true": live == full,
            "index_diff": state_diff(live, full), "rebuilds_after_start": rebuilds["n"],
            "create_ms": create_ms, "sweep_ms": sweep_ms}


# --------------------------------------------------------------- parents ---

def _child_env(work: Path, vault: Path, pythonpath: Path) -> dict:
    home = work / "home"
    (home / ".silica").mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SILICA_")}
    # PRISTINE_VAULT resolved here: under the child's HOME, "~" is the scratch.
    env.update({"HOME": str(home), "SILICA_VAULT": str(vault), "PYTHONPATH": str(pythonpath),
                "PRISTINE_VAULT": str(PRISTINE_VAULT),
                "HF_HOME": str(Path.home() / ".cache" / "huggingface")})
    return env


def _child(args: list[str], env: dict) -> dict:
    cmd = [sys.executable, str(Path(__file__).resolve()), "_child", *args]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"child {args} failed:\n{proc.stderr[-4000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _p95(xs: list[float]) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


def run_g1(work: Path, seed: int, ws: bool, limit: int = 50) -> dict:
    vault = work / "vault"
    copy_notes(vault)
    shutil.rmtree(work / "home", ignore_errors=True)
    res = _child(["g1", str(seed), "ws" if ws else "fs", str(limit)], _child_env(work, vault, REPO))
    cases = res["cases"]
    n = len(cases)
    summary = {k: sum(1 for c in cases if c.get(k)) for k in ("a", "b", "c", "vault_back")}
    deletes = res["timings_ms"]["delete"]
    creates = res["timings_ms"]["create"]
    res["summary"] = {
        "n": n, **{f"{k}_pass": v for k, v in summary.items()},
        "sequences": {k: v.get("ok") for k, v in res["sequences"].items() if isinstance(v, dict)},
        "delete_repair_ms": {"n": len(deletes), "p50": round(statistics.median(deletes), 2) if deletes else 0,
                             "p95": round(_p95(deletes), 2), "max": round(max(deletes, default=0), 2)},
        "create_repair_ms": {"n": len(creates), "p50": round(statistics.median(creates), 2) if creates else 0,
                             "p95": round(_p95(creates), 2), "max": round(max(creates, default=0), 2)},
        "live_rebuilds": res["rebuilds"],
    }
    return res


def run_g2(work: Path, base_ref: str) -> dict:
    base = work / "base"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(REPO), "archive", base_ref, "silica"],
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(base)], input=archive, check=True)
    sessions = probe_sessions()
    rows = []
    for s in sessions:
        row = {"session": s.stem}
        for tree, root in (("head", base), ("built", REPO)):
            vault = work / "vault"
            copy_notes(vault)
            shutil.rmtree(work / "home", ignore_errors=True)
            row[tree] = _child(["g2", str(s)], _child_env(work, vault, root))
        rows.append(row)
    diffs = []
    for row in rows:
        for i, (h, b) in enumerate(zip(row["head"]["outcomes"], row["built"]["outcomes"])):
            if h["outcome"] != b["outcome"]:
                diffs.append({"session": row["session"], "call": i, "tool": h["tool"],
                              "args": h.get("args"), "head": h, "built": b})
    creates = [ms for row in rows for ms in row["built"]["create_ms"]]
    sweeps = [ms for row in rows for ms in row["built"].get("sweep_ms", [])]
    return {"sessions": len(rows), "rows": rows, "outcome_diffs": diffs,
            "built_index_true": sum(1 for r in rows if r["built"]["index_true"]),
            "head_index_true": sum(1 for r in rows if r["head"]["index_true"]),
            "built_rebuilds": sum(r["built"]["rebuilds_after_start"] for r in rows),
            "calls": sum(len(r["built"]["outcomes"]) for r in rows),
            "create_repair_ms": {"n": len(creates), "p95": round(_p95(creates), 2),
                                 "max": round(max(creates, default=0), 2)},
            "create_sweep_ms": {"n": len(sweeps), "p95": round(_p95(sweeps), 2),
                                "max": round(max(sweeps, default=0), 2),
                                "each": [round(x, 2) for x in sweeps]}}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "_child":
        kind = argv[1]
        if kind == "g1":
            out = g1_child(int(argv[2]), argv[3] == "ws", int(argv[4]))
        else:
            out = g2_child(Path(argv[2]))
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 0
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("gate", choices=["g1", "g2"])
    ap.add_argument("--ws", action="store_true", help="G1 on the stub plugin instead of the fs backend")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--limit", type=int, default=50,
                    help="G1: run only the first N sampled notes (debugging; the gate is 50)")
    ap.add_argument("--base-ref", default="1583ffd", help="G2's base tree (git ref)")
    ap.add_argument("--work", default=None, help="scratch dir (default: a fresh temp dir)")
    ap.add_argument("--out", default=None, help="write the full JSON here")
    ap.add_argument("--sabotage", action="store_true",
                    help="G1 with the fs index repair switched off: the kill test, which must fail")
    args = ap.parse_args(argv)
    if args.sabotage:
        os.environ["GATE0092_SABOTAGE"] = "1"
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="gate0092-"))
    work.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    res = (run_g1(work, args.seed, args.ws, args.limit) if args.gate == "g1"
           else run_g2(work, args.base_ref))
    res["seconds"] = round(time.time() - t0, 1)
    if args.out:
        Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str),
                                  encoding="utf-8")
    shown = res.get("summary") if args.gate == "g1" else {k: v for k, v in res.items() if k != "rows"}
    print(json.dumps(shown, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
