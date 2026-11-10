# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/zotero: the linked Zotero library as a command (ADR-0055 "Chat and commands")."""
from __future__ import annotations

from silica.command_table import ROWS, Call, Result


def command(c: Call) -> Result:
    """/zotero [file|group|unlink|grant] [--apply|--yes]: the linked library (ADR-0055)."""
    run(list(c.args) or c.raw.strip().split()[1:], c.out)
    return c.out


def run(tokens: list[str], console) -> None:
    """/zotero [file|group|unlink|grant] [--apply|--yes]."""
    from silica.kernel.base.manifest import get_active_manifest
    from silica.sources import zotero, zotero_write

    verb = next((t for t in tokens if not t.startswith("--")), "")
    link = get_active_manifest().zotero
    if link is None:
        console.print("  This vault is not linked to Zotero: Settings › Zotero › Use in this vault.")
        return
    zot = zotero.client()
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
            inbox = zotero.collection_key(zot, link.inbox)
            names = {c["key"]: c["data"]["name"] for c in zot.paged("/collections")}
            rows = zotero_write.suggest_filing(zot, inbox)
            sure = [r for r in rows if r["votes"] == 3]
            if "--apply" in tokens:
                n, _ = zotero_write.file_in_a_run(zot, [(r["key"], r["first"]) for r in sure], inbox)
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
                n, _ = zotero_write.groups_in_a_run(zot, plan)
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
            console.print(f"  Usage: {ROWS['/zotero'].synopsis}")
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
