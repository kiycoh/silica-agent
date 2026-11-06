# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Vault layout: canonical note paths and which folders are the vault's own.

The CLI backend resolves notes by their vault-relative POSIX path. Any code
path that accepts a user- or agent-supplied note path MUST canonicalize it
through ``to_vault_relative`` before handing it to the driver: otherwise
absolute filesystem paths reach the Obsidian CLI verbatim and surface as a
misleading "No matches found" / "File not found", because the CLI indexes
by vault-relative path.
"""
from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path

from silica.config import CONFIG

logger = logging.getLogger(__name__)

DEFAULT_LOG_FILENAME = "log.md"


def in_folder(path: str, folder: str | None) -> bool:
    """True if vault-rel `path` is inside `folder` (empty folder ⇒ whole vault).

    Single source of truth for folder-scoping: index reconciliation
    (embed/cooccur build_index), the /embed, /cooccur, /dedup tools, the
    graph-report signals, and the cooccur/relatedness scope filters. The `.md`
    strip is a no-op on keyspaces that are already suffix-free (cooccur_key).
    """
    if not folder:
        return True
    f = folder.replace("\\", "/").strip("/").lower()
    p = path.replace("\\", "/").strip("/").lower().removesuffix(".md")
    return p == f or p.startswith(f + "/")


def vault_epoch(vault: str | None = None) -> str:
    """Cheap validity signature of a vault's current file state, or "".

    One stat walk — sorted (path, mtime_ns, size) folded into a hash: the memo
    key for vault-wide derivatives (report, graph payload, timeline). Any
    create, edit, delete or move changes it, out-of-band ones included, since
    this observes the disk and not an index. Size rides along so an
    mtime-preserving restore (rsync -a, Syncthing) still bumps it.

    Deliberately NOT debounced: a cached epoch would vouch for a vault it has
    not looked at, and an edit inside the window would serve a pre-edit
    derivative as fresh. The stat walk is the whole price (~10 ms per 1k
    notes). Returns "" when no signature can be taken (unbound vault): ""
    means "do not memoize", never "nothing changed".

    Deliberately driver-free: the graph viewer split forbids this chain from
    reaching the agent layer, and a stat walk needs nothing above stdlib.
    """
    import hashlib

    raw = vault or getattr(CONFIG, "vault_path", "") or ""
    if not str(raw).strip():
        return ""  # unbound — Path("") would silently mean the CWD
    root = Path(raw)
    if not root.is_dir():
        return ""
    try:
        ignored = ignore_matcher(root)
        h = hashlib.sha1(str(root).encode("utf-8"))
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames if not d.startswith(".") and not ignored(d)
            )
            for fn in sorted(filenames):
                if not fn.endswith(".md"):
                    continue
                p = Path(dirpath) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue  # deleted mid-walk: absent, like the sweep treats it
                h.update(
                    f"{p.relative_to(root)}\x00{st.st_mtime_ns}\x00{st.st_size}\x00"
                    .encode("utf-8", errors="replace")
                )
        return h.hexdigest()
    except Exception as e:
        logger.debug("vault_epoch unavailable (%s)", e)
        return ""


# ---------------------------------------------------------------------------
# Verbatim source leaves (spec-harness-promotion 2026-07-24 §2)
# ---------------------------------------------------------------------------

# Vault folder holding verbatim source leaves. Leaves are retrieval-invisible:
# excluded from search, embeddings, co-occurrence, and the autolink title
# index (one rule, all indexes — partial exclusion reintroduces the dilution
# the LoCoMo hybrid arm measured). A leaf is reachable only through an
# explicit `## Sources` wikilink and silica_read_note.
SOURCES_DIR = "sources"


# The H2 under which a note links back to its verbatim leaf. Its presence is
# also the note-side signal that the source is still retrievable, which is what
# separates a grounded claim from a purely distilled one (reliability_tier).
SOURCES_MARKER = "## Sources"


# Two rules disagree (probe 2026-09-17): this hides leaves from every index
# built on list_files, while the silica-core engine behind silica_search_context
# walks sources/ and ranked a leaf first. Verbatim outranking its distilled note
# is the measured good outcome for answering (verbatim 0.840 vs distill 0.693,
# docs/evaluation/v1-substrate/V1-substrate-pin.md:87-89) and a defect only for
# structure indexes. S3 of docs/audits/2026-09-17-paper-backed-simplifications.md.
def is_source_leaf(path: str) -> bool:
    """True when `path` (vault-relative, any separator) lives under sources/.

    A vault whose writes are confined to `silica/` stores its leaves at
    `silica/sources/`, and those were answering every search: the bare-root
    check missed them, so the verbatim lectures competed with the notes
    distilled from them and won, being longer. Both roots answer, like
    `run_log` composes its journal path — legacy root leaves stay invisible
    forever.
    """
    norm = (path or "").replace("\\", "/").lstrip("/")
    if norm.startswith(SOURCES_DIR + "/"):
        return True
    try:
        from silica.kernel.base.manifest import in_write_dir

        composed = in_write_dir(SOURCES_DIR).replace("\\", "/").strip("/")
    except Exception:  # config not resolvable — the bare root is the honest guess
        return False
    return bool(composed) and norm.startswith(composed + "/")


# Directories never walked when sampling or indexing a vault: vendored trees and
# build output, never someone's notes. Hidden dirs are pruned separately (that
# covers .git/.venv/.obsidian). Deliberately NOT `.gitignore`: a gitignored
# folder can be exactly where private notes live (this repo's own `docs/`), so
# honouring it would hide notes. Per-vault additions go in `.silicaignore`.
# Directories a shell or a GUI client can start in that are never a vault: the
# FHS top level, macOS's, and the temp dir. Exact match only, /tmp/<x> is
# somebody's folder while /tmp is nobody's. A root process with cwd /tmp
# adopted it and wrote /tmp/vault.yaml (2026-09-02), which the session hook
# then found above every pytest tmp dir and greeted with the wrong vault.
SYSTEM_DIRS: frozenset[str] = frozenset({
    "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/media",
    "/mnt", "/opt", "/proc", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr",
    "/var", "/var/tmp", "/private/tmp", "/Users", "/Volumes", "/Applications",
    "/System", "/Library", tempfile.gettempdir(),
})


def is_system_dir(path) -> bool:
    return str(Path(path).resolve()) in SYSTEM_DIRS


NOISE_DIRS: frozenset[str] = frozenset({
    "node_modules", "vendor", "build", "dist", "target", "__pycache__",
    "site-packages", "coverage", "htmlcov",
})


# Per-vault extension of NOISE_DIRS, because "grows when it bites" should not
# mean "edit the source". Seeded by `onboarding.adopt.seed_silicaignore`.
SILICAIGNORE_REL = ".silicaignore"


def _ignore_patterns(vault) -> tuple[frozenset[str], tuple[str, ...]]:
    """(exact names, globs) to prune: NOISE_DIRS plus `<vault>/.silicaignore`.

    Split so the common case stays a set lookup; a missing or unreadable file
    means the built-ins alone (an ignore file is a convenience, never a gate).
    """
    names, globs = set(NOISE_DIRS), []
    try:
        text = (Path(vault) / SILICAIGNORE_REL).read_text(encoding="utf-8")
    except OSError:
        return frozenset(names), ()
    for line in text.splitlines():
        pat = line.split("#", 1)[0].strip().strip("/")
        if not pat:
            continue
        (globs.append(pat) if any(c in pat for c in "*?[") else names.add(pat))
    return frozenset(names), tuple(globs)


def ignore_matcher(vault) -> Callable[[str], bool]:
    """Predicate over a directory NAME: True ⇒ do not walk it.

    Matches by name at any depth, like NOISE_DIRS itself — not by path, so
    `docs/private` does not work but `private` and `*.egg-info` do.
    ponytail: name-only matching, add path anchoring when someone needs it.

    Reads the file once: build the matcher before a walk, never inside it.
    """
    names, globs = _ignore_patterns(vault)
    if not globs:
        return names.__contains__
    return lambda d: d in names or any(fnmatch(d, g) for g in globs)


def _is_under(base: Path, target: Path) -> bool:
    """True when `target` is strictly inside `base` (both already normalized)."""
    return target != base and base in target.parents


def contain_in_vault(path: str, vault: Path) -> str:
    """The vault containment choke point: `path` as a safe POSIX vault-relative
    path, or ``ValueError``.

    Every driver write MUST pass its caller-supplied path through here before
    joining it onto the vault root. ``Path(vault) / rel`` silently DISCARDS the
    vault root when `rel` is absolute, and joins a `../..` verbatim, so a path
    that merely *looks* vault-relative is not a boundary — this is.

    Accepted: ordinary relative paths, and absolute paths under the vault
    (relativized). Rejected: absolute paths outside the vault, any ``..`` that
    escapes, a symlink whose target leaves the vault, and the vault root itself
    (a note is never the directory).

    Containment is decided on the fully resolved paths — a note that is a
    symlink out of the vault escapes on write even though its own path reads as
    vault-relative. Existence is NOT required: resolving a not-yet-created file
    only normalizes, which is what `create()` needs.
    """
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("Empty path is not a valid vault reference")

    root = Path(vault)
    candidate = Path(raw.replace("\\", "/"))
    if not candidate.is_absolute():
        candidate = root / candidate

    real_root = Path(os.path.realpath(root))
    real = Path(os.path.realpath(candidate))
    if not _is_under(real_root, real):
        raise ValueError(
            f"Path {path!r} escapes the vault {root.as_posix()!r}"
        )

    # Prefer the lexical form so an intra-vault symlinked folder keeps the key
    # the index already uses; fall back to the resolved one (an unresolved or
    # symlinked vault root), which is contained by construction.
    #
    # The lexical form is only usable when it still names the file containment
    # was decided on: `normpath` cancels `a/..` textually, which is a LIE as
    # soon as `a` is a symlink. `out/back/../x.md`, with `out` leaving the vault
    # and `back` pointing into it, resolves inside (so it passes above) yet
    # collapses to `out/x.md` — and the caller rejoins that onto the vault root,
    # writing through `out` to the outside. Comparing the two resolutions costs
    # one realpath and closes that gap.
    root_norm = Path(os.path.normpath(str(root)))
    cand_norm = Path(os.path.normpath(str(candidate)))
    if _is_under(root_norm, cand_norm) and Path(os.path.realpath(cand_norm)) == real:
        return cand_norm.relative_to(root_norm).as_posix()
    return real.relative_to(real_root).as_posix()


def to_vault_relative(path: str, *, ensure_md: bool = True) -> str:
    """Normalize an arbitrary note path to POSIX vault-relative form.

    Rules:
      - already-relative paths pass through (POSIX-normalized, leading
        slashes stripped);
      - absolute paths *under* the configured vault root are relativized;
      - absolute paths *outside* the vault, and relative paths whose ``..``
        segments walk out of it, raise ``ValueError`` with a clear diagnostic —
        they would otherwise become a silent "File not found" when the CLI
        fails to resolve them, or worse, a write outside the vault;
      - if ``ensure_md`` is True (default) and the result does not end in
        ``.md``, the extension is appended.

    The vault root is read at call time from ``CONFIG.vault_path`` so test
    fixtures that mutate the singleton continue to work.
    """
    if not path:
        raise ValueError("Empty path is not a valid vault reference")

    p = Path(path)
    vault_str = getattr(CONFIG, "vault_path", None) or ""
    if not vault_str:
        if p.is_absolute():
            raise ValueError(
                f"Absolute path {path!r} provided but SILICA_VAULT is not configured"
            )
        # No vault to contain against: the historical pass-through, unchanged.
        rel = p.as_posix().strip("/")
    else:
        try:
            rel = contain_in_vault(path, Path(vault_str))
        except ValueError as exc:
            # Wording kept verbatim: this diagnostic is what the CLI surfaces to
            # the user for a bad /organize --scope, and it is asserted on.
            raise ValueError(
                f"Path {path!r} is outside the configured vault "
                f"{Path(vault_str).as_posix()!r}"
            ) from exc

    if ensure_md and not rel.endswith(".md"):
        rel += ".md"
    return rel


# Where a chat keeps the files attached to it (ADR-0121 point 5): hidden, so no
# walk lists it, and inside the vault, so the read tools reach it. Not
# `.silica/chat`: with the vault at $HOME that is the config dir those tools refuse.
CHAT_DIR = ".silica-chat"


def chat_folder(session: str) -> str:
    """A chat's folder, vault-relative, inside the write boundary: a vault that
    writes only under docs/silica (a repository) gets no folder at its root."""
    from silica.kernel.base.manifest import active_write_dir

    base = active_write_dir().strip("/")
    return f"{base}/{CHAT_DIR}/{session}" if base else f"{CHAT_DIR}/{session}"


def is_chat_path(path: str) -> bool:
    """True for a vault-relative path inside a chat's folder of attached files."""
    return CHAT_DIR in Path(path).parts[:-1]


