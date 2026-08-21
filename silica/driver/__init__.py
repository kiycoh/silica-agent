# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Obsidian Driver package — exposes the global DRIVER instance.

Two backends, one of which is never built from config:
  - ObsidianFSBackend — direct filesystem access, headless, no Obsidian
    required. Built here, always.
  - ObsidianWSBackend — installed live by `silica connect` via set_driver()
    when the Obsidian plugin dials in.

Usage:
    from silica.driver import DRIVER
    content = DRIVER.read_note("Computer Vision")
"""
from __future__ import annotations

import logging
import threading

from silica.driver.base import (  # noqa: F401 — re-export domain types
    GraphSnapshot,
    Heading,
    Hit,
    Link,
    NoteContent,
    NoteRef,
    ObsidianDriver,
    Txn,
)

logger = logging.getLogger(__name__)


def _create_driver() -> ObsidianDriver:
    """Build the filesystem backend against the configured vault."""
    from silica.config import CONFIG
    from silica.driver.fs_backend import ObsidianFSBackend

    return ObsidianFSBackend(vault_path=CONFIG.vault_path)


# Lazy initialization — created on first access, protected by lock for thread safety
_driver: ObsidianDriver | None = None
_driver_lock = threading.Lock()


def get_driver() -> ObsidianDriver:
    """Get the global driver instance (lazy-initialized, thread-safe)."""
    global _driver
    if _driver is None:
        with _driver_lock:
            # Double-checked locking: recheck after acquiring lock
            if _driver is None:
                _driver = _create_driver()
    return _driver


def driver_kind() -> str:
    """"ws" while `silica connect` has a plugin attached, else "fs".

    Reads the cached instance rather than CONFIG so it reports what is actually
    serving reads, and never forces driver construction just to answer.
    """
    return "ws" if type(_driver).__name__ == "ObsidianWSBackend" else "fs"


def reset_driver() -> None:
    """Drop the cached driver so the next get_driver() rebuilds against the
    current CONFIG.vault_path. Used by the runtime /vault switch."""
    global _driver
    with _driver_lock:
        _driver = None


def set_driver(driver: ObsidianDriver | None) -> None:
    """Install a live driver instance — `silica connect`'s attached ws backend.
    None falls back to building the fs backend on next access."""
    global _driver
    with _driver_lock:
        _driver = driver


# For convenience: DRIVER can be imported directly
# But since it's lazy, access via get_driver() in hot paths
class _DriverProxy:
    """Proxy that lazy-initializes the driver on first attribute access."""

    def __getattr__(self, name: str):
        return getattr(get_driver(), name)


DRIVER = _DriverProxy()
