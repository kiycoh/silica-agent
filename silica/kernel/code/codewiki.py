# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""codewiki — deterministic partition + digest for the behavioral code wiki.

Zero LLM in this module. Reads the codegraph (structure, imports, call edges)
and produces one SubsystemDigest per subsystem: the grounding the capability
layer renders into prompts. Subsystems are directories, the human mental model
of the repo (spec: community-based partition rejected).
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import orjson

from silica.kernel.code.codeast import BARE_LANGUAGES, language_for
from silica.kernel.code.codegraph import CodeGraph

_EXCLUDED_TOP = {"tests", "test", "docs"}
_ROOT_KEY = "(root)"   # synthetic subsystem for loose files under the source root
# ponytail: single source root in v1; multi-package monorepo deferred, seam here


@dataclass(frozen=True)
class Subsystem:
    key: str
    path: str          # repo-relative dir of the subsystem
    members: list[str]  # repo-relative files, sorted


def _symbol_bearing(path: str) -> bool:
    """Only these count toward source-root density: bare files (toml/html/css)
    enter the graph but a site/ full of HTML must never win source-root."""
    lang = language_for(path)
    return (lang is not None and lang not in BARE_LANGUAGES) \
        or path.lower().endswith(".ipynb")


def source_root(graph: CodeGraph) -> str:
    """Top-level dir with the most symbol-bearing files; "" when loose files
    at the repo root outnumber every dir (the repo root is the source root)."""
    counts: Counter[str] = Counter()
    loose = 0
    for path in graph.files:
        if not _symbol_bearing(path):
            continue
        top, _, rest = path.partition("/")
        if rest:
            if top in _EXCLUDED_TOP:
                continue  # tests/docs never win source-root: they are not the code
            counts[top] += 1
        else:
            loose += 1
    # >= not >: a tie between loose files and the densest dir means the repo
    # root itself is the source root (flat repo, tests/docs then excluded).
    if not counts or loose >= max(counts.values()):
        return ""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def module_roots(graph: CodeGraph) -> list[str]:
    """Sibling modules of a multi-module build (Gradle/Maven core/, desktop/,
    android/), each its own source root. Empty when the repo has none.

    Without this `source_root` elects the densest module and every other one
    silently vanishes from the wiki — on a two-module game repo that is half
    the codebase.
    # ponytail: keyed on the src/ convention, the one that creates the problem;
    # probe pom.xml/build.gradle if a real repo lays its modules out otherwise
    """
    mods: set[str] = set()
    for path in graph.files:
        if not _symbol_bearing(path):
            continue
        top, _, rest = path.partition("/")
        if rest.startswith("src/") and top not in _EXCLUDED_TOP:
            mods.add(top)
    return sorted(mods) if len(mods) > 1 else []


def _corridor(rest: list[str]) -> str:
    """Prefix of information-free directories to skip, "" when there is none.

    A directory holding a single subdirectory of code and no code of its own
    says nothing about the design: Maven's src/main/java, Java's io/github/app.
    Descend it until a real fork. Only symbol-bearing files vote and tests/docs
    never do, so src/{main,test} still reads as a corridor and a stray
    src/main/resources/*.toml never forks it.
    """
    out = ""
    while True:
        dirs: set[str] = set()
        loose = False
        for r in rest:
            if not _symbol_bearing(r):
                continue
            head, sep, _ = r.partition("/")
            if not sep:
                loose = True
            elif head not in _EXCLUDED_TOP:
                dirs.add(head)
        if loose or len(dirs) != 1:
            return out
        only = dirs.pop()
        out += only + "/"
        rest = [r[len(only) + 1:] for r in rest if r.startswith(only + "/")]


