# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The prompts the kernel's own lanes send: the distiller's contract, its
profiles, and the anti-slop fragment every body-writing prompt ends with
(capabilities read that one here, downward)."""
from __future__ import annotations

from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    """Raises when the file is missing: a packaging miss must not reach the model
    as an empty instruction."""
    return (PROMPT_DIR / name).read_text(encoding="utf-8")
