# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What a folder is: an Obsidian vault, a git repository, and the code lane's root (ADR-0019)."""
from __future__ import annotations

import logging
from pathlib import Path


logger = logging.getLogger(__name__)


def is_obsidian_vault(path) -> bool:
    """True when `path` is an Obsidian vault (carries a `.obsidian/` dir).

    Never decides *which* folder is the vault (that is always the one you named
    or launched in), only whether writing notes into its root is at home there:
    an Obsidian vault writes in place, a source tree gets a `write_dir`
    (`onboarding.adopt`). Non-existent paths are False.
    """
    return (Path(path) / ".obsidian").is_dir()


def resolve_repo_root(vault: str | Path) -> tuple[Path | None, str | None]:
    """Code-lane repo root for `vault`, validating the vault⊂repo invariant (ADR-0019).

    Returns (root, warning). Valid layouts: a repo-mode vault (`<root>/docs/silica`
    or any plain dir — git discovers the target repo above it) and an Obsidian
    vault that is itself the repo root. An Obsidian vault nested inside a
    FOREIGN git repo yields (None, warning): code lane disabled, never grounded
    on the wrong repo. (None, None) when git is absent or no repo contains the
    vault. Pure resolution, no caching — see `repo_root_for`.
    """
    from silica.kernel.base import gitstate

    v = Path(vault).resolve()
    root = gitstate.find_repo_root(v)
    if root is None:
        return None, None
    if is_obsidian_vault(v) and root != v:
        return None, (
            f"code lane disabled: Obsidian vault {v} is nested inside "
            f"foreign git repo {root}"
        )
    return root, None


# Resolved-once storage for the code-lane root (ADR-0019), keyed by resolved
# vault path so it follows /vault switches and stays correct for entry points
# that never run the CLI startup (the GUI). "No repo" results are NOT cached,
# so a `git init` after first resolution is still picked up.
_REPO_ROOT_CACHE: dict[str, tuple[Path | None, str | None]] = {}


def _repo_root_resolved(vault: str | Path) -> tuple[Path | None, str | None]:
    raw = str(vault or "").strip()
    if not raw:
        return None, None
    key = str(Path(raw).resolve())
    hit = _REPO_ROOT_CACHE.get(key)
    if hit is not None:
        return hit
    root, warn = resolve_repo_root(raw)
    if warn:
        # A fixed template, the warning its argument: hosted logs keep the
        # template only (ADR-0100 point 2), and this one names two paths.
        logger.warning("repo root: %s", warn)
    if root is not None or warn is not None:
        _REPO_ROOT_CACHE[key] = (root, warn)
    return root, warn


def repo_root_for(vault: str | Path) -> Path | None:
    """The single choke point every code-lane consumer derives its repo root
    through (ADR-0019): resolved once per vault, invariant-validated, warning
    logged once. None ⇒ code lane disabled for this vault."""
    return _repo_root_resolved(vault)[0]


def repo_root_warning(vault: str | Path) -> str | None:
    """The invariant-violation message for `vault`, if any — for the CLI to
    surface loudly at startup and /vault switch."""
    return _repo_root_resolved(vault)[1]


def clear_repo_root_cache() -> None:
    """Drop cached repo-root resolutions (test isolation)."""
    _REPO_ROOT_CACHE.clear()