def partition(graph: CodeGraph) -> list[Subsystem]:
    roots = module_roots(graph) or [source_root(graph)]
    out: list[Subsystem] = []
    for root_prefix in roots:
        under = {}
        for path in sorted(graph.files):
            if not root_prefix:
                under[path] = path
            elif path.startswith(root_prefix + "/"):
                under[path] = path[len(root_prefix) + 1:]
        skip = _corridor(sorted(under.values()))
        groups: dict[str, list[str]] = {}
        for path, rest in under.items():
            if skip:
                if not rest.startswith(skip):
                    continue          # the corridor's excluded siblings (src/test)
                rest = rest[len(skip):]
            head, _, tail = rest.partition("/")
            if not root_prefix and head in _EXCLUDED_TOP:
                continue
            # "(root)" cannot be a package name, so loose files under the source
            # root can never merge with a real directory (a repo with <root>/core/
            # would otherwise silently conflate the two under one key)
            groups.setdefault(head if tail else _ROOT_KEY, []).append(path)
        base = "/".join(p for p in (root_prefix, skip.rstrip("/")) if p)
        for key, members in sorted(groups.items()):
            sub_path = base if key == _ROOT_KEY else (f"{base}/{key}" if base else key)
            # one module's "manager" is not another's: qualify keys, or the two
            # would share a note path and overwrite each other
            if len(roots) > 1:
                key = root_prefix if key == _ROOT_KEY else f"{root_prefix}.{key}"
            out.append(Subsystem(key=key, path=sub_path, members=members))
    return sorted(out, key=lambda s: s.key)


def subsystem_for_path(graph: CodeGraph, rel: str,
                       taken: frozenset[str] = frozenset()) -> Subsystem | None:
    """Ad-hoc subsystem over an arbitrary repo directory, recursive.

    `partition` cuts one level under the source root, so a deep tree
    (Maven's core/src/main/java/io/github/app/manager) never surfaces as a
    key. /wiki <path> synthesizes the subsystem instead of refusing.
    None when no indexed source file lives under it."""
    prefix = rel.strip("/")
    members = sorted(p for p in graph.files if p.startswith(prefix + "/"))
    if not members:
        return None
    key = prefix.rsplit("/", 1)[-1]
    # the note path is subsystems/<key>.md: a leaf name shared with a real
    # subsystem would overwrite its note, so fall back to the full path
    return Subsystem(key=prefix.replace("/", ".") if key in taken else key,
                     path=prefix, members=members)


# ---------------------------------------------------------------------------
# SubsystemDigest — deterministic grounding, one per subsystem
# ---------------------------------------------------------------------------

_HUB_CAP = 5
_REG_DECORATORS = {"command", "route", "tool", "get", "post", "websocket"}
# ponytail: minimal decorator shortlist; extend when a real framework is missing
_INBOUND_SEED_CAP = 5


@dataclass(frozen=True)
class SubsystemDigest:
    key: str
    path: str
    members: list[str]
    struct_sig: str                                # sha256[:16] of members + call set
    public_symbols: dict[str, list[dict]]          # file -> symbol dicts
    module_docs: dict[str, str]
    module_comments: dict[str, list[str]]
    external_deps: list[str]
    collaborators_out: list[tuple[str, int, int]]  # (key, import_w, call_w)
    collaborators_in: list[tuple[str, int, int]]
    fan_in_hubs: list[tuple[str, int]]
    entry_points: list[tuple[str, str]]            # (path, heuristic label)
    flow_sketches: list[list[str]]                 # filled by flow layer
    parse_errors: int


def _file_to_key(subsystems: list[Subsystem]) -> dict[str, str]:
    return {m: s.key for s in subsystems for m in s.members}


def _public_symbols(entry: dict) -> list[dict]:
    """Every top-level symbol, with a docstring budget instead of a cut.

    Exported symbols carry the contract, private ones carry the mechanism, and
    a digest that keeps only the contract cannot explain how a module works
    (in this repo 42% of top-level defs are private). Private symbols come
    back marked `brief`: the renderer spends their signature and first
    docstring line, not the whole doc. Private *methods* still stay out —
    a class's internal helpers are noise at subsystem altitude.
    """
    allow = entry.get("dunder_all")
    out: list[dict] = []
    for s in entry.get("symbols", []):
        name = s.get("name", "")
        if s.get("parent"):
            if name.startswith("_"):
                continue
            if allow is not None and s["parent"] not in allow:
                continue
            out.append(s)
            continue
        exported = (name in allow) if allow is not None else not name.startswith("_")
        out.append(s if exported else {**s, "brief": True})
    for r in entry.get("reexports", []):
        out.append({"kind": "reexport", "name": r["name"], "parent": "",
                    "signature": f"{r['name']}  # re-exported from {r['from']}",
                    "doc": "", "doc_full": "", "decorators": []})
    return out


