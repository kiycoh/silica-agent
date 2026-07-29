# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""codepack - deterministic context pack for one source file (spec-code-recall).

Not a ranker: a budgeted closure over facts the codegraph store already holds.
In the vault relevance is unknown and has to be estimated (RRF fusion, reranker);
in code it is given, and it is reachability. So no embeddings (D1), no language
server (ADR-0022), no scoring. Same repo state, same bytes, like
codegraph._serialize.

The asymmetry that makes the pack worth its budget: verbatim only what you are
about to rewrite, signatures for everything around it (D5).

# ponytail: kill by 2026-10-28 if unused; exposed via `silica mcp --all` only
"""
from __future__ import annotations

from pathlib import Path

from silica.kernel.code import codegraph
from silica.kernel.recall import paths as _paths

BUDGET_CHARS = 24000


def code_pack(vault: Path | str, target: str,
              budget_chars: int = BUDGET_CHARS) -> dict:
    """Context pack for `target` ("path", "path#Class" or "path#Class.member").

    Raises ValueError when the target file cannot be read: a bad path is a
    caller mistake, not a state to degrade around. Every other shortfall
    (no repo, no graph, unsupported language, empty neighbourhood) degrades to
    a poorer pack and says why in `dropped` (D4).
    """
    path, _, selector = target.partition("#")
    root = _paths.repo_root_for(vault) or Path(vault)
    try:
        source = (root / path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"cannot read target: {path} ({exc})") from exc

    graph = codegraph.load_codegraph(vault)
    dropped: list[str] = []
    if graph is None:
        dropped.append("neighborhood: no code graph (vault is not inside a git repo)")
        entry: dict = {}
        head_ref = ""
    else:
        head_ref = graph.head_ref
        entry = graph.files.get(path, {})
        if not entry:
            dropped.append(f"neighborhood: {path} is not in the code graph")

    body, mode = source.rstrip("\n"), "verbatim"
    chunks = [f"## target {path} @ {head_ref} mode: {mode}\n{body}"]
    sections: dict[str, list[str]] = {"target": [path]}
    return {
        "text": "\n\n".join(chunks) + "\n",
        "sections": sections,
        "dropped": dropped,
        "target_mode": mode,
        "truncated": mode != "verbatim",
        "head_ref": head_ref,
    }
