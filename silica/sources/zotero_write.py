# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What Silica writes into Zotero, and how it takes it back (ADR-0055 decisions 7 and 8).

Three kinds of write, each owned or confirmed: a `silica`-tagged child note
per imported paper, the `Silica groups` collection, and moves out of the
inbox collection the user confirmed. Every write reads its object first and
sends that version, and every write is a line in `<write_dir>/zotero.jsonl`,
because the undo journal holds vault files only (`InverseOpKind` goes through
DRIVER) and filing touches the user's own curated tree.

Revert checks state, not versions: Silica's own later writes bump an item's
version too, so "still at the version Silica left" would refuse to undo a
filing after a groups write. A filed item moves back while it still sits
where Silica put it; a note is deleted while nobody edited it.
"""
from __future__ import annotations

import html
import json
import logging
import os
import time
from collections import Counter
from pathlib import Path
from urllib.parse import quote

import httpx

from silica.sources.zotero import Zotero, ZoteroError, _is_paper

logger = logging.getLogger(__name__)

TAG = "silica"
GROUPS_NAME = "Silica groups"
_WEB_PORT = 8765  # silica.ui.web.server.serve's default


# --- ledger -------------------------------------------------------------------

def ledger_path() -> Path:
    from silica.config import CONFIG
    from silica.kernel.vault_manifest import in_write_dir

    return Path(CONFIG.vault_path or ".") / in_write_dir("zotero.jsonl")


def _record(run_id: str, action: str, key: str, **extra) -> None:
    line = {"run": run_id, "action": action, "key": key, "at": time.time(), **extra}
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def _lines() -> list[dict]:
    p = ledger_path()
    if not p.is_file():
        return []
    out = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue  # a torn last line from a crash mid-append: the rest still undo
    return out


def _reverted(lines: list[dict]) -> set[str]:
    return {line["run"] for line in lines if line.get("action") == "reverted"}


def last_run() -> str | None:
    """The newest run with Zotero writes not yet reverted."""
    lines = _lines()
    done = _reverted(lines)
    live = [line for line in lines if line.get("run") not in done and line.get("action") != "reverted"]
    return live[-1]["run"] if live else None


def has_run(run_id: str) -> bool:
    return any(line.get("run") == run_id for line in _lines())


def run_started(run_id: str) -> float:
    return next((line["at"] for line in _lines() if line.get("run") == run_id), 0.0)


def _last_version(key: str) -> int | None:
    versions = [line.get("after") for line in _lines()
                if line.get("key") == key and line.get("after") is not None]
    return versions[-1] if versions else None


# --- writes -------------------------------------------------------------------

def _write(zot: Zotero, method: str, path: str, *, body=None, version: int | None = None
           ) -> httpx.Response:
    if not zot.api_key:
        raise ZoteroError("Zotero write access is not granted: Settings › Zotero › Grant, "
                          "or /zotero grant")
    headers = {"Zotero-API-Key": zot.api_key,
               "Zotero-Server-ID": zot.server_id or zot.library_id()}
    if version is not None:
        headers["If-Unmodified-Since-Version"] = str(version)
    r = zot.send(method, f"/api/users/0{path}", json=body, headers=headers)
    if r.status_code >= 300:
        why = {403: "write access was revoked in Zotero", 412: "changed in Zotero this instant",
               428: "Zotero wants its server id"}.get(r.status_code, f"HTTP {r.status_code}")
        raise ZoteroError(f"Zotero refused {method} {path}: {why}")
    return r


def _created(r: httpx.Response) -> tuple[str, int]:
    ok = r.json().get("successful", {}).get("0", {})
    return ok.get("key", ""), int(ok.get("version") or 0)


def _version_after(r: httpx.Response, fallback: int) -> int:
    return int(r.headers.get("Last-Modified-Version") or fallback)


def _set_collections(zot: Zotero, item: dict, cols: list[str]) -> int:
    r = _write(zot, "PATCH", f"/items/{item['key']}", body={"collections": cols},
               version=item["version"])
    return _version_after(r, item["version"] + 1)


# --- back-link notes ------------------------------------------------------------

def _web_url(leaf_rel: str) -> str:
    """The web UI's link, only while it is up: a dead link in Zotero is worse
    than none."""
    try:
        httpx.get(f"http://127.0.0.1:{_WEB_PORT}/", timeout=0.2)
    except httpx.HTTPError:
        return ""
    return f"http://127.0.0.1:{_WEB_PORT}/#explore/graph?note={quote(leaf_rel)}"


def notes_citing(leaf_rel: str) -> list[str]:
    """Names of the vault notes whose `## Sources` link the leaf."""
    from silica.driver import DRIVER
    from silica.kernel.recall.paths import is_source_leaf

    try:
        refs = DRIVER.backlinks(leaf_rel)
    except Exception:
        return []  # an unindexed vault lists nothing rather than failing the import
    return sorted({r.name for r in refs if not is_source_leaf(r.path)}, key=str.casefold)


def _note_html(title: str, notes: list[str], leaf_rel: str) -> str:
    from silica.config import CONFIG

    e = html.escape
    vault = Path(CONFIG.vault_path or ".").name
    obsidian = f"obsidian://open?vault={quote(vault)}&file={quote(leaf_rel, safe='')}"
    links = f'<a href="{e(obsidian)}">Open in Obsidian</a>'
    web = _web_url(leaf_rel)
    if web:
        links += f' · <a href="{e(web)}">Open in Silica</a>'
    items = "".join(f"<li>{e(n)}</li>" for n in notes)
    return (f"<h1>Silica · {e(title)}</h1>\n<p>Notes Silica made from this paper:</p>\n"
            f"<ul>{items}</ul>\n<p>{links}</p>\n"
            "<p>Written by Silica. Edit it freely: Silica stops instead of overwriting "
            "a note you changed.</p>")


def backlink(zot: Zotero, item_key: str, leaf_rel: str, *, run_id: str) -> str:
    """Write or refresh the paper's `Silica · <title>` child note.

    Returns created, updated, unchanged, or edited when the user changed the
    note since Silica's last write and it was left alone.
    """
    item = zot.item(item_key)
    title = item.get("data", {}).get("title", "")
    body = _note_html(title, notes_citing(leaf_rel), leaf_rel)
    mine = [c for c in zot.paged(f"/items/{item_key}/children")
            if c.get("data", {}).get("itemType") == "note"
            and TAG in [t.get("tag") for t in c["data"].get("tags", [])]]
    if not mine:
        r = _write(zot, "POST", "/items", body=[{
            "itemType": "note", "parentItem": item_key, "note": body,
            "tags": [{"tag": TAG}]}])
        key, version = _created(r)
        _record(run_id, "create_note", key, after=version, parent=item_key)
        return "created"
    note = mine[0]
    if note["version"] != _last_version(note["key"]):
        return "edited"
    if note["data"].get("note") == body:
        return "unchanged"
    r = _write(zot, "PATCH", f"/items/{note['key']}", body={"note": body}, version=note["version"])
    _record(run_id, "update_note", note["key"], after=_version_after(r, note["version"] + 1),
            prior=note["data"].get("note", ""))
    return "updated"


# --- filing -----------------------------------------------------------------------

def file_items(zot: Zotero, moves: list[tuple[str, str]], inbox: str, *, run_id: str) -> int:
    """Move each (item, collection) out of the inbox collection. An item that
    already left the inbox is skipped: the user filed it first."""
    done = 0
    for key, target in moves:
        item = zot.item(key)
        cols = list(item.get("data", {}).get("collections", []))
        if inbox not in cols:
            continue
        new = [c for c in cols if c != inbox] + ([target] if target not in cols else [])
        after = _set_collections(zot, item, new)
        _record(run_id, "move", key, after=after, before=cols, target=target, inbox=inbox)
        done += 1
    return done


def _vectors(items: list[dict], embed) -> dict[str, list[float]]:
    """Title + abstract vectors, cached by item key and text: a library is
    embedded once, and only new or edited items cost an embedding call."""
    import hashlib

    from silica.kernel.recall.paths import index_dir

    cache_path = index_dir() / "zotero_vectors.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    texts = {i["key"]: f"{i['data'].get('title', '')}\n{i['data'].get('abstractNote', '')}"
             for i in items}
    sig = {k: hashlib.sha1(t.encode("utf-8")).hexdigest()[:12] for k, t in texts.items()}
    todo = [k for k in texts if cache.get(k, {}).get("sig") != sig[k]]
    if todo:
        for k, vec in zip(todo, embed([texts[k] for k in todo])):
            cache[k] = {"sig": sig[k], "vec": list(map(float, vec))}
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass  # an unwritable index costs a re-embed next time, not the answer
    return {k: cache[k]["vec"] for k in texts}


def _default_embed(texts: list[str]) -> list[list[float]]:
    from silica.agent.providers import get_embedder
    from silica.config import CONFIG

    return get_embedder(CONFIG).embed(texts)


def suggest_filing(zot: Zotero, inbox: str, *, embed=None, k: int = 3) -> list[dict]:
    """For each inbox item: the vote of its k nearest filed items, by title and
    abstract. Measured 2026-10-05 on 249 filed items held out one at a time:
    the first choice is the user's collection 76% of the time, the top two 88%.
    """
    import numpy as np

    papers = [i for i in zot.paged("/items/top") if _is_paper(i)]
    filed = [i for i in papers if len(i["data"].get("collections", [])) == 1
             and inbox not in i["data"]["collections"]]
    queue = [i for i in papers if inbox in i["data"].get("collections", [])]
    if not filed or not queue:
        return []
    vecs = _vectors(filed + queue, embed or _default_embed)

    def unit(key: str):
        v = np.asarray(vecs[key], dtype=float)
        n = np.linalg.norm(v)
        return v / n if n else v

    F = np.stack([unit(i["key"]) for i in filed])
    out = []
    for item in queue:
        order = np.argsort(-(F @ unit(item["key"])))
        cols = [filed[j]["data"]["collections"][0] for j in order]
        first, votes = Counter(cols[:k]).most_common(1)[0]
        second = next((c for c in cols if c != first), "")
        data = item["data"]
        creators = data.get("creators") or [{}]
        out.append({"key": item["key"], "title": data.get("title", ""),
                    "sur": creators[0].get("lastName") or creators[0].get("name", ""),
                    "year": (item.get("meta", {}).get("parsedDate") or "")[:4],
                    "first": first, "votes": int(votes), "second": second})
    return out


# --- groups -------------------------------------------------------------------------

def group_plan(nodes: list[dict], edges: list[dict], zones: dict[int, str]) -> dict[str, list[str]]:
    """{zone label: [zotero keys]}: a paper joins the zone most of its concept
    notes hold. Leaves have no vector, so a paper has no zone of its own."""
    zone_of = {n["id"]: n.get("sgroup", -1) for n in nodes}
    plan: dict[str, list[str]] = {}
    for n in nodes:
        if n.get("type") != "paper":
            continue
        held = Counter(zone_of.get(e["from"], -1) for e in edges
                       if e.get("type") == "EXTRACTED" and e.get("to") == n["id"])
        held.pop(-1, None)
        held = Counter({z: c for z, c in held.items() if z in zones})
        if not held:
            continue
        top = max(held.values())
        zone = min(z for z, c in held.items() if c == top)
        plan.setdefault(zones[zone], []).append(n["paper"]["zotero_key"])
    return {label: sorted(keys) for label, keys in sorted(plan.items())}


def compute_group_plan() -> dict[str, list[str]]:
    """The plan for this vault now: graph, zones, papers."""
    from silica.kernel.recall.citations import paper_layer
    from silica.kernel.recall.graph_export import (
        build_graph_data,
        detect_semantic_partition,
        knn_edges,
    )

    nodes, edges = build_graph_data(folder="")
    paper_layer(nodes)
    zones = detect_semantic_partition(nodes, knn_edges(nodes, k=6))
    return group_plan(nodes, edges, {z.id: z.label for z in zones})


def _groups_root(zot: Zotero, run_id: str) -> str:
    from silica.config import CONFIG
    from silica.kernel.vault_manifest import get_active_manifest, set_zotero

    link = get_active_manifest().zotero
    # A vault with no link used to create "Silica groups" in whatever library
    # answered and write a `zotero:` block holding only `groups` (ADR-0066
    # context, fact 1): groups are the linked vault's zones written back.
    if link is None:
        raise ZoteroError("this vault is not linked to a Zotero library: groups are written "
                          "from the linked vault")
    key = link.groups
    if key:
        try:
            zot.get(f"/collections/{key}")
            return key
        except ZoteroError:
            pass  # deleted in Zotero: make a new one rather than fail the write
    r = _write(zot, "POST", "/collections", body=[{"name": GROUPS_NAME, "parentCollection": False}])
    key, version = _created(r)
    _record(run_id, "create_collection", key, after=version)
    set_zotero(CONFIG.vault_path, "groups", key)
    return key


def write_groups(zot: Zotero, plan: dict[str, list[str]], *, run_id: str) -> int:
    """Make `Silica groups` match the plan. Only Silica's own subcollections
    are touched; every collection of the user's on an item stays.

    ponytail: a zone that disappears leaves its subcollection empty rather than
    deleted, because a deletion would need a recreate-with-new-key inverse.
    Delete empty ones at 10 such leftovers per library.
    """
    root = _groups_root(zot, run_id)
    subs = {c["data"]["name"]: c["key"] for c in zot.paged(f"/collections/{root}/collections")}
    for label in plan:
        if label not in subs:
            r = _write(zot, "POST", "/collections", body=[{"name": label, "parentCollection": root}])
            subs[label], version = _created(r)
            _record(run_id, "create_collection", subs[label], after=version)
    owned = set(subs.values())
    want = {key: subs[label] for label, keys in plan.items() for key in keys}
    members = {i["key"] for sub in owned for i in zot.paged(f"/collections/{sub}/items/top")}
    changed = 0
    for key in sorted(set(want) | members):
        item = zot.item(key)
        cols = list(item.get("data", {}).get("collections", []))
        new = [c for c in cols if c not in owned] + ([want[key]] if key in want else [])
        if new == cols:
            continue
        after = _set_collections(zot, item, new)
        _record(run_id, "group", key, after=after, before=cols, owned=sorted(owned))
        changed += 1
    return changed


# --- revert, unlink, authorize -----------------------------------------------------

def revert(zot: Zotero, run_id: str) -> dict:
    """Undo a run's Zotero writes, newest first, by state."""
    lines = _lines()
    if run_id in _reverted(lines):
        return {"undone": 0, "left": []}
    undone, left = 0, []
    for line in reversed([x for x in lines if x.get("run") == run_id and x["action"] != "reverted"]):
        key, action = line["key"], line["action"]
        try:
            if action in ("create_note", "update_note"):
                note = zot.item(key)
                if note["version"] != line["after"]:
                    left.append((key, "edited in Zotero since"))
                    continue
                if action == "create_note":
                    _write(zot, "DELETE", f"/items/{key}", version=note["version"])
                else:
                    _write(zot, "PATCH", f"/items/{key}", body={"note": line["prior"]},
                           version=note["version"])
            elif action == "move":
                item = zot.item(key)
                cols = item["data"].get("collections", [])
                if line["target"] not in cols or line["inbox"] in cols:
                    left.append((key, "moved in Zotero since"))
                    continue
                back = [c for c in cols if c != line["target"]]
                back += [c for c in line["before"] if c not in back]
                _set_collections(zot, item, back)
            elif action == "group":
                item = zot.item(key)
                cols = item["data"].get("collections", [])
                owned = set(line["owned"])
                back = [c for c in cols if c not in owned] + [c for c in line["before"] if c in owned]
                if back != cols:
                    _set_collections(zot, item, back)
            elif action == "create_collection":
                col = zot.get(f"/collections/{key}").json()
                _write(zot, "DELETE", f"/collections/{key}", version=col["version"])
            undone += 1
        except ZoteroError as exc:
            left.append((key, str(exc)))
    _record(run_id, "reverted", "")
    return {"undone": undone, "left": left}