def _struct_sig(members: list[str], call_set: list[tuple[str, str, str]],
                entries: dict[str, dict]) -> str:
    """Signature over everything the digest grounds prose on: member set, call
    set, and each member's stored entry (signatures, docstrings, module docs
    and comments, imports). Doc-only edits must flip the sig — gate (a) sees
    only structural skeleton diffs, so gate (b) is the one gate that fires on
    the digest's prose payload."""
    payload = orjson.dumps(
        {"members": members, "calls": sorted(call_set), "entries": entries},
        option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(payload).hexdigest()[:16]


def _pyproject_script_files(root: Path, files: set[str]) -> set[str]:
    pp = root / "pyproject.toml"
    if not pp.is_file():
        return set()
    try:
        import tomllib
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
    except Exception:
        return set()
    out: set[str] = set()
    for target in (data.get("project", {}).get("scripts") or {}).values():
        stem = str(target).split(":", 1)[0].replace(".", "/")
        for cand in (f"{stem}.py", f"{stem}/__init__.py"):
            if cand in files:
                out.add(cand)
    return out


def _entry_points(graph: CodeGraph, sub: Subsystem,
                  scripts: set[str]) -> list[tuple[str, str]]:
    """Files execution can actually start at: a declared script, a main guard,
    a `python -m` target, a framework registration.

    No "caller-free" heuristic. Absence of an inbound call edge is evidence
    about the graph, not about the code: a lazily-imported module reads as
    caller-free, and a library subsystem is entered from outside by
    definition, so the label named every leaf an entry point. An empty list
    is the honest answer for a library layer; `_inbound_prefixes` is what
    carries the "how is this entered" signal there.
    """
    out: list[tuple[str, str]] = []
    for path in sub.members:
        entry = graph.files[path]
        labels: list[str] = []
        if path in scripts:
            labels.append("project.scripts")
        if entry.get("has_main_guard"):
            labels.append("__main__ guard")
        if path.endswith("__main__.py"):
            labels.append("python -m")
        if any(d.rsplit(".", 1)[-1] in _REG_DECORATORS
               for s in entry.get("symbols", []) for d in s.get("decorators", [])):
            labels.append("registration decorator")
        if labels:
            out.append((path, ", ".join(labels)))
    return out


def _inbound_prefixes(all_calls: list[tuple[str, str, str, str]],
                      member_set: set[str], key_of: dict[str, str]) -> list[list[str]]:
    """Chain prefixes [external caller, member] for the heaviest callers into
    the subsystem, one per caller. These make the flow section useful for a
    library layer, whose real flows all begin outside its own files.

    Only callers that belong to a subsystem qualify: `partition` keeps tests
    and docs out of the architecture, and they are the heaviest callers of a
    well-tested kernel — seeded from them the section would document the test
    suite instead of the product.
    """
    weight: Counter = Counter()
    for src, tgt, _callee, _caller in all_calls:
        if tgt in member_set and src not in member_set and key_of.get(src):
            weight[(src, tgt)] += 1
    out: list[list[str]] = []
    seen_src: set[str] = set()
    for (src, tgt), _w in weight.most_common():
        if src in seen_src:
            continue
        seen_src.add(src)
        out.append([src, tgt])
        if len(out) >= _INBOUND_SEED_CAP:
            break
    return out


_DOC_BUDGET_CHARS = 45_000   # per subsystem, spent on full docstrings by rank


def _spend_doc_budget(publics: dict[str, list[dict]], graph: CodeGraph,
                      privileged: set[str]) -> set[str]:
    """Full docstrings to the files that carry the subsystem, first lines to the
    tail; returns the paths that ran out of budget.

    A flat 86-file package is a real shape and no partition rule shrinks it:
    silica/kernel renders at ~68k tokens, more than most read paths will accept
    in one prompt, and half of that is docstring prose. Rank is fan-in, with
    entry points and flow members privileged. Nothing disappears — every symbol
    keeps its signature and its first doc line.
    # ponytail: one char budget for the whole subsystem, cheapest thing that
    # bounds the note; split the subsystem for real if it still reads as a
    # catalogue rather than an explanation
    """
    order = sorted(publics, key=lambda p: (p not in privileged, -graph.fan_in(p), p))
    spent = 0
    trimmed: set[str] = set()
    for path in order:
        if spent >= _DOC_BUDGET_CHARS:
            trimmed.add(path)
        for sym in publics[path]:
            if sym.get("brief"):
                continue          # already on the short budget (module-private)
            if spent < _DOC_BUDGET_CHARS:
                spent += len(sym.get("doc_full", ""))
            else:
                sym["brief"] = True
    return trimmed


def build_digests(graph: CodeGraph, subsystems: list[Subsystem], root: Path,
                  context: list[Subsystem] | None = None) -> list[SubsystemDigest]:
    """`context` names every file in the repo, so a scoped run still resolves
    its collaborators: keyed on `subsystems` alone, every edge leaving the
    scope hits an unknown file and the digest reports no collaborators at all.
    The scoped subsystems come last, so they win the key for their own files."""
    key_of = _file_to_key((context or []) + subsystems)
    all_calls = graph.call_edges()
    call_in: Counter = Counter()
    call_out: Counter = Counter()
    for src, tgt, _, _ in all_calls:
        if src != tgt:
            call_out[src] += 1
            call_in[tgt] += 1
    scripts = _pyproject_script_files(root, set(graph.files))
    adj = call_adjacency(graph)

    digests: list[SubsystemDigest] = []
    for sub in subsystems:
        member_set = set(sub.members)
        imp_out: Counter = Counter()
        imp_in: Counter = Counter()
        c_out: Counter = Counter()
        c_in: Counter = Counter()
        externals: set[str] = set()
        parse_errors = 0
        for path in sub.members:
            entry = graph.files[path]
            if entry.get("parse_error"):
                parse_errors += 1
            externals.update(entry.get("external", []))
            for tgt in entry.get("imports", []):
                other = key_of.get(tgt)
                if other and other != sub.key:
                    imp_out[other] += 1
        for path, entry in graph.files.items():
            if path in member_set:
                continue
            for tgt in entry.get("imports", []):
                if tgt in member_set:
                    other = key_of.get(path)
                    if other and other != sub.key:
                        imp_in[other] += 1
        sub_calls: list[tuple[str, str, str]] = []
        for src, tgt, callee, _caller in all_calls:
            src_in, tgt_in = src in member_set, tgt in member_set
            if not (src_in or tgt_in):
                continue
            sub_calls.append((src, tgt, callee))
            if src_in and not tgt_in and key_of.get(tgt):
                c_out[key_of[tgt]] += 1
            if tgt_in and not src_in and key_of.get(src):
                c_in[key_of[src]] += 1

        def _merge(imp: Counter, cal: Counter) -> list[tuple[str, int, int]]:
            keys = sorted(set(imp) | set(cal))
            return [(k, imp[k], cal[k]) for k in keys]

        hubs = sorted(((p, graph.fan_in(p)) for p in sub.members),
                      key=lambda t: (-t[1], t[0]))[:_HUB_CAP]
        eps = _entry_points(graph, sub, scripts)
        flows = flow_sketches(adj, [p for p, _ in eps],
                              prefixes=_inbound_prefixes(all_calls, member_set, key_of))
        publics = {p: _public_symbols(graph.files[p]) for p in sub.members}
        trimmed = _spend_doc_budget(
            publics, graph, {p for p, _ in eps} | {p for f in flows for p in f})
        digests.append(SubsystemDigest(
            key=sub.key, path=sub.path, members=sub.members,
            struct_sig=_struct_sig(sub.members, sub_calls,
                                   {p: graph.files[p] for p in sub.members}),
            public_symbols=publics,
            module_docs={p: graph.files[p].get("module_doc", "") for p in sub.members},
            # a hoisted inline comment is the least useful field on a file the
            # note only lists, so it goes with the rest of the trimmed budget
            module_comments={p: [] if p in trimmed
                             else graph.files[p].get("module_comments", [])
                             for p in sub.members},
            external_deps=sorted(externals),
            collaborators_out=_merge(imp_out, c_out),
            collaborators_in=_merge(imp_in, c_in),
            fan_in_hubs=hubs,
            entry_points=eps,
            flow_sketches=flows,
            parse_errors=parse_errors,
        ))
    return digests


def cross_edges(graph: CodeGraph, subsystems: list[Subsystem]) -> list[tuple[str, str, int, int]]:
    key_of = _file_to_key(subsystems)
    imp: Counter = Counter()
    cal: Counter = Counter()
    for path, entry in graph.files.items():
        a = key_of.get(path)
        if not a:
            continue
        for tgt in entry.get("imports", []):
            b = key_of.get(tgt)
            if b and b != a:
                imp[(a, b)] += 1
        for e in entry.get("calls", []):
            b = key_of.get(e["target"])
            if b and b != a:
                cal[(a, b)] += 1
    pairs = sorted(set(imp) | set(cal))
    return [(a, b, imp[(a, b)], cal[(a, b)]) for a, b in pairs]


def edges_ref(edges: list[tuple[str, str, int, int]]) -> str:
    pairs = sorted({(a, b) for a, b, _, _ in edges})
    return hashlib.sha256(orjson.dumps(pairs)).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Flow sketches + deterministic Mermaid
# ---------------------------------------------------------------------------

_FLOW_DEPTH = 6
_FLOWS_PER_ENTRY = 3
_FLOW_BRANCHING = 2
# ponytail: file-level flows via bounded BFS; symbol-level processes stay a seam


def call_adjacency(graph: CodeGraph) -> dict[str, list[str]]:
    """File -> called files, ordered by call weight desc then path."""
    weights: dict[str, Counter] = {}
    for src, tgt, _, _ in graph.call_edges():
        if src != tgt:
            weights.setdefault(src, Counter())[tgt] += 1
    return {src: [t for t, _ in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))]
            for src, c in weights.items()}


