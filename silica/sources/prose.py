# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Prose source adapter — markdown/.txt notes, the Obsidian-native source.

Distill lane: the Injector FSM owns destination and write; this adapter
only classifies, reads (sanitized, zero-trust frontier per ADR-0009) and
declares the lane. read() is total for a file in the vault: an unreadable one
yields an empty RawItem rather than raising, because the FSM re-reads through
the driver anyway and dispatch must not block a batch on one bad path. A path
outside the vault raises ValueError, which `registry.stage` reports and the
batch goes on (ADR-0179 point 4).
"""
from __future__ import annotations

from pathlib import Path

from silica.config import CONFIG
from silica.kernel.base.formats import PROSE_EXTS
from silica.kernel.text.sanitize import strip_degenerate_runs
from silica.sources.base import GroundedStub, RawItem


class ProseAdapter:
    name = "prose"

    def matches(self, target: str) -> bool:
        return target.lower().endswith(PROSE_EXTS)

    def read(self, target: str) -> RawItem:
        from silica.kernel.base.layout import readable_in_vault

        vault = (CONFIG.vault_path or "").strip()
        try:
            p = readable_in_vault(target, vault or Path.cwd())
        except ValueError as e:
            raise ValueError(f"it is {e}: put it in the Inbox, then nucleate it from there") from None
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return RawItem(target=target, text="")
        return RawItem(target=target, text=strip_degenerate_runs(text))

    def to_stub(self, item: RawItem) -> GroundedStub:
        return GroundedStub(lane="distill", body=item.text)


PROSE = ProseAdapter()
