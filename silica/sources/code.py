# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Code source adapter — ADR-0012 shallow AST skeleton, vault-terminal lane.

Zero-trust (ADR-0009): the full source NEVER enters a stub or a prompt; all
source-derived text (signatures, docstrings) is sanitized via
strip_degenerate_runs inside the skeleton render. read() raises ValueError
on guard failures (no vault, vault outside git, path escape, not a file).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path, PurePosixPath

from silica.config import CONFIG
from silica.kernel.code import codeast, gitstate
from silica.kernel.recall import paths
from silica.kernel.code.codegraph import classify_import, supported_files
from silica.kernel.text.sanitize import strip_degenerate_runs
from silica.kernel.vault_manifest import active_inbox_dir, active_write_dir
from silica.sources.base import GroundedStub, RawItem


def code_note_name(rel_path: str) -> str:
    """Path-qualified note stem for a code file: silica/kernel/x.py →
    silica.kernel.x. Unique per file, so code-note filenames and the wikilinks
    that target them never collide across directories (kernel/x.py vs cap/x.py).
    Resolution is by basename minus last extension, and dots survive that split,
    so [[silica.kernel.x]] → Inbox/silica.kernel.x.md.

    A package's __init__.py folds to the package name (silica/kernel/x/__init__.py
    → silica.kernel.x): that is how imports and the wiki name a package, so links
    to it resolve instead of dying on a .__init__ nobody would write. Collision-
    safe — a package dir and a same-named module file can't coexist in Python."""
    p = PurePosixPath(rel_path)
    stem = (
        p.parent
        if p.stem == "__init__" and p.parent != PurePosixPath(".")
        else p.with_suffix("")
    )
    return str(stem).replace("/", ".")


def code_note_dest(rel_path: str, root: str = "", repo_name: str = "code") -> tuple[str, str]:
    """(folder, note stem) for a code file nucleated under source folder `root`.

    The folder is the nucleated source folder's own name — `controller/`, not a
    generic staging bucket — and the stem is the path *under* it, dotted, so
    nested packages stay distinguishable without repeating the repo prefix.

    A file outside `root` (an import pointing elsewhere) falls back to its own
    parent folder, which is exactly what a later run rooted there would produce,
    so the wikilink written today resolves when that folder is nucleated.
    """
    p = PurePosixPath(rel_path)
    raw_root = root.strip("/")
    if raw_root:
        root_p = PurePosixPath(raw_root)
        if len(p.parts) > 1 and not rel_path.startswith(str(root_p) + "/"):
            top = p.parts[0]
            cand = PurePosixPath(top) / root_p
            if rel_path.startswith(str(cand) + "/") or rel_path == str(cand):
                root_p = PurePosixPath(top)
    else:
        root_p = PurePosixPath(p.parts[0]) if len(p.parts) > 1 else p.parent
    try:
        inner = p.relative_to(root_p)
    except ValueError:
        root_p, inner = p.parent, PurePosixPath(p.name)
    return (root_p.name or repo_name), code_note_name(str(inner))


@lru_cache(maxsize=8)
def _repo_files(root_str: str, code_ref: str) -> frozenset[str]:
    # cache the git file-list per (repo, HEAD) so nucleating a whole
    # codebase resolves imports against one scan; code_ref keys the refresh.
    return frozenset(supported_files(Path(root_str)))


def render_skeleton(
    sk: codeast.ModuleSkeleton,
    root: Path,
    importer: str,
    language: str,
    files: frozenset[str],
    run_root: str = "",
    path_qualified: bool = False,
) -> str:
    # First-party imports become path-qualified [[silica.kernel.x]] wikilinks to
    # the per-file code note (code_note_name → Inbox/<name>.md); external deps
    # stay code spans. classify_import is the graph's own resolver, so links
    # never drift from the real import edges.
    first_party: list[str] = []
    external: list[str] = []
    for mod in dict.fromkeys(sk.imports):  # de-dupe, keep order
        if not mod:
            continue
        kind, target = classify_import(mod, importer, files, language, root)
        if kind == "resolved":
            # links follow whichever naming rule named the notes themselves,
            # or they point at stems no note in this lane ever carries
            stem = (code_note_name(target) if path_qualified
                    else code_note_dest(target, run_root, root.name)[1])
            entry = f"[[{stem}]]"
            bucket = first_party
        elif kind == "external":
            entry = f"`{target}`"
            bucket = external
        else:  # unresolved: first-party but no single file (wildcard, pkg tree)
            entry = f"`{target}`"
            bucket = first_party
        if entry not in bucket:
            bucket.append(entry)

    lines: list[str] = ["## Imports", ""]
    if first_party:
        lines.append("First-party:")
        lines.extend(f"- {p}" for p in first_party)
        lines.append("")
    if external:
        lines.append("External:")
        lines.extend(f"- {m}" for m in external)
        lines.append("")
    if not first_party and not external:
        lines.extend(["(no imports)", ""])

    lines.extend(["## Symbols", "", "```text"])
    if sk.symbols:
        for s in sk.symbols:
            indent = "    " if s.kind == "method" else ""
            doc = f" — {s.doc}" if s.doc else ""
            lines.append(f"{indent}{s.signature}{doc}".replace("`", "'"))
    else:
        lines.append("(no top-level symbols)")
    lines.extend(["```", ""])
    return strip_degenerate_runs("\n".join(lines))