def is_inbox_path(path: str) -> bool:
    """True when a vault-relative path sits anywhere under an inbox root
    (case-insensitive). The inbox is staging, never a write or merge target —
    callers use this to filter candidates and reject ops.

    EVERY root answers, composed and bare, whichever one `active_inbox_dir`
    happens to resolve to for this vault. The question here is "may an op target
    this?", and the answer is no for both the inbox Silica stages into and the
    one the user keeps — deriving the set from the resolved inbox alone left
    whichever lost the resolution unguarded, and patch ops aimed at it stopped
    being rejected.
    """
    from silica.config import CONFIG
    from silica.kernel.base.manifest import active_inbox_dir, in_write_dir

    p = path.replace("\\", "/").lstrip("/").casefold()
    inbox = (getattr(CONFIG, "inbox_dir", "") or "").strip("/")
    roots = {active_inbox_dir(), in_write_dir(inbox), inbox, "Inbox"}
    return any(p.startswith(r.casefold() + "/") for r in roots if r)


def is_staging_path(path: str) -> bool:
    """True for Silica's own staging paths: the inbox and the `done/` archive.

    Neither is knowledge — the inbox is material awaiting distillation, `done/`
    is what has already been distilled — so nothing links to them by design and
    calling them orphaned reports Silica's bookkeeping as vault damage. One
    predicate for the report's orphans and the orphan pass's candidates and
    targets (owner ruling 2026-09-27: the pass leaves inbox and done/ alone).
    `kernel/graph_diff._is_staging` is an older, looser copy (any `done/`
    segment) kept for the regression gate.
    """
    from silica.kernel.base.manifest import active_done_dir, in_write_dir

    from silica.kernel.base.layout import DEFAULT_LOG_FILENAME

    p = (path or "").replace("\\", "/").lstrip("/")
    if is_inbox_path(p):
        return True
    if p.casefold() == in_write_dir(DEFAULT_LOG_FILENAME).casefold():
        return True  # the run journal, same reason
    done = active_done_dir().casefold()
    return bool(done) and p.casefold().startswith(done + "/")


