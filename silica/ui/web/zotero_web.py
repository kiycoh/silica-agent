# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What Settings › Zotero and Explore › Library ask the server (ADR-0055).

Plain functions returning dicts, so server.py stays a list of thin routes and
the logic is tested without a browser. A ZoteroError becomes a 409 there.
"""
from __future__ import annotations

import os
from pathlib import Path

from silica.config import CONFIG
from silica.sources import zotero, zotero_write


def _link():
    from silica.kernel.vault_manifest import get_active_manifest

    return get_active_manifest().zotero


def _probe_client() -> "zotero.Zotero":
    link = _link()
    return zotero.Zotero(server_id=link.library if link else "",
                         api_key=os.getenv("SILICA_ZOTERO_API_KEY", ""))


def _bbt(zot) -> bool:
    try:
        r = zot.send("POST", "/better-bibtex/json-rpc",
                     json={"jsonrpc": "2.0", "method": "api.ready", "params": []})
        return r.status_code == 200 and "result" in r.json()
    except (zotero.ZoteroError, ValueError):
        return False


def status() -> dict:
    """The Status row: one of running, closed, api_off, other, read_only."""
    link = _link()
    zot = _probe_client()
    base = {"linked": link is not None, "write": bool(zot.api_key),
            "backlinks": link.backlinks if link else True,
            "inbox": link.inbox if link else "", "groups": link.groups if link else "",
            "linked_library": link.library if link else ""}
    try:
        r = zot.send("GET", "/api/")
    except zotero.OtherLibrary as exc:
        return {**base, "state": "other", "library": exc.answered}
    except zotero.ZoteroError:
        return {**base, "state": "closed"}
    if r.status_code >= 400:
        return {**base, "state": "api_off"}
    version = r.headers.get("X-Zotero-Version", "")
    library = r.headers.get("Zotero-Server-ID", "")
    major = version.split(".")[0]
    items = [i for i in zot.paged("/items/top") if zotero._is_paper(i)]
    cols = [c["data"]["name"] for c in zot.paged("/collections")
            if not c["data"].get("parentCollection")]
    elsewhere = "" if link else str(zotero.linked_elsewhere(library, CONFIG.vault_path) or "")
    return {**base, "state": "read_only" if major.isdigit() and int(major) < 10 else "running",
            "version": version, "library": library, "items": len(items), "bbt": _bbt(zot),
            "collections": sorted(cols), "elsewhere": elsewhere}


def link(on: bool) -> dict:
    """Write or remove the `zotero:` block. Raises ZoteroError when there is no
    library to link or another vault already links it (decision 10)."""
    from silica.kernel.vault_manifest import set_zotero, unlink_zotero

    if not on:
        unlink_zotero(CONFIG.vault_path)
        return {"ok": True, "linked": False}
    library = zotero.Zotero().library_id()
    if not library:
        raise zotero.ZoteroError("Zotero is not answering on 127.0.0.1:23119")
    elsewhere = zotero.linked_elsewhere(library, CONFIG.vault_path)
    if elsewhere is not None:
        raise zotero.ZoteroError(f"library {library} is linked to {elsewhere}; this vault "
                                 "reaches its papers by peek")
    set_zotero(CONFIG.vault_path, "library", library)
    return {"ok": True, "linked": True, "library": library}


_SETTABLE = {"backlinks": bool, "inbox": str}


def setting(key: str, value) -> dict:
    from silica.kernel.vault_manifest import set_zotero

    kind = _SETTABLE.get(key)
    if kind is None:
        raise KeyError(key)
    value = (value if isinstance(value, bool) else str(value).lower() == "true") \
        if kind is bool else str(value or "")
    set_zotero(CONFIG.vault_path, key, value)
    return {"ok": True, "key": key, "value": value}


def grant() -> dict:
    return {"ok": True, "kept": zotero_write.authorize(zotero.client())}


def _papers(zot, path: str) -> list[dict]:
    return [i for i in zot.paged(path) if zotero._is_paper(i)]


def _holder(zot) -> Path | None:
    """The vault that holds the link, when it is not this one (ADR-0066
    decision 1): what a read-only Library names, and where its peek reads."""
    if _link() is not None:
        return None
    library = zot.library_id()
    return zotero.linked_elsewhere(library, CONFIG.vault_path) if library else None


def library() -> dict:
    """The tree: every collection with "in vault / total", and the header.

    Every vault reads it (ADR-0066 decision 1). One that does not hold the
    link gets it read-only, with the path of the vault that does."""
    zot = zotero.client()
    items = _papers(zot, "/items/top")
    leaves = zotero.vault_leaves()
    det = zotero.last_detection(CONFIG.vault_path)
    cols = []
    for c in zot.paged("/collections"):
        members = [i for i in items if c["key"] in i["data"].get("collections", [])]
        cols.append({"key": c["key"], "name": c["data"]["name"],
                     "parent": c["data"].get("parentCollection") or "",
                     "total": len(members), "in_vault": sum(i["key"] in leaves for i in members)})
    cols.sort(key=lambda c: c["name"].casefold())
    link = _link()
    holder = _holder(zot)
    return {"total": len(items), "in_vault": sum(i["key"] in leaves for i in items),
            "new": len(det["new"]), "collections": cols,
            "inbox": _inbox_key(zot, link.inbox) if link and link.inbox else "",
            "read_only": link is None, "holder": str(holder or "")}


def _inbox_key(zot, value: str) -> str:
    from silica.sources.zotero_cli import _collection_key

    return _collection_key(zot, value)


def _row(item: dict, leaves: dict, det: dict, counts, there: dict | None = None) -> dict:
    from silica.kernel.recall.citations import norm_id
    from silica.kernel.recall.paths import SOURCES_DIR
    from silica.kernel.vault_manifest import in_write_dir

    data = item["data"]
    key = item["key"]
    ids = {norm_id(data.get("DOI", "")), norm_id(zotero._arxiv_of(data))} - {""}
    if key in det["changed"]:
        state = "changed"
    elif key in leaves:
        state = "in"
    elif there and key in there:
        state = "there"   # a leaf in the linked vault, read here by peek
    elif key in det["new"]:
        state = "new"
    else:
        state = "out"
    creators = data.get("creators") or [{}]
    return {"key": key, "state": state, "title": data.get("title", ""),
            "citekey": data.get("citationKey", ""),
            "sur": creators[0].get("lastName") or creators[0].get("name", ""),
            "year": (item.get("meta", {}).get("parsedDate") or "")[:4],
            "cited_by": sum(counts[i] for i in ids),
            "leaf": f"{in_write_dir(SOURCES_DIR)}/{leaves[key]}" if key in leaves else ""}


def collection(key: str) -> dict:
    """One collection's papers. The inbox collection also carries the two
    filing suggestions per row; any other is sorted by how often the library
    cites each paper."""
    from silica.kernel.recall.citations import cited_counts

    zot = zotero.client()
    items = _papers(zot, f"/collections/{key}/items/top")
    leaves = zotero.vault_leaves()
    det = zotero.last_detection(CONFIG.vault_path)
    counts = cited_counts()
    holder = _holder(zot)
    there = {k: name for k, (name, _) in zotero._leaf_index(holder).items()} if holder else {}
    rows = [_row(i, leaves, det, counts, there) for i in items]
    link = _link()
    is_inbox = bool(link and link.inbox) and _inbox_key(zot, link.inbox) == key
    if is_inbox:
        sugg = {s["key"]: s for s in zotero_write.suggest_filing(zot, key)}
        for r in rows:
            s = sugg.get(r["key"])
            if s:
                r.update(first=s["first"], second=s["second"], votes=s["votes"])
    else:
        rows.sort(key=lambda r: (-r["cited_by"], r["sur"].casefold()))
    return {"rows": rows, "inbox": is_inbox}


def item(key: str) -> dict:
    """The preview of a paper not in the vault yet."""
    data = zotero.client().item(key)
    paper = zotero.paper_of(zotero.client(), data)
    return {"key": key, "title": paper.title, "authors": paper.fields.get("authors", []),
            "year": paper.fields.get("year", ""), "venue": paper.fields.get("venue", ""),
            "abstract": paper.abstract, "pdf": paper.pdf is not None,
            "zotero": f"zotero://select/library/items/{key}"}


def peek(key: str) -> dict:
    """A paper's leaf in the linked vault, read from here by peek (ADR-0066
    decision 1; ADR-0035): through silica_read_note's `vault=`, which reads a
    known vault read-only and never writes to it."""
    from silica.tools import TOOLS

    holder = _holder(zotero.client())
    if holder is None:
        raise zotero.ZoteroError("no other vault holds this library's link")
    leaf = zotero._leaf_index(holder).get(key)
    if leaf is None:
        raise zotero.ZoteroError(f"{holder} has no leaf for {key}")
    text = TOOLS["silica_read_note"].run(name=leaf[0].removesuffix(".md"), vault=str(holder))
    return {"vault": str(holder), "leaf": leaf[0], "text": text}


def covers_status() -> dict:
    """The COVERS pass (ADR-0066 decision 6): whether it runs, how many papers
    it has read of how many, and why it stopped if it failed."""
    from silica.sources import zotero_covers

    return zotero_covers.status()


def covers_start() -> dict:
    from silica.sources import zotero_covers

    return zotero_covers.start((CONFIG.vault_path or "").strip())


def _journal_run() -> str:
    from silica.kernel.write.undo_journal import get_undo_journal

    return get_undo_journal().start_run(source="zotero", vault=CONFIG.vault_path.strip() or None)


def file(moves: list[list[str]]) -> dict:
    link = _link()
    if not link or not link.inbox:
        raise zotero.ZoteroError("no inbox collection set: Settings › Zotero › Inbox collection")
    zot = zotero.client()
    run = _journal_run()
    moved = zotero_write.file_items(zot, [(k, t) for k, t in moves],
                                    _inbox_key(zot, link.inbox), run_id=run)
    return {"moved": moved, "run": run}


def groups_preview() -> dict:
    plan = zotero_write.compute_group_plan()
    link = _link()
    now: dict[str, int] = {}
    if link and link.groups:
        zot = zotero.client()
        try:
            for sub in zot.paged(f"/collections/{link.groups}/collections"):
                now[sub["data"]["name"]] = len(zot.paged(f"/collections/{sub['key']}/items/top"))
        except zotero.ZoteroError:
            now = {}  # the collection was deleted in Zotero: everything reads as new
    return {"groups": [{"label": label, "papers": len(keys), "now": now.get(label, 0)}
                       for label, keys in plan.items()]}


def groups_write() -> dict:
    plan = zotero_write.compute_group_plan()
    run = _journal_run()
    changed = zotero_write.write_groups(zotero.client(), plan, run_id=run)
    return {"groups": len(plan), "changed": changed, "run": run}


_PAPER_FM = ("zotero_key", "citekey", "title", "authors", "year", "venue", "doi", "arxiv", "pdf_key")


def paper_context(leaf_rel: str, props: dict, backlinks: list[dict]) -> dict:
    """The paper half of the Node panel (ADR-0055 "The paper in the Node panel").

    Collections are read live and are [] while Zotero is closed: membership
    is never copied into the vault (decision 10).
    """
    from silica.kernel.recall.citations import paper_neighbours
    from silica.kernel.recall.paths import is_source_leaf

    nb = paper_neighbours(leaf_rel) or {"cites": [], "cited_by": [], "missing": [], "labels": {}}
    name = lambda rel: {"name": nb["labels"].get(rel, rel), "path": rel}  # noqa: E731
    top = []
    for i, n in nb["missing"][:5]:
        kind, value = i.split(":", 1)
        top.append({"label": f"arXiv {value}" if kind == "arxiv" else f"doi {value}",
                    "href": (f"https://arxiv.org/abs/{value}" if kind == "arxiv"
                             else f"https://doi.org/{value}"), "cited_by": n})
    collections: list[str] = []
    key = str(props.get("zotero_key") or "")
    try:
        zot = zotero.client()
        names = {c["key"]: c["data"]["name"] for c in zot.paged("/collections")}
        collections = [names.get(c, c) for c in zot.item(key)["data"].get("collections", [])]
    except Exception:
        collections = []  # Zotero closed or the item gone: the panel says nothing rather than guess
    return {
        "fm": {k: props[k] for k in _PAPER_FM if props.get(k)},
        "cites": [name(r) for r in nb["cites"]],
        "cited_by": [name(r) for r in nb["cited_by"]],
        "missing": {"count": len(nb["missing"]), "top": top},
        "notes": [b for b in backlinks if not is_source_leaf(b.get("path", ""))],
        "collections": collections,
    }


def labels() -> dict[str, str]:
    """{leaf path: "Surname Year"} for every paper leaf: what a read chip in
    chat says instead of the citekey stem. No Zotero call, the leaves suffice."""
    from silica.kernel.recall.citations import _label_base, _paper_leaves

    return {rel: _label_base(data) for rel, (data, _, _) in _paper_leaves().items()}


def unlink() -> dict:
    return zotero_write.unlink(zotero.client())
