# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""MOC (map-of-content) section helpers.

Shared by the FSM's HUB_UPDATE state (router/states/write.py) and the
deferred-retry recovery path (tools/pipeline.py): the retry path must give
recovered notes the same hub-MOC membership the FSM gives in-flight ones, and
tools cannot import router states without inverting the tools←router layering.
"""
from __future__ import annotations

import re


def moc_heading(source_name: str, sample: str) -> str:
    """Language-aware MOC section heading: '## Da: {name}' or '## From: {name}'.

    Routes through kernel/language (C1) — the private Italian marker regex
    this replaces missed prose outside its hardcoded word list.
    """
    from silica.kernel.language import detect
    prefix = "Da" if detect(sample) == "italian" else "From"
    return f"## {prefix}: {source_name}"


def merge_moc_section(content: str, heading: str, note_lines: list[str]) -> str:
    """Append note_lines to an existing MOC section or create a new one.

    When the same source file produces multiple chunks, each chunk calls
    HUB_UPDATE.  Rather than duplicating the heading, new links are appended
    inside the existing section.
    """
    if heading + "\n" in content or heading + "\r\n" in content:
        # Append new links just before the next same-level heading or end of file.
        pattern = re.compile(re.escape(heading) + r'(.*?)(?=\n##\s|\Z)', re.DOTALL)
        def _append(m: re.Match) -> str:
            return m.group(0).rstrip() + "\n" + "\n".join(note_lines) + "\n"
        return pattern.sub(_append, content, count=1)
    moc_block = f"\n{heading}\n\n" + "\n".join(note_lines) + "\n"
    return content.rstrip() + "\n" + moc_block


def hub_desc(snippet: str, cap: int = 120) -> str:
    """First real prose line of a body for a hub bullet — stripped of blockquote,
    callout ([!NOTE]), heading and list markers, and capped.

    Guards the MOC bullet from garbage like `> [!NOTE] Documento originale: ...`
    when the distiller opens a body with a fabricated callout (audit finding 3).
    """
    for raw in (snippet or "").splitlines():
        line = re.sub(r'^\s*>+\s*', '', raw.strip())          # blockquote
        line = re.sub(r'^\[![^\]]+\][-+]?\s*', '', line)      # callout tag
        line = re.sub(r'^#{1,6}\s*', '', line)                # heading
        line = re.sub(r'^[-*+]\s+', '', line)                 # list bullet
        line = line.strip().strip('*_`').strip()
        if line:
            return line[:cap].rstrip()
    return ""
