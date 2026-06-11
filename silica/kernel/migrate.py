"""migrate — one-shot vault namespace migrations.

migrate_adr_namespace moves docs/adr/ → docs/silica/adr/. Vault-relative
wikilinks ([[adr/...]]) survive because the path stays `adr/` relative to the
new vault root; only the root moved. A tombstone is left for external refs.
Pure filesystem (no git): works whether or not the files are git-tracked, and
never auto-commits (the developer commits the move).

Only `*.md` files are migrated; any non-markdown files in docs/adr/ (images,
attachments) are left in place. A file is never overwritten: if its
destination already exists (e.g. a partial re-run), it is skipped, not moved.
"""
from __future__ import annotations

import shutil
from pathlib import Path


def migrate_adr_namespace(repo_root: Path | str) -> list[str]:
    """Move docs/adr/*.md → docs/silica/adr/. Returns moved vault-relative
    paths (under the new root). No-op (returns []) when docs/adr/ is absent."""
    root = Path(repo_root)
    old = root / "docs" / "adr"
    if not old.is_dir():
        return []
    new = root / "docs" / "silica" / "adr"
    new.mkdir(parents=True, exist_ok=True)

    moved: list[str] = []
    for md in sorted(old.glob("*.md")):
        dest = new / md.name
        if dest.exists():
            # Destination already present (e.g. a partial re-run); never
            # overwrite — leave the source as a signal and skip it.
            continue
        shutil.move(str(md), str(dest))
        moved.append(f"adr/{md.name}")

    (old / "MOVED.md").write_text(
        "# Moved\n\nADRs now live in `docs/silica/adr/` (the Silica vault root).\n",
        encoding="utf-8",
    )
    return moved