def resolve_target_dir(target_dir: str) -> str:
    """Fold a user-typed vault folder onto the existing tree, case-insensitively.

    'Informatica/Intelligenza Artificiale' typed against a vault holding
    'Informatica/Intelligenza artificiale' silently forks the tree on a
    case-sensitive filesystem: new-note writes ENOENT through the Obsidian
    bridge and patch paths mismatch their expected collisions. Each segment
    adopts the casing of an existing folder when one matches case-insensitively;
    unmatched segments keep the typed casing (a genuinely new folder).
    Absolute paths and unconfigured vaults pass through untouched.
    """
    vault_str = getattr(CONFIG, "vault_path", None) or ""
    if not target_dir or not vault_str or Path(target_dir).is_absolute():
        return target_dir
    base = Path(vault_str)
    resolved: list[str] = []
    for seg in Path(target_dir.strip("/")).parts:
        cur = base.joinpath(*resolved)
        if not (cur / seg).is_dir() and cur.is_dir():
            seg = next(
                (e.name for e in cur.iterdir()
                 if e.is_dir() and e.name.casefold() == seg.casefold()),
                seg,
            )
        resolved.append(seg)
    return "/".join(resolved)


# Silica's own generated files at the vault ROOT (log.md, GRAPH_REPORT.md) are
# tooling output, not knowledge notes. The driver indexes them like any note, so
# without this filter GRAPH_REPORT.md's hundreds of `[[...]]` would make it the
# top hub AND give every note it lists an incoming link — silently zeroing the
# orphan count on the next run. Matched by root-relative stem only, so a genuine
# note in a subfolder (e.g. "Concepts/log.md") stays in the graph.
_VAULT_ROOT_ARTIFACT_STEMS = frozenset({"log", "GRAPH_REPORT"})


def is_vault_artifact(note_id: str) -> bool:
    """True if `note_id` is a Silica-generated file at the vault root.

    Id form varies by caller (graph node ids carry `.md`; other callers may
    not), so this matches on the `.md`-stripped stem and requires no path
    separator — i.e. vault-root only.
    """
    stem = note_id.replace("\\", "/").removesuffix(".md")
    return "/" not in stem and stem in _VAULT_ROOT_ARTIFACT_STEMS
