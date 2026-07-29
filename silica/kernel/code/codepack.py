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

import re
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
    """Shallowest declaration node of `kinds` whose `name` field reads `name`.
    Breadth-first on purpose: a nested class or method with the same name must
    never shadow the top-level one the selector addresses."""
    level = [node]
    while level:
        nxt = []
        for parent in level:
            for i in range(parent.named_child_count):
                child = parent.named_child(i)
                if child.type in kinds:
                    field = child.child_by_field_name("name")
                    if field is not None and src[field.start_byte:field.end_byte].decode(
                            "utf-8", errors="replace") == name:
                        return child
                nxt.append(child)
        level = nxt
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
        dropped.append(f"target: selector '{selector}' not found, degraded to a file-level pack")
    if len(source) <= budget_chars:
        return source.rstrip("\n"), "verbatim"
    outline = _outline(entry)
    if not outline:
        return source.rstrip("\n"), "verbatim"
    return outline, "outline"


# Each `extends`/`implements` clause is captured up to the next such keyword
# (or the end of the signature): a plain `[^{]+` is greedy and lets `extends`
# swallow a trailing `implements` clause whole, losing its bases.
_JAVA_SUPER = re.compile(
    r"\b(?:extends|implements)\s+((?:(?!\bextends\b|\bimplements\b)[^{])+)"
)
_PY_BASES = re.compile(r"\bclass\s+\w+\s*\(([^)]*)\)")
_CPP_BASES = re.compile(r"\b(?:class|struct)\s+\w+\s*:\s*([^{]+)")
# Dots included so a qualified name (`com.example.Base`, `Map.Entry`) is
# captured whole and reduced to its last segment, rather than truncated at
# the first dot.
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_GENERIC = re.compile(r"<[^<>]*>|\[[^\[\]]*\]")
_ACCESS = frozenset({"public", "private", "protected", "virtual"})


def _supertypes(signature: str) -> list[str]:
    """Declared bases of a class signature. The hierarchy is already in the
    store: `signature` is the declaration line verbatim, in all four families
    (spec section 1), so this is a regex and not an index."""
    groups = [m.group(1) for m in _JAVA_SUPER.finditer(signature)]
    for rx in (_PY_BASES, _CPP_BASES):
        m = rx.search(signature)
        if m:
            groups.append(m.group(1))
    out: list[str] = []
    for group in groups:
        # Strip bracket-nested spans (generic/template parameters) before
        # splitting on comma: a multi-parameter generic base like
        # `HashMap<K, V>` or `B[C, D]` must not read as two bases. Repeated
        # until stable so a nested generic strips from the inside out.
        prev = None
        while prev != group:
            prev, group = group, _GENERIC.sub("", group)
        for part in group.split(","):
            if "=" in part:
                continue  # a Python keyword argument (metaclass=...), not a base
            # first identifier only: it drops C++ access keywords. A
            # qualified name reduces to its last dotted segment, the class
            # itself rather than its package or enclosing type.
            idents = [i for i in _IDENT.findall(part) if i not in _ACCESS]
            if idents:
                base = idents[0].rsplit(".", 1)[-1]
                if base and base not in out:
                    out.append(base)
    return out


def _hierarchy(graph, path: str, entry: dict) -> list[tuple[str, str]]:
    """(label, line) pairs: the declared bases of each top-level class in the
    target, then every class in the repo that declares one of them.

    Known limitation, by design (declared facts only, no resolution): the
    repo scan matches bases by bare name, so two same-named classes in
    different packages can produce a false `Base <- path#Sub` edge."""
    out: list[tuple[str, str]] = []
    own: list[str] = []
    for s in entry.get("symbols", []):
        if s.get("kind") != "class" or s.get("parent"):
            continue
        name = s.get("name", "")
        own.append(name)
        bases = _supertypes(s.get("signature", ""))
        if bases:
            out.append((name, f"{name} extends {', '.join(bases)}"))
    if graph is None or not own:
        return out
    for p in sorted(graph.files):
        if p == path:
            continue
        for s in graph.files[p].get("symbols", []):
            if s.get("kind") != "class":
                continue
            for base in _supertypes(s.get("signature", "")):
                if base in own:
                    label = f"{p}#{s.get('name', '')}"
                    out.append((label, f"{base} <- {label}"))
    return out


