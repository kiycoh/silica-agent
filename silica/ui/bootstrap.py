# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The one start sequence, for every process that opens a vault.

The order is what matters, and it was a convention three call sites each kept:
the vault before anything that reads it, the manifest it declares before the budget
that follows the model, logging before the servers that report through it, the
servers last. A subcommand runs before `main()`'s setup, so each used to pick its
own subset of these lines; it names a kind here instead.
"""
from __future__ import annotations

from typing import Literal

from silica.config import CONFIG, default_vault
from silica.onboarding.adopt import adopt_vault, chosen_vault
from silica.onboarding.serve import ensure_local_servers
from silica.session import meter
from silica.term.console import CONSOLE
from silica.ui import logging as ui_logging


def open_vault() -> None:
    """Adopt the vault this process started in, or the stable home vault where
    nothing names one ($HOME with nothing configured), and say which."""
    target = chosen_vault()
    r = adopt_vault(target) if target else adopt_vault(default_vault(), own=True)
    if r.error:
        CONSOLE.print(f"  [red]{target} cannot be a vault — {r.error}[/]")
        return
    CONSOLE.print(f"  Vault: [bold]{r.vault}[/]")
    if r.write_dir:
        CONSOLE.print(f"  Writes confined to [bold]{r.write_dir}/[/] (`write_dir` in vault.yaml).")
    if r.seeded_ignore:
        CONSOLE.print("  Created [bold].silicaignore[/] — add folders to keep out of the index.")


def announce_code_lane() -> None:
    """Eager repo-root resolution (ADR-0019): validate the vault⊂repo invariant
    once at startup and surface a violation loudly."""
    from silica.kernel.base.roots import repo_root_warning

    warn = repo_root_warning(CONFIG.vault_path)
    if warn:
        CONSOLE.print(f"  [yellow]⚠ {warn}[/]")


def bootstrap(kind: Literal["session", "vault", "doctor"], *, debug: bool = False) -> None:
    """`session` is a process that stays: the REPL, the GUI, the bridge. `vault` needs
    the folder and nothing else (`silica import`). `doctor` looks and adopts nothing,
    its writes (vault.yaml, .silicaignore) included: left unset, every vault-scoped row
    read an empty store, and a vault with 22 notes embedded said "no retrieval index
    yet" (2026-10-02)."""
    if kind == "doctor":
        if target := chosen_vault():
            adopt_vault(target, writes=False)
        ensure_local_servers()
        return
    open_vault()
    if kind == "session":
        announce_code_lane()
        meter.resolve_context_budget()
        ui_logging.setup_logging(debug=debug)
        ensure_local_servers()
