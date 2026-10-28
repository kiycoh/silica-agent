# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Rebuild Discover's catalog snapshot from Kagi News's feed list (ADR-0137 point 3).

Run by hand at a release, never at run time (decision 5):

    curl -sO https://raw.githubusercontent.com/kagisearch/kite-public/main/kite_feeds.json
    uv run python scripts/discover_catalog.py kite_feeds.json <commit sha>

Keeps each category's name, kind, language and feed URLs, and drops the
twenty-language display names: 518 KB of JSON against 725 KB measured
2026-10-28. The MIT notice ships beside it in LICENSE-kite-feeds.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "silica" / "sources" / "discover" / "catalog.json"


def trim(source: dict) -> list[dict]:
    rows = []
    for name, cat in source.items():
        feeds = [u for u in cat.get("feeds") or [] if isinstance(u, str) and u.startswith(("http://", "https://"))]
        if feeds:
            rows.append({"name": name, "kind": cat.get("category_type") or "topic",
                         "lang": cat.get("source_language") or "", "feeds": feeds})
    return sorted(rows, key=lambda r: (r["kind"] != "topic", r["name"].lower()))


def main(path: str, commit: str) -> None:
    rows = trim(json.loads(Path(path).read_text(encoding="utf-8")))
    doc = {"source": "https://github.com/kagisearch/kite-public/blob/" + commit + "/kite_feeds.json",
           "licence": "MIT, Copyright (c) 2024 Kagi Search: LICENSE-kite-feeds",
           "taken": date.today().isoformat(), "categories": rows}
    OUT.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{len(rows)} categories, {sum(len(r['feeds']) for r in rows)} feeds, "
          f"{OUT.stat().st_size // 1000} KB -> {OUT}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