def _signatures(entry: dict) -> str:
    """Public signatures of a neighbour file, methods indented. Public is
    spelled per family: no leading underscore (Python, TS; dunder names
    excepted, since a constructor is exactly the contract a port needs to
    read), no `private` modifier token anywhere in the declaration prefix
    (Java — catches `private void f()` and the legal-but-reordered
    `static private void f()` alike). A member whose own declaring class was
    filtered out is dropped too, tracked by bare name as symbols are walked
    in document order (`ModuleSkeleton.symbols` guarantees a class always
    precedes its own members — base.py, and every walker's class handler
    appends the class before recursing into its body), so a private inner
    class never leaks its public methods reparented onto the outer class.

    `hidden` is keyed by bare name, the only key `parent` ever carries, so a
    survivor re-opens its own name the moment it is emitted: two unrelated
    classes that happen to share a simple name (`Outer.Builder` filtered,
    `Other.Builder` public, both named "Builder") must not let the first
    poison the second's members. Document order makes this safe: by the time
    a later same-named class is read, any of the first one's still-hidden
    descendants have already been resolved.

    Known limitation: this cannot filter a private C++ member. `codeast/c.py`
    never records the `access_specifier` node (`private:` is a class-body
    section label, a sibling of the members it governs, not a per-member
    modifier token), so a private C++ method's `signature` looks identical to
    a public one. Teaching codeast about access specifiers is out of scope —
    `codeast.Symbol` must not change — so C++ neighbour signatures currently
    show every member, public or not."""
    lines: list[str] = []
    hidden: set[str] = set()
    for s in entry.get("symbols", []):
        name, parent = s.get("name", ""), s.get("parent", "")
        sig = s.get("signature", "")
        underscored = name.startswith("_") and not (name.startswith("__") and name.endswith("__"))
        private = "private" in sig.split("(", 1)[0].split()
        if parent in hidden or underscored or private:
            hidden.add(name)
            continue
        hidden.discard(name)  # a later same-named symbol that survives re-opens the name
        lines.append(("  " if parent else "") + sig)
    return "\n".join(lines)


# An import/include line always names the file it resolves to, so searching
# it unmasked would make every import a "mention" by definition and the
# filter below would exclude nothing. Blanked to same-length spaces (not
# deleted) so byte offsets elsewhere in the source stay valid: ordering by
# "first real use" still means position in the actual file.
_IMPORT_LINE = re.compile(r"^[ \t]*(?:import|from|#include|using)\b.*$", re.MULTILINE)


def _first_mention(source: str, path: str, entry: dict) -> int | None:
    """Offset of the first place `source` names this file: any top-level
    symbol name, or the file stem. `source` is expected to already have its
    import/include lines masked to blanks (see `_neighborhood`) — otherwise
    an import's own line always matches its own target, and the filter that
    is supposed to separate real uses from bare imports never excludes
    anything. None means it is never named outside an import, so it is not a
    neighbour. The filter took the median neighbourhood from 9 to 3 and the
    median pack from 6588 to 2351 characters (spec section 2)."""
    names = {s.get("name", "") for s in entry.get("symbols", []) if not s.get("parent")}
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if stem not in ("__init__", "index"):
        names.add(stem)
    best: int | None = None
    for name in names:
        if not name:
            continue
        m = re.search(rf"\b{re.escape(name)}\b", source)
        if m is not None and (best is None or m.start() < best):
            best = m.start()
    return best


def _neighborhood(graph, path: str, entry: dict,
                  source: str) -> list[tuple[str, str]]:
    """(label, block) pairs: resolved imports first, then package siblings,
    each group by first mention with the path as the tiebreak (spec section 4).
    An import crosses a package boundary, so it is a contract you cannot see by
    opening the folder next door; a sibling is one `ls` away.

    Mentions are searched for over `source` with its own import/include lines
    masked to same-length blanks first: an import line always names the file
    it imports, so searching the raw source would make the mention filter
    vacuous for group 1 (every import "mentions itself"). Same-length blanks
    keep the rest of the offsets meaningful, so `sorted(ranked)` still orders
    by real position in the file."""
    if graph is None:
        return []
    imports = [p for p in entry.get("imports", []) if p != path and p in graph.files]
    siblings: list[str] = []
    if entry.get("language") == "java":
        # D6: in Java the directory is the package, and a package sibling is
        # referenced with no import at all. One rule, instantiated per language:
        # every other family sees nothing beyond its explicit imports.
        folder = path.rpartition("/")[0]
        siblings = sorted(
            p for p, e in graph.files.items()
            if p != path and p not in imports
            and p.rpartition("/")[0] == folder and e.get("language") == "java"
        )
    body = _IMPORT_LINE.sub(lambda m: " " * len(m.group(0)), source)
    out: list[tuple[str, str]] = []
    for group in (imports, siblings):
        ranked = []
        for p in group:
            at = _first_mention(body, p, graph.files[p])
            if at is not None:
                ranked.append((at, p))
        for _, p in sorted(ranked):
            sigs = _signatures(graph.files[p])
            if sigs:
                out.append((p, f"{p}\n{sigs}"))
    return out


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
    fan_in = graph.fan_in(path) if graph is not None else 0
    importers = [(p, p) for p in (graph.importers(path) if graph is not None else [])]
    external = [(d, d) for d in entry.get("external", [])]
    for name, entries in (
        ("hierarchy", _hierarchy(graph, path, entry)),
        ("neighborhood", _neighborhood(graph, path, entry, source)),
        ("external", external),
        ("importers", importers),
    ):
        if not entries:
            continue
        header = f"## {name}" + (f" (fan-in {fan_in})" if name == "importers" else "")
        chunks.append(header + "\n" + "\n".join(line for _, line in entries))
        sections[name] = [label for label, _ in entries]
    return {
        "text": "\n\n".join(chunks) + "\n",
        "sections": sections,
        "dropped": dropped,
        "target_mode": mode,
        "truncated": mode != "verbatim",
        "head_ref": head_ref,
    }
