# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Silica — Obsidian-native agentic CLI."""
from __future__ import annotations

import os as _os

# A SILICA_VAULT *exported* in the real environment (`SILICA_VAULT=x silica`, a
# cron unit) is a deliberate per-invocation pin: the one thing that outranks the
# working directory in cli._activate_repo_mode. The same name sitting in a .env
# file is config, not intent, and loses to cwd.
#
# Captured HERE, not in config.py, because the two become indistinguishable the
# moment anything calls load_dotenv — and litellm/__init__.py does exactly that
# at import time, before silica.config is reached. The package root is the
# earliest point silica controls: it runs before any submodule, hence before any
# third-party import. Re-exported as silica.config.VAULT_PINNED, its usual name.
VAULT_PINNED = bool(_os.environ.get("SILICA_VAULT", "").strip())

try:
    # Written by setuptools-scm at build/install time (gitignored).
    from ._version import version as __version__
except ImportError:  # pragma: no cover - source tree without a build
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version

        __version__ = _pkg_version("silica")
    except (ImportError, PackageNotFoundError):  # pragma: no cover
        __version__ = "0.0.0+unknown"
