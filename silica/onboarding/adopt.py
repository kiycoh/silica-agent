# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Vault adoption — declare the write boundary once, at the moment a path becomes
the vault.

A vault path is adopted as-is: Silica reads the folder the user pointed at, not a
subfolder it invented. What still needs deciding is where it may *write*, and the
answer differs by content:

  * a folder of prose, Obsidian vault included → notes land in place, next to
    the notes they belong with. Safe mode confines them instead to `silica`, a
    staging mirror of the vault's own tree, merged by a plain file-manager paste
    of its contents over the vault root — opt in from the settings toggle or by
    writing `write_dir: silica` in vault.yaml;
  * a source tree → notes land in `docs/silica`, which is Silica's own folder in
    that repo rather than a mirror of it. Not safe mode: a repo has a place for
    its docs whether or not the staging lane is on.

Either way the choice is declared in `vault.yaml` so it is visible, versionable
and editable instead of re-guessed every run — and reversible from one toggle in
the settings panel, which writes this same field.

No prompt: the detection picks a default, the caller prints it, and `vault.yaml`
makes it changeable. A question at adoption time would block the GUI entry
point for a decision one line of YAML already expresses.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple

from silica.config import CONFIG
from silica.kernel.base.layout import NOISE_DIRS, SILICAIGNORE_REL
from silica.onboarding.code_probe import looks_like_code
from silica.kernel.base.manifest import MANIFEST_REL

from silica.kernel.base.manifest import CODE_WRITE_DIR, SAFE_WRITE_DIR

logger = logging.getLogger(__name__)

_SILICAIGNORE_HEADER = """\
# .silicaignore — directory names Silica never walks when indexing this vault.
#
# One name or glob per line; `#` starts a comment. Matched against the directory
# NAME at any depth, not against a path. Hidden dirs (.git, .venv, .obsidian)
# are always skipped, and .gitignore is deliberately NOT honoured — a gitignored
# folder is often exactly where private notes live.
#
# The list below is built in. It is here to be read and extended, so
# uncommenting a line changes nothing; add your own below it.
"""


def seed_silicaignore(vault: str | Path) -> Path | None:
    """Write the `.silicaignore` template into a source-tree vault, once.

    Returns the path when this call created it, else None: the file is already
    there (never overwrite a hand-edited one), the vault reads as prose (a notes
    folder has no vendored trees to prune), or the root is unwritable.
    """
    root = Path(vault)
    target = root / SILICAIGNORE_REL
    if not root.is_dir() or target.exists() or not looks_like_code(root):
        return None
    body = "".join(f"# {d}\n" for d in sorted(NOISE_DIRS))
    try:
        target.write_text(_SILICAIGNORE_HEADER + body, encoding="utf-8")
    except OSError as exc:
        logger.warning("could not write %s (%s) — built-in ignores still apply", target, exc)
        return None
    return target


def write_dir_for(vault: str | Path) -> str:
    """The write boundary this folder's *content* calls for when confined; ""
    ⇒ in place.

    Pure decision, no I/O beyond the detection scan. Callers that compose
    `vault.yaml` themselves (the first-run wizard) use this; `declare_write_dir`
    is the persisting variant, and the settings toggle re-derives from here
    rather than restoring a remembered value — there is no "previous write_dir"
    state to go stale.

    This answers where writes go *if* they are confined, which is why the prose
    branch says `silica` even though safe mode is off by default: it is the
    toggle's ON value, not the adoption default. `declare_write_dir` is what
    decides whether a fresh vault starts confined at all.

    A `docs/silica` that already holds notes settles it whatever the content
    ratio says: that is a vault from before the write boundary existed, and the
    same declaration every new repo gets is also its migration — the notes stay
    where they are, the vault goes back to being the folder you launched in.
    """
    root = Path(vault)
    if not root.is_dir():
        return ""
    # glob is lazy and stops at the first hit; a missing dir yields nothing.
    if next((root / CODE_WRITE_DIR).glob("**/*.md"), None):
        return CODE_WRITE_DIR
    return CODE_WRITE_DIR if looks_like_code(root) else SAFE_WRITE_DIR


