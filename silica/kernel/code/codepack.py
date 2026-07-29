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


def _outline(entry: dict, skip: str = "") -> str:
    """Signatures of a file, one per line, methods indented. This is the
    "everything around it" half of D5, and the fallback when the target itself
    is too big to serve whole."""
    head = skip.split(".", 1)[0]
    lines: list[str] = []
    for s in entry.get("symbols", []):
        name, parent = s.get("name", ""), s.get("parent", "")
        qual = f"{parent}.{name}" if parent else name
        if skip and (qual == skip or ("." not in skip
                and (parent == head or (parent == "" and name == head)))):
            continue
        doc = f"  # {s['doc']}" if s.get("doc") else ""
        lines.append(("  " if parent else "") + s.get("signature", "") + doc)
    return "\n".join(lines)


# Declaration node types per family, the same sets the codeast walkers use.
# C and C++ are absent on purpose: their names sit inside `declarator`, so a
# whole-file pack is the honest degrade (D4).
# ponytail: add the C/C++ declarator walk only if a real C target asks for it
_DECL_NODES: dict[str, tuple[str, ...]] = {
    "python": ("class_definition", "function_definition"),
    "java": ("class_declaration", "interface_declaration", "enum_declaration",
             "record_declaration", "annotation_type_declaration",
             "method_declaration", "constructor_declaration"),
    "typescript": ("class_declaration", "abstract_class_declaration",
                   "interface_declaration", "function_declaration",
                   "method_definition"),
}
_DECL_NODES["javascript"] = _DECL_NODES["typescript"]


def _find_decl(node, src: bytes, kinds: tuple[str, ...], name: str):
    """First declaration node of `kinds` whose `name` field reads `name`."""
    for i in range(node.named_child_count):
        child = node.named_child(i)
        if child.type in kinds:
            field = child.child_by_field_name("name")
            if field is not None and src[field.start_byte:field.end_byte].decode(
                    "utf-8", errors="replace") == name:
                return child
        found = _find_decl(child, src, kinds, name)
        if found is not None:
            return found
    return None


def _symbol_source(source: str, language: str, selector: str) -> str | None:
    """Verbatim declaration text for "Class", "Class.member" or a top-level
    name. Reparses this one file: no offset is persisted, so Symbol and
    STORE_VERSION stay untouched (D7). None when the family has no selector
    table or the name is not there."""
    kinds = _DECL_NODES.get(language or "")
    if not kinds:
        return None
    try:
        from tree_sitter_language_pack import get_parser
        src = source.encode("utf-8")
        node = get_parser(language).parse(src).root_node
    except Exception:
        return None
    outer, _, inner = selector.partition(".")
    node = _find_decl(node, src, kinds, outer)
    if node is not None and inner:
        node = _find_decl(node, src, kinds, inner)
    if node is None:
        return None
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _target_block(source: str, entry: dict, selector: str, budget_chars: int,
                  dropped: list[str]) -> tuple[str, str]:
    """(body, mode). Verbatim when it fits, the selected symbol plus the rest
    as an outline when a selector resolves, the file outline otherwise. An
    empty outline is not an improvement, so the truncated source stays: never
    serve less than the target (spec section 6)."""
    if selector:
        picked = _symbol_source(source, entry.get("language") or "", selector)
        if picked is not None:
            rest = _outline(entry, skip=selector)
            tail = f"\n\n-- rest of file, outline --\n{rest}" if rest else ""
            return picked.rstrip("\n") + tail, "symbol"
        dropped.append(f"target: selector '{selector}' not found, whole file served")
    if len(source) <= budget_chars:
        return source.rstrip("\n"), "verbatim"
    outline = _outline(entry)
    if not outline:
        return source.rstrip("\n"), "verbatim"
    return outline, "outline"


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

    body, mode = _target_block(source, entry, selector, budget_chars, dropped)
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
