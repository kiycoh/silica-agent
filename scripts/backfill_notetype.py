#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""One-shot backfill of the OKF `type` field on legacy notes.

New notes get their `type` at the driver seam; notes written before that seam
existed do not. This closes the gap once, in place, reusing the same
`derive_type` the seam uses — so a backfilled note and a freshly written one
carry the same value.

    uv run python scripts/backfill_notetype.py [VAULT] [--dry-run]

VAULT defaults to SILICA_VAULT. Notes that already declare a `type`, notes with
no frontmatter, and notes with broken YAML are left untouched (`silica doctor`
censuses those). This is an event, not a mode: there is deliberately no
permanent `--fix` flag anywhere in the product.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from silica.kernel.recall.paths import ignore_matcher  # noqa: E402
from silica.kernel.write.notetype import stamp_type    # noqa: E402


def backfill(vault: Path, dry_run: bool = False) -> Counter:
    counts: Counter = Counter()
    ignored = ignore_matcher(vault)
    for f in sorted(vault.rglob("*.md")):
        parts = f.relative_to(vault).parts
        if any(p.startswith(".") for p in parts) or any(ignored(p) for p in parts[:-1]):
            continue
        rel = f.relative_to(vault).as_posix()
        try:
            content = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            counts["unreadable"] += 1
            continue
        stamped = stamp_type(rel, content)
        if stamped == content:
            counts["skipped"] += 1
            continue
        counts["stamped"] += 1
        if not dry_run:
            f.write_text(stamped, encoding="utf-8")
    return counts


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("-")]
    dry_run = "--dry-run" in argv
    vault = Path(args[0] if args else os.environ.get("SILICA_VAULT", "")).expanduser()
    if not str(vault) or not vault.is_dir():
        print("usage: backfill_notetype.py [VAULT] [--dry-run]  (or set SILICA_VAULT)")
        return 2
    counts = backfill(vault, dry_run)
    verb = "would stamp" if dry_run else "stamped"
    print(f"{vault}: {verb} {counts['stamped']}, left alone {counts['skipped']}"
          + (f", unreadable {counts['unreadable']}" if counts["unreadable"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
