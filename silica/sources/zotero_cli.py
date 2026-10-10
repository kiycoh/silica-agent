# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/zotero: the linked Zotero library from the REPL (ADR-0055 "Chat and commands").

Out of cli.py because it speaks API paths, and cli.py's string literals that
start with a slash are read as slash commands by the registry parity test.
"""
from __future__ import annotations


def _collection_key(zot, value: str) -> str:
    """`zotero.inbox` holds a collection's name or its key; names are what a
    person writes in vault.yaml, keys are what the API takes."""
    try:
        cols = zot.paged("/collections")
    except Exception:
        return value  # an unreadable list leaves the value as the key it may be
    if any(c.get("key") == value for c in cols):
        return value
    return next((c["key"] for c in cols if c.get("data", {}).get("name") == value), value)


def run(tokens: list[str], console) -> None:
    """/zotero [file|group|unlink|grant] [--apply|--yes]."""
    from silica.config import CONFIG
    from silica.kernel.vault_manifest import get_active_manifest
    from silica.kernel.write.undo_journal import get_undo_journal
    from silica.sources import zotero, zotero_write

    verb = next((t for t in tokens if not t.startswith("--")), "")
    link = get_active_manifest().zotero
    if link is None:
        console.print("  This vault is not linked to Zotero: Settings › Zotero › Use in this vault.")
        return
    zot = zotero.client()
    vault = CONFIG.vault_path.strip() or None
    try:
        if verb == "":
            _status(zot, console)
        elif verb == "grant":
            kept = zotero_write.authorize(zot)
            console.print("  Write access granted; the key is kept in your user .env, outside the vault."
                          if kept else
                          "  Allowed once: the key is not kept. Silica asks again before the next write.")
        elif verb == "file":
            if not link.inbox:
                console.print("  No inbox collection set: Settings › Zotero › Inbox collection.")
                return
            inbox = _collection_key(zot, link.inbox)
            names = {c["key"]: c["data"]["name"] for c in zot.paged("/collections")}
            rows = zotero_write.suggest_filing(zot, inbox)
            sure = [r for r in rows if r["votes"] == 3]
            if "--apply" in tokens:
                run = get_undo_journal().start_run(source="zotero", vault=vault)
                n = zotero_write.file_items(zot, [(r["key"], r["first"]) for r in sure],
                                            inbox, run_id=run)
                console.print(f"  Moved {n} item(s) out of {link.inbox} · recorded in "
                              "zotero.jsonl · undo: /revert")
                return
            console.print(f"  {link.inbox} · {len(rows)} item(s) · the nearest 3 filed papers vote "
                          f"· {len(sure)} agree 3 of 3")
            for r in rows:
                console.print(f"    {r['sur']} {r['year']}  {r['title'][:48]}  "
                              f"→ {names.get(r['first'], r['first'])}  {r['votes']}/3  "
                              f"or {names.get(r['second'], r['second'])}")
            console.print(f"  [dim]--apply files the {len(sure)} that agree 3 of 3; file the rest "
                          "from Explore › Library[/]")
        elif verb == "group":
            plan = zotero_write.compute_group_plan()
            for label, keys in plan.items():
                console.print(f"    {label}  {len(keys)} paper(s)")
            if "--apply" in tokens:
                run = get_undo_journal().start_run(source="zotero", vault=vault)
                n = zotero_write.write_groups(zot, plan, run_id=run)
                console.print(f"  Wrote {len(plan)} group(s) under Silica groups, {n} membership "
                              "change(s) · undo: /revert")
            else:
                console.print("  [dim]--apply writes them under Silica groups[/]")
        elif verb == "unlink":
            if "--yes" not in tokens:
                n = sum(1 for i in zot.paged("/items", tag=zotero_write.TAG)
                        if i["data"].get("itemType") == "note")
                console.print(f"  This removes from Zotero {n} note(s) tagged silica and the Silica "
                              "groups collection. Items are never touched. Run /zotero unlink --yes.")
                return
            res = zotero_write.unlink(zot)
            console.print(f"  Removed {res['notes']} note(s) and {res['collections']} collection(s) "
                          "from Zotero.")
        else:
            console.print("  Usage: /zotero [file|group|unlink|grant] [--apply|--yes]")
    except zotero.ZoteroError as exc:
        console.print(f"  [yellow]Zotero: {exc}[/]")


def _status(zot, console) -> None:
    r = zot.send("GET", "/api/")
    version = r.headers.get("X-Zotero-Version", "")
    access = "write access granted" if zot.api_key else "read only until write access is granted"
    console.print(f"  Zotero {version} · library {zot.server_id} · {access}")
    from silica.config import CONFIG
    from silica.sources import zotero

    found = zotero.detect(CONFIG.vault_path) or {"new": [], "changed": []}
    console.print(f"  {len(found['new'])} new since the last session · "
                  f"{len(found['changed'])} PDF(s) changed under vault papers")
    for key in found["new"] + found["changed"]:
        tag = "new" if key in found["new"] else "changed"
        data = zot.item(key).get("data", {})
        creators = data.get("creators") or [{}]
        console.print(f"    {tag:8} {creators[0].get('lastName', '')}  {data.get('title', '')[:70]}")
    if found["new"]:
        console.print("  [dim]bring them in: Explore › Library, or /nucleate @<citekey>[/]")
