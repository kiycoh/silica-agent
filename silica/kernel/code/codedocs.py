# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""codedocs — doc↔source staleness for codebase mode.

A note documents source files via frontmatter:
    documents: [src/m.py, src/n.py]   # repo-relative paths
    code_ref: <sha>                   # HEAD when last verified

A note is stale if any referenced path's newest commit differs from code_ref.
Staleness state lives in per-note frontmatter (ADR decision in the spec), not a
central index. All git access goes through gitstate and degrades soft.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from silica.kernel.code import codeast, gitstate

from silica.kernel.write import frontmatter

from silica.kernel.recall import paths
from silica.kernel.code.gitstate import CommitInfo

CHANGE_COSMETIC = "cosmetic"
CHANGE_STRUCTURAL = "structural"


@dataclass(frozen=True)
class StaleDoc:
    note_path: str          # vault-relative note path
    code_path: str          # repo-relative source path that changed
    recorded_ref: str       # code_ref stored in the note
    current_ref: str        # newest commit sha for code_path
    intervening: list[CommitInfo] = field(default_factory=list)
    change_level: str = CHANGE_STRUCTURAL   # conservative default (floor, not ceiling)
    details: list[str] = field(default_factory=list)


def documents_of(data: dict) -> list[str]:
    raw = (data or {}).get("documents")
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw]
    return [str(x) for x in raw if x]


_documents_of = documents_of  # internal alias (pre-rename call sites)


def validate_documents(entries: list[str], root: Path | str) -> tuple[list[str], str | None]:
    """Normalize agent-supplied `documents:` entries against the repo root.

    Trust boundary: the value reaches frontmatter and nothing else ever reads
    the path back, so a typo'd binding would be invisible forever. Rejects
    absolute paths, traversal and drive letters (same rule as `wiki_dir` in
    vault_manifest) and any entry that does not exist in the working tree.
    Returns (normalized, error): on error the caller must not write.
    """
    root = Path(root)
    out: list[str] = []
    for raw in entries or []:
        p = str(raw or "").strip().replace("\\", "/").rstrip("/")
        if not p:
            continue
        parts = p.split("/")
        if p.startswith("/") or ".." in parts or ":" in parts[0]:
            return [], f"documents entry must be a repo-relative path: {raw!r}"
        if not (root / p).exists():
            return [], f"documents entry does not exist in the repo: {p}"
        out.append(p)
    return list(dict.fromkeys(out)), None


def iter_documenting_notes(vault: Path | str):
    """Yield (note_path, data, body) for every note carrying `documents:`."""
    vault = Path(vault)
    for md in sorted(vault.rglob("*.md")):
        try:
            content = md.read_text(encoding="utf-8")
        except OSError:
            continue
        data, _, body = frontmatter.split(content)
        if not data or not _documents_of(data):
            continue
        yield md.relative_to(vault).as_posix(), data, body


def _skeleton_of(src: str, path: str, language):
    if path.lower().endswith(".ipynb"):
        from silica.kernel.code import ipynb
        cells = ipynb.parse_cells(src)          # ValueError → caller's fallback
        lang = ipynb.CODEAST_LANGUAGE.get(cells.language)
        if lang is None:
            raise ValueError("unsupported kernel language")
        return codeast.extract_skeleton(cells.code, lang, path=path)
    return codeast.extract_skeleton(src, language, path=path)


def classify_change(
    root: Path, base_ref: str, path: str, new_ref: str | None = None
) -> tuple[str, list[str]]:
    """Per-path verdict: skeleton of `path` at base_ref vs the working tree
    (or vs new_ref when given). Single conservative fallback branch: anything
    preventing structural analysis → STRUCTURAL with the named reason."""
    language = codeast.language_for(path)
    if language is None and not path.lower().endswith(".ipynb"):
        return CHANGE_STRUCTURAL, [f"{path}: no structural analysis (unsupported language)"]
    old_src = gitstate.show_file(root, base_ref, path)
    if old_src is None:
        return CHANGE_STRUCTURAL, [f"{path}: no structural analysis (ref {base_ref[:8]} unavailable)"]
    if new_ref is not None:
        new_src = gitstate.show_file(root, new_ref, path)
        if new_src is None:
            return CHANGE_STRUCTURAL, [f"{path}: deleted"]
    else:
        target = Path(root) / path
        if not target.is_file():
            return CHANGE_STRUCTURAL, [f"{path}: deleted"]
        try:
            new_src = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return CHANGE_STRUCTURAL, [f"{path}: no structural analysis (read failed)"]
    try:
        old_sk = _skeleton_of(old_src, path, language)
        new_sk = _skeleton_of(new_src, path, language)
    except ValueError as e:
        return CHANGE_STRUCTURAL, [f"{path}: no structural analysis ({e})"]
    if old_sk.parse_error or new_sk.parse_error:
        return CHANGE_STRUCTURAL, [f"{path}: no structural analysis (parse failed)"]
    diff = codeast.diff_skeletons(old_sk, new_sk)
    if not diff:
        return CHANGE_COSMETIC, []
    return CHANGE_STRUCTURAL, [f"{path}: {d}" for d in diff]


