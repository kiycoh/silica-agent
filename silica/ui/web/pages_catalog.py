# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Pages, the public host's index of listed vaults (ADR-0151 point 6).

The host runs this every two minutes over every account's published
manifests, as data: an account wrote each `vault.json`, so nothing in one is
trusted beyond its shape. The handle is the folder the host's map gives the
account, never the one a manifest claims, so an account cannot list a vault
under another's name. A manifest's `listed` is only its author's request: a
vault is listed when the host's own list, which root alone writes, names it
too (ADR-0153 point 7), and a list that is missing lists nothing.

    python -m silica.ui.web.pages_catalog --public /srv/silica/public \\
        --handles /etc/silica/handles.tsv --approved /etc/silica/wikis.tsv \\
        --out /srv/silica/pages
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from silica.ui.web import public_site as site

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
_HANDLE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,28}[a-z0-9])?$")
_ID = re.compile(r"^[a-z0-9-]{1,32}$")
# shortcut: a manifest past this size is skipped, not read; Cultura's is 60 KB at 654 notes, so
# reopen near 10,000 notes a vault
_MAX_MANIFEST = 2_000_000


def _text(v, n: int) -> str:
    return " ".join(v.split())[:n] if isinstance(v, str) else ""


def _int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10**7 else 0


def entry(raw: dict, handle: str, slug: str) -> dict | None:
    """The card's fields from one manifest, None unless it asks to be listed."""
    if not isinstance(raw, dict) or raw.get("listed") is not True:
        return None
    count = c if isinstance(c := raw.get("count"), dict) else {}
    areas = [[_text(a[0], 80), _int(a[1])] for a in raw.get("areas", [])[:200]
             if isinstance(a, list) and len(a) == 2 and _text(a[0], 80)] if isinstance(raw.get("areas"), list) else []
    published = p if isinstance(p := raw.get("published"), str) else ""
    return {
        "handle": handle, "slug": slug,
        "title": _text(raw.get("title"), 80) or slug,
        "line": _text(raw.get("line"), 160),
        "published": published[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", published) else "",
        "count": {"notes": _int(count.get("notes")), "areas": len(areas)},
        "areas": areas,
    }


def handles(path: Path) -> list[tuple[str, str]]:
    """(handle, account id) from the host's map, the one caddy imports too."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and _HANDLE.match(parts[0]) and _ID.match(parts[1]):
            out.append((parts[0], parts[1]))
    return out


def approved(path: Path) -> set[tuple[str, str]]:
    """(account id, slug) from the host's list, bin/pages' to write. By id, so a handle that
    changes hands does not carry a review with it."""
    if not path.is_file():
        return set()
    rows = (line.split("\t") for line in path.read_text(encoding="utf-8").splitlines())
    return {(r[0], r[1]) for r in rows if len(r) >= 2 and _ID.match(r[0]) and _SLUG.match(r[1])}


def collect(public: Path, pairs: list[tuple[str, str]], ok: set[tuple[str, str]]) -> list[dict]:
    out = []
    for h, acct in pairs:
        live = public / acct / h / "live"
        if not live.is_dir():
            continue
        for d in sorted(live.iterdir()):
            f = d / "vault.json"
            if not ((acct, d.name) in ok and _SLUG.match(d.name) and d.is_dir() and not d.is_symlink()
                    and f.is_file() and not f.is_symlink()):
                continue
            try:
                if f.stat().st_size > _MAX_MANIFEST:
                    continue
                e = entry(json.loads(f.read_text(encoding="utf-8")), h, d.name)
            except (OSError, ValueError):
                continue
            if e:
                out.append(e)
    out.sort(key=lambda e: (e["published"], e["handle"], e["slug"]), reverse=True)
    return out


def render(entries: list[dict], app: str) -> str:
    cards = "".join(site.card(e, f"/@{e['handle']}/{e['slug']}/", f"/@{e['handle']}/{e['slug']}/vault.json")
                    for e in entries)
    notes = sum(e["count"]["notes"] for e in entries)
    head = ('<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<title>Pages · Silica</title>\n'
            '<meta name="description" content="Notes people chose to publish from their Silica vaults.">\n'
            '<link rel="stylesheet" href="/_s/pages.css">\n<script src="/_s/pages.js" defer></script>\n'
            '<meta property="og:title" content="Pages · Silica">\n<meta property="og:site_name" content="Silica">\n'
            '</head>\n')
    tools = ('<div class="cat-tools" hidden><label class="search"><svg viewBox="0 0 24 24" aria-hidden="true">'
             '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg><input type="search" '
             'placeholder="Search titles, areas and authors" autocomplete="off" aria-label="Search wikis"></label>'
             '<span class="seg" role="group" aria-label="Sort" data-sort><button type="button" data-v="new" '
             'aria-pressed="true">Newest</button><button type="button" data-v="size" aria-pressed="false">Most notes'
             '</button><button type="button" data-v="az" aria-pressed="false">A to Z</button></span>'
             f'<span class="cat-count num" data-count>{len(entries)} {"wiki" if len(entries) == 1 else "wikis"} · '
             f'{site._fmt(notes)} notes</span></div>'
             '<div class="ar-row" role="group" aria-label="Areas" data-areas-row hidden></div>')
    empty = "" if entries else "Nothing is listed yet."
    return (head + "<body>\n" + site.top("/", app, current="pages")
            + '<main class="sheet"><section class="wrap cat" aria-labelledby="cat-h" data-catalog>'
            '<div class="cat-head grid"><h1 id="cat-h">Pages</h1><p>Notes people chose to publish from their Silica '
            'vaults. Read one here, or save a copy into your own vault: every note in the copy keeps its author\'s '
            'name.</p></div>' + tools
            + f'<div class="cat-grid">{cards}</div><p class="cat-empty" data-empty{"" if not entries else " hidden"}>'
            f'{empty}</p>'
            '<div class="cat-join"><span><b>List your own.</b> In Silica, open My Wikis, set a wiki to Public and ask to '
            'list it. It is reviewed first.</span><span>Something here should not be? Open the wiki and press Report.'
            '</span></div>'
            '</section></main>\n'
            + site.foot("Every wiki here is a copy its author chose to publish, served as static files.", app))


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o644)
    os.replace(tmp, path)


def write(public: Path, handles_tsv: Path, wikis_tsv: Path, out: Path, app: str) -> int:
    """Write the index and the static files the public host serves; returns the vaults listed."""
    entries = collect(public, handles(handles_tsv), approved(wikis_tsv)) if handles_tsv.is_file() else []
    out.mkdir(parents=True, exist_ok=True)
    (out / "_s").mkdir(exist_ok=True)
    for name, data in site.static_files().items():
        _write(out / "_s" / name, data)
    _write(out / "index.html", render(entries, app).encode("utf-8"))
    return len(entries)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--public", type=Path, required=True)
    ap.add_argument("--handles", type=Path, required=True)
    ap.add_argument("--approved", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--app", default=os.environ.get("SILICA_PUBLIC_SAVE_URL", "") or "https://app.silica-agent.dev")
    a = ap.parse_args(argv)
    n = write(a.public, a.handles, a.approved, a.out, a.app.rstrip("/"))
    print(f"pages: {n} listed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
