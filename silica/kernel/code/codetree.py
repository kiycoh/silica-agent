# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""codetree — the why-tree: notes bound to a repo path, its members, its containers.

`documents:` binds a note to a repo path; this module reads that binding back
as a containment roll-up. Nothing is materialized: the tree is computed at
query time over the real repo paths, so it cannot drift from the filesystem.
That is the whole argument for the lane — a derived index note would be a cache
with an invalidation problem, which is exactly what git already solves.

Pure function, no LLM, no store, no cache.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from silica.kernel import codedocs, gitstate, paths

# exact < member < container: a note about the path itself outranks one about a
# file inside it, which outranks one about the package above it.
_RANK = {"exact": 0, "member": 1, "container": 2}


@dataclass(frozen=True)
class WhyNote:
    note_path: str    # vault-relative
    bound_path: str   # the documents: entry that matched
    relation: str     # "exact" | "member" | "container"
    distance: int     # path segments between bound_path and the query
    stale: bool       # file bindings only; always False for a directory
    hook: str         # first non-empty body line, so the caller can triage


def _norm(p: str) -> str:
    return str(p or "").strip().replace("\\", "/").strip("/")


def _segments(p: str) -> int:
    return len(p.split("/")) if p else 0


def _relate(query: str, q_segments: int, bound: str) -> tuple[str | None, int]:
    """How `bound` sits relative to `query`. Plain prefix containment: repo
    paths are posix strings, so walking the real tree would answer the same
    question with a stat per node."""
    if bound == query:
        return "exact", 0
    if not query or bound.startswith(query + "/"):
        return "member", _segments(bound) - q_segments
    if query.startswith(bound + "/"):
        return "container", q_segments - _segments(bound)
    return None, 0


def why_for(
    vault: Path | str,
    path: str,
    *,
    repo_root: Path | str | None = None,
    cap: int = 20,
) -> tuple[list[WhyNote], int]:
    """Notes bound to `path`, to anything under it, and to its containers.

    `path` is repo-relative; "" means the repo root. Returns (notes, residue)
    where residue is the count dropped by the cap — declared, never a silent
    truncation (same convention as `_capped` in capabilities/codewiki.py).
    """
    root = Path(repo_root) if repo_root else paths.repo_root_for(vault)
    if root is None:
        return [], 0

    query = _norm(path)
    q_segments = _segments(query)

    # ponytail: iter_documenting_notes rglobs the whole vault per call, same as
    # /stale. Add an index only if the gate's latency numbers show it matters.
    # ponytail: containment only. Rolling up along imports (a path's importers)
    # is a graph, not a tree, and codegraph already holds it — not v1.
    best: dict[str, tuple[WhyNote, str]] = {}   # note_path -> (hit, code_ref)
    for note_path, data, body in codedocs.iter_documenting_notes(vault):
        hook = next((ln.strip() for ln in (body or "").splitlines() if ln.strip()), "")
        recorded = str((data or {}).get("code_ref") or "").strip()
        for raw in codedocs.documents_of(data):
            bound = _norm(raw)
            relation, distance = _relate(query, q_segments, bound)
            if relation is None:
                continue
            hit = WhyNote(note_path=note_path, bound_path=bound, relation=relation,
                          distance=distance, stale=False, hook=hook)
            # One entry per note: a note bound to both a file and its package
            # would otherwise spend the cap twice saying the same thing.
            prior = best.get(note_path)
            if prior is None or _sort_key(hit) < _sort_key(prior[0]):
                best[note_path] = (hit, recorded)

    hits = _mark_stale(root, list(best.values()))
    hits.sort(key=_sort_key)
    return hits[:cap], max(0, len(hits) - cap)


def _sort_key(n: WhyNote) -> tuple[int, int, str]:
    return (_RANK.get(n.relation, len(_RANK)), n.distance, n.note_path)


def _mark_stale(root: Path, entries: list[tuple[WhyNote, str]]) -> list[WhyNote]:
    """Set `stale` on file bindings a commit touched after the note's `code_ref`.

    One history walk per distinct code_ref, the same primitive /stale uses. This
    is the boolean, not the verdict: the structural-vs-cosmetic AST diff is
    expensive and /stale already owns it. Directory bindings are never stale —
    a rationale does not expire because some file under the package changed.
    """
    if gitstate.head_ref(root) is None:
        return [hit for hit, _ in entries]   # no git → staleness is unknowable
    by_ref: dict[str, set[str]] = {}
    for hit, ref in entries:
        if ref and (root / hit.bound_path).is_file():
            by_ref.setdefault(ref, set()).add(hit.bound_path)
    touched = {ref: gitstate.paths_touched_since(root, ref, sorted(paths))
               for ref, paths in by_ref.items()}

    def _stale(hit: WhyNote, ref: str) -> bool:
        if ref not in by_ref or hit.bound_path not in by_ref[ref]:
            return False                     # directory binding, or no code_ref
        moved = touched.get(ref)
        return moved is None or hit.bound_path in moved   # None = ref gone → stale

    return [replace(hit, stale=True) if _stale(hit, ref) else hit
            for hit, ref in entries]