def note_verdict(docs: list[StaleDoc]) -> tuple[str, list[str]]:
    """Aggregate per-path verdicts for one note (spec §2): a single
    STRUCTURAL path makes the note structural; details concatenate."""
    level = (CHANGE_STRUCTURAL
             if any(d.change_level == CHANGE_STRUCTURAL for d in docs)
             else CHANGE_COSMETIC)
    return level, [line for d in docs for line in d.details]


def stale_docs(vault: Path | str, repo_root: Path | str | None = None) -> list[StaleDoc]:
    """Return one StaleDoc per (note, changed path). Empty when git is absent."""
    vault = Path(vault)
    root = Path(repo_root) if repo_root else paths.repo_root_for(vault)
    if root is None:
        return []

    notes = list(iter_documenting_notes(vault))
    wanted: set[str] = set()
    by_ref: dict[str, set[str]] = {}
    for _, data, _ in notes:
        recorded = str(data.get("code_ref") or "").strip()
        if recorded:
            docs = _documents_of(data)
            wanted.update(docs)
            by_ref.setdefault(recorded, set()).update(docs)
    latest = gitstate.latest_shas(root, sorted(wanted))
    # One history walk per distinct code_ref, not per (note, path): notes
    # written in the same session share a ref.
    touched = {ref: gitstate.paths_touched_since(root, ref, sorted(paths))
               for ref, paths in by_ref.items()}

    out: list[StaleDoc] = []
    for note_path, data, _ in notes:
        out.extend(_stale_for_note(root, note_path, data, latest, touched))
    return out


def _stale_for_note(root: Path, note_path: str, data: dict,
                    latest: dict[str, str], touched: dict) -> list[StaleDoc]:
    """Per-(note, path) staleness, shared by the vault scan and the read gate.

    One rule in one place: a second copy for the read path would drift from the
    report and the two would disagree about the same note.
    """
    recorded = str(data.get("code_ref") or "").strip()
    if not recorded:
        return []  # unknown → not stale
    out: list[StaleDoc] = []
    for code_path in _documents_of(data):
        current = latest.get(code_path, "")
        if not current:
            continue  # path has no history → unknown, not stale
        # Not `current != recorded`: code_ref is HEAD when the note was
        # verified, so that test fires for every path HEAD did not touch
        # and reports "stale" with zero intervening commits. The path is
        # stale only when a commit after `recorded` actually touched it.
        # None = the ref does not resolve at all → conservatively stale.
        moved = touched.get(recorded)
        if moved is None or code_path in moved:
            level, details = classify_change(root, recorded, code_path)
            out.append(
                StaleDoc(
                    note_path=note_path,
                    code_path=code_path,
                    recorded_ref=recorded,
                    current_ref=current,
                    intervening=gitstate.commits_since(root, recorded, code_path),
                    change_level=level,
                    details=details,
                )
            )
    return out


def read_warning(vault: Path | str, data: dict, repo_root: Path | str | None = None) -> str:
    """One-line staleness banner for a note being READ, "" when it is fine.

    The freshness signal already existed in frontmatter and was only ever
    consumed by the `/stale` report, so an agent reading a wiki note got no hint
    that it described a layout that had since moved. Two checks, cheapest first:

    - a `documents:` path that is gone from the working tree — free, and the
      loud case: after a refactor the note names a file nobody can open;
    - the git staleness rule above, for paths that still exist.

    Soft on every failure: a banner is an aid, never a reason a read fails.
    """
    try:
        docs = _documents_of(data)
        if not docs:
            return ""
        root = Path(repo_root) if repo_root else paths.repo_root_for(Path(vault))
        if root is None:
            return ""
        missing = [p for p in docs if not (root / p).exists()]
        if missing:
            return (f"[stale] documents {len(missing)}/{len(docs)} path(s) that no longer "
                    f"exist ({', '.join(missing[:3])}) — the source moved or was deleted "
                    f"since code_ref. Verify against the working tree before trusting this.")
        recorded = str(data.get("code_ref") or "").strip()
        if not recorded:
            return ""
        latest = gitstate.latest_shas(root, sorted(docs))
        touched = {recorded: gitstate.paths_touched_since(root, recorded, sorted(docs))}
        stale = _stale_for_note(root, "", data, latest, touched)
        if not stale:
            return ""
        level, _ = note_verdict(stale)
        changed = ", ".join(sorted({d.code_path for d in stale})[:3])
        return (f"[stale] {level} change in {len(stale)} documented path(s) since code_ref "
                f"{recorded[:8]} ({changed}). Verify against the source before trusting this.")
    except Exception:
        return ""


def stale_count(vault: Path | str) -> int:
    """Count of stale (note, path) pairs. Soft-zero on any failure / no git."""
    try:
        return len(stale_docs(vault))
    except Exception:
        return 0