def unlink(zot: Zotero) -> dict:
    """Remove from Zotero what Silica owns: its tagged notes and its groups.
    Items are never touched."""
    from silica.config import CONFIG
    from silica.kernel.vault_manifest import get_active_manifest, set_zotero

    notes = [i for i in zot.paged("/items", tag=TAG) if i["data"].get("itemType") == "note"]
    for note in notes:
        _write(zot, "DELETE", f"/items/{note['key']}", version=note["version"])
    link = get_active_manifest().zotero
    removed = 0
    if link and link.groups:
        try:
            subs = zot.paged(f"/collections/{link.groups}/collections")
            for col in [*subs, zot.get(f"/collections/{link.groups}").json()]:
                _write(zot, "DELETE", f"/collections/{col['key']}", version=col["version"])
                removed += 1
        except ZoteroError:
            pass  # already deleted in Zotero: nothing of Silica's left to remove
        set_zotero(CONFIG.vault_path, "groups", "")
    return {"notes": len(notes), "collections": removed}


def authorize(zot: Zotero) -> bool:
    """Ask Zotero for a write key; Zotero raises its own Allow dialog.

    True when the user chose Always Allow: the key is then kept in the
    user-level .env, outside the vault. A single-use key is consumed by its
    first write, so keeping it would only make the next write fail.
    """
    from silica.kernel.recall.paths import atomic_write_bytes
    from silica.onboarding import wizard

    server = zot.server_id or zot.library_id()
    r = zot.send("POST", "/api/local/authorize", json={"appName": "Silica"},
                 headers={"Zotero-Server-ID": server}, timeout=180.0)
    if r.status_code != 200:
        raise ZoteroError("Zotero did not grant write access")
    data = r.json()
    zot.api_key = data.get("key", "")
    remember = bool(data.get("remember"))
    if remember and zot.api_key:
        path = wizard.resolve_env_path()
        base = path.read_text(encoding="utf-8") if path.exists() else ""
        atomic_write_bytes(path, wizard.merge_env(base, {"SILICA_ZOTERO_API_KEY": zot.api_key})
                           .encode("utf-8"))
        os.environ["SILICA_ZOTERO_API_KEY"] = zot.api_key
    return remember