def declare_write_dir(vault: str | Path) -> str | None:
    """Persist `write_dir` in `<vault>/vault.yaml` when the vault needs one.

    Returns the declared value when this call wrote it (the caller announces it),
    or None when there was nothing to declare: a manifest already exists (the
    vault has spoken, never overrule it), the path is not a folder, or the
    content only calls for the safe-mode mirror.

    That last case is the default: safe mode is OFF until asked for, so a prose
    vault files in place and gets no `vault.yaml` at all. Only a source tree —
    which needs a home for its docs either way — is declared here. Turning safe
    mode on writes the line that this function deliberately does not.

    Deliberately creates no directory: an empty `docs/silica` would then read as
    a pre-`write_dir` vault to every back-compat lookup.
    """
    root = Path(vault)
    manifest = root / MANIFEST_REL
    if not root.is_dir() or manifest.exists():
        return None
    declared = write_dir_for(root)
    if not declared or declared == SAFE_WRITE_DIR:
        return None
    try:
        manifest.write_text(f"write_dir: {declared}\n", encoding="utf-8")
    except OSError as exc:
        # A read-only or unwritable vault root is the user's business, not a
        # crash: fall through to the in-place default and say nothing new.
        logger.warning("could not write %s (%s) — write_dir stays the default", manifest, exc)
        return None
    return declared

# Lives here and not in ui/bootstrap, which startup reads it from: doctor
# (onboarding.checks) must apply the same rule, and checks is imported by
# tools.atomic, so reaching it through cli pulled every capability worker
# under tools and broke the P9 contract (lint-imports, 2026-10-02).
def resolve_cwd_vault(cwd, home=None):
    """Pure resolver for the vault a `silica` launched in `cwd` curates.

    Returns the directory to adopt, or None when this place is not a vault and
    the caller should fall back. The shell already says which vault you mean, so
    the working directory decides — a SILICA_VAULT constant in a .env would
    otherwise follow you into every other project.

    - inside a git repo → the repo root (one project is one vault, from any of
      its subdirectories);
    - anywhere else → cwd itself;
    - $HOME or the filesystem root → None: a vault is a folder of notes, not
      everything you own. The root is not reachable by launching a shell there
      but a spawned process can start with cwd ``/`` (a GUI client's stdio
      server did, before ADR-0130), and indexing the whole disk is never what
      that meant.
    - a system directory itself (`layout.SYSTEM_DIRS`: /tmp, /usr, the
      temp dir...) → None, same reason; a folder inside one is still a vault.

    Adoption of the returned path (a pre-existing ``docs/silica`` under it still
    wins for back-compat) belongs to ``resolve_vault_switch``; where writes may
    land inside it is the separate `write_dir` axis (`onboarding.adopt`).
    """
    from pathlib import Path
    from silica.kernel.base import gitstate
    from silica.kernel.base.layout import is_system_dir

    cwd = Path(cwd).resolve()
    if cwd == Path(home or Path.home()).resolve() or cwd == Path(cwd.anchor):
        return None
    if is_system_dir(cwd):
        return None
    root = gitstate.find_repo_root(cwd)
    if root is None:
        return str(cwd)
    return str(Path(root).resolve())


class VaultTarget(NamedTuple):
    """Outcome of resolving a runtime ``/vault <arg>`` switch.

    ``vault`` is the absolute path to adopt, ``created`` True when the directory
    does not exist yet and the caller must mkdir it. ``error`` is set (and the
    other fields meaningless) only when the path cannot be a vault at all.
    """
    vault: str
    created: bool
    error: str | None = None


def resolve_vault_switch(arg: str) -> VaultTarget:
    """Resolve a ``/vault <arg>`` (or explicit ``SILICA_VAULT``) target.

    The path is adopted **as-is**, always: the vault is the folder the user
    named, never a subfolder Silica invents or remembers. Whether notes may be
    written into that root or into a subtree is a separate axis, declared
    per-vault as `write_dir` in ``vault.yaml`` (see `onboarding.adopt`).

    So a vault created before that split reads its whole repo again on the next
    launch, while its notes stay in the ``docs/silica`` its manifest now names.
    Read-only I/O.
    """
    target = Path(arg).expanduser().resolve()
    if target.exists() and not target.is_dir():
        return VaultTarget("", False, f"not a directory: {target}")
    return VaultTarget(str(target), not target.is_dir())