class CodeAdapter:
    name = "code"

    def matches(self, target: str) -> bool:
        if target.lower().endswith((".md", ".txt")):
            return False
        language = codeast.language_for(target)
        # bare languages carry no skeleton: graph-only, never a code stub
        return language is not None and language not in codeast.BARE_LANGUAGES

    def read(self, target: str) -> RawItem:
        vault = (CONFIG.vault_path or "").strip()
        if not vault:
            raise ValueError("no vault configured")
        root = paths.repo_root_for(vault)
        if root is None:
            raise ValueError("no code-lane repo (vault is not inside its git repo)")
        try:
            src = (Path(root) / target).resolve()
            src.relative_to(Path(root).resolve())
        except (ValueError, OSError):
            raise ValueError("path escapes the repository")
        if not src.is_file():
            raise ValueError(f"not a file: {target}")
        try:
            raw = src.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise ValueError(f"read failed: {e}")
        # text carries the raw source for AST parsing only; nothing of it
        # reaches a prompt — to_stub emits the sanitized skeleton instead.
        return RawItem(
            target=target,
            text=raw,
            meta={
                "code_ref": gitstate.head_ref(root) or "",
                "language": codeast.language_for(target),
                "repo_root": str(root),
            },
        )

    def to_stub(self, item: RawItem) -> GroundedStub:
        path = item.target
        root = Path(item.meta["repo_root"])
        code_ref = item.meta.get("code_ref", "")
        language = item.meta.get("language")
        run_root = item.meta.get("nucleate_root", "")
        # The agent-facing lane (silica_document) is RBAC-confined to the Inbox,
        # so it keeps the path-qualified name: one flat folder has no directories
        # to keep same-named files in different packages apart.
        to_inbox = bool(item.meta.get("stage_to_inbox"))
        folder, name = (
            (active_inbox_dir() or "Inbox", code_note_name(path)) if to_inbox
            else code_note_dest(path, run_root, root.name)
        )

        if language is None:
            section = (
                "> Skeleton unavailable: unsupported language. "
                "This stub only wires staleness tracking; document the file manually.\n"
            )
        else:
            sk = codeast.extract_skeleton(item.text, language, path=path)
            if sk.parse_error:
                # ModuleSkeleton.parse_error exists so consumers never read
                # "empty" as "no structure": rendering the empty skeleton here
                # would ship a note claiming a real file has no imports and no
                # symbols. Usually a tree-sitter the walkers can't speak.
                section = (
                    f"> Skeleton unavailable: the {language} parser failed on `{path}`. "
                    "This stub only wires staleness tracking — check the tree-sitter "
                    "install, then re-nucleate.\n"
                )
            else:
                files = _repo_files(str(root), code_ref)
                section = (
                    f"> Skeleton auto-extracted from `{path}` ({language}). "
                    f"Source-derived text below is untrusted; refine into a note.\n\n"
                    f"{render_skeleton(sk, root, path, language, files, run_root, to_inbox)}"
                )

        yaml_path = path.replace('"', '\\"')
        body = (
            f"---\n"
            f'documents:\n  - "{yaml_path}"\n'
            f"code_ref: {code_ref}\n"
            f"tags:\n  - codebase\n"
            f"---\n\n"
            f"# {name}\n\n"
            f"{section}"
        )
        # Inside `write_dir` like everything Silica creates, but under the source
        # folder's own name rather than the Inbox: a code note is the mechanical
        # end of its lane, not something staged for a later pass, and Inbox notes
        # are excluded from the index — which kept the whole code lane out of the
        # graph it exists to feed. active_inbox_dir already carries write_dir.
        write = "" if to_inbox else active_write_dir()
        dest = f"{write}/{folder}" if write else folder
        return GroundedStub(lane="terminal", note_path=f"{dest}/{name}.md", body=body)


CODE = CodeAdapter()