def flow_sketches(adj: dict[str, list[str]], entries: list[str],
                  prefixes: list[list[str]] | None = None) -> list[list[str]]:
    """Real call paths from each entry point: the LLM narrates these instead
    of guessing flows from import adjacency.

    `prefixes` are chain starts already in progress (an external caller and
    the member it calls), so a subsystem with no entry point of its own still
    shows how it is reached."""
    out: list[list[str]] = []
    seen: set[frozenset] = set()
    for start in [[e] for e in entries] + list(prefixes or []):
        count = 0
        queue: list[tuple[str, list[str]]] = [(start[-1], list(start))]
        while queue and count < _FLOWS_PER_ENTRY:
            node, chain = queue.pop(0)
            nxt = [t for t in adj.get(node, []) if t not in chain]
            if not nxt or len(chain) >= _FLOW_DEPTH:
                key = frozenset(chain)
                if len(chain) >= 2 and key not in seen:
                    seen.add(key)
                    out.append(chain)
                    count += 1
                continue
            for t in nxt[:_FLOW_BRANCHING]:
                queue.append((t, chain + [t]))
    return out


def render_mermaid(edges: list[tuple[str, str, int, int]]) -> str:
    """Cross-subsystem graph as a Mermaid block. Deterministic and
    byte-stable: sorted input, rendered by code, never by the LLM.
    Node ids are enumerated with the key as a quoted label: a key that is a
    Mermaid reserved word (`end`) or carries non-word characters (`(root)`)
    must never break the diagram."""
    nodes = sorted({n for a, b, _, _ in edges for n in (a, b)})
    nid = {n: f"n{i}" for i, n in enumerate(nodes)}
    lines = ["```mermaid", "graph LR"]
    for a, b, _iw, _cw in sorted(edges):
        qa, qb = a.replace('"', "'"), b.replace('"', "'")
        lines.append(f'  {nid[a]}["{qa}"] --> {nid[b]}["{qb}"]')
    lines.append("```")
    return "\n".join(lines)