class VaultSwitch(NamedTuple):
    """What a completed ``adopt_vault`` did, for whoever has to report it.

    ``error`` set means nothing happened at all. Everything else is a thing the
    caller may want to say out loud: ``created`` the directory did not exist,
    ``write_dir`` this call declared one in vault.yaml, ``invalid_write_dir``
    the manifest declares one Silica cannot resolve (every write will be
    rejected), ``repo_warning`` the vault⊂repo invariant is violated.
    """
    vault: str
    error: str | None = None
    created: bool = False
    write_dir: str | None = None
    seeded_ignore: bool = False
    invalid_write_dir: bool = False
    repo_warning: str = ""


def chosen_vault() -> str:
    """The vault a process started here curates, "" when nothing names one.

    The working directory wins. An *exported* SILICA_VAULT outranks it
    (`config.VAULT_PINNED`, the pin for headless runs like cron, which start
    wherever the scheduler put them); one in a .env file is ignored by
    config.load_user_env. Where cwd is not a vault ($HOME), the export is the
    fallback. Cross-project personal memory is the separate SILICA_MEMORY_VAULT
    axis (`kernel/recall/memory_lane.py`), which self-disables inside its own vault.
    """
    from silica.config import VAULT_PINNED

    return (None if VAULT_PINNED else resolve_cwd_vault(Path.cwd())) or CONFIG.vault_path.strip()


def adopt_vault(path: str | Path, *, writes: bool = True, own: bool = False) -> VaultSwitch:
    """Point the running process at a vault — the whole sequence, and the only way.

    Not an assignment: a live process holds a driver, an overlay cache, a
    manifest and vault-scoped index caches, and every one of them still answers
    for the old folder until it is reset. Skipping any single step leaves reads
    and writes disagreeing about which vault they are in.

    `writes=False` is a look and not an adoption, for the doctor and for a probe
    over a vault it does not own: nothing lands on disk, a folder that is not there
    stays unadopted (`created` says so), and vault.yaml's settings are left out of
    CONFIG. `own` is Silica's own fallback vault, which nobody pointed at: kept as
    spelled, since its index is keyed on the spelling, and nothing declared in it.

    Prints nothing, so a start, /vault and the settings panel can share it: the
    caller renders the returned record. Never raises for a bad path, that is
    ``error``.
    """
    from silica.driver import reset_driver
    from silica.kernel.base.roots import repo_root_warning
    from silica.kernel.recall.relatedness import reset_vault_caches
    from silica.kernel.text.overlay import reset_overlay_cache
    from silica.kernel.base.manifest import (
        apply_manifest_to_config,
        get_active_manifest,
        reset_manifest_cache,
    )

    target = VaultTarget(str(path), not Path(path).is_dir()) if own else resolve_vault_switch(str(path))
    if target.error:
        return VaultSwitch("", error=target.error)
    resolved = target.vault
    if target.created and not writes:
        return VaultSwitch("", created=True)
    if target.created:
        Path(resolved).mkdir(parents=True, exist_ok=True)
    declared = declare_write_dir(resolved) if writes and not own else None
    seeded = seed_silicaignore(resolved) if writes and not own else None
    CONFIG.vault_path = resolved
    reset_driver()
    reset_overlay_cache()  # overlay is vault-scoped; don't serve the old vault's
    reset_manifest_cache()  # manifest is vault-scoped too
    if writes:
        apply_manifest_to_config()
    # Vault-scoped store caches are path-keyed (harmless on lookup) but retain
    # the old vault's index/vectors for the process lifetime.
    reset_vault_caches()
    return VaultSwitch(
        vault=resolved,
        created=target.created,
        write_dir=declared,
        seeded_ignore=bool(seeded),
        # Declared but unresolvable (absolute/traversal). Refusing here is the
        # whole point of not degrading it to "" in the parser.
        invalid_write_dir=get_active_manifest().write_dir is None,
        repo_warning=repo_root_warning(resolved) or "",
    )
