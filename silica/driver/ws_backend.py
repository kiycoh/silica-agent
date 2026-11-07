# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Obsidian WS Backend — L0 implementation over the Silica ⇄ Obsidian bridge.

Speaks PROTOCOL.md's `rpc`/`rpc_result` channel to the Obsidian plugin. Driver
calls arrive on the sync agent worker thread, so the socket lives on a dedicated
event-loop thread and `_rpc` marshals across via `run_coroutine_threadsafe` +
`future.result(timeout)`. Each write is one RPC; the plugin holds the
postcondition and its reply is the settle, so writes need no polling.

No CDP machinery here (no subprocess, no settle waiters) — the plugin holds
the postconditions and the reply *is* the settle (PROTOCOL.md §2.4).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

import networkx as nx
from websockets.asyncio.client import connect

from silica.driver.base import (
    MAX_FRAME,
    PROTOCOL_VERSION,
    namesake_order,
    GraphIndexMixin,
    GraphSnapshot,
    Heading,
    Hit,
    Link,
    NoteBytes,
    NoteContent,
    NoteNotFound,
    NoteRef,
)
from silica.kernel.base.markdown import crlf_dominant, encode, extract_links_typed, lf
from silica.kernel.base.layout import is_vault_artifact
from silica.kernel.base.layout import is_source_leaf

logger = logging.getLogger(__name__)


def _disk_prior(path: str) -> tuple[str | None, bool]:
    """The note as it stands on disk, on LF, and whether its bytes were CRLF.

    Read the way session_changes.touched_from_disk reads it: the plugin has
    the same folder open that CONFIG points at, so the prior the check needs
    is one open() away and costs no round trip. None when the file is not
    there (the write is a create)."""
    from pathlib import Path

    from silica.config import CONFIG

    try:
        raw = (Path(CONFIG.vault_path) / path).read_bytes()
    except OSError:
        return None, False
    try:
        return lf(raw.decode("utf-8")), crlf_dominant(raw)
    except UnicodeDecodeError:
        # Not text the composers could have read either: nothing to compare.
        return None, False


class ObsidianWSBackend(GraphIndexMixin):
    """Transport over the WebSocket bridge."""

    kind = "ws"
    knows_mtimes = False
    composes_props = False
    links_natively = True
    vault_path = None   # the plugin has the folder CONFIG points at open

    def __init__(self, url: str, token: str = "", timeout: float = 10.0):
        self._url = url
        self._token = token
        self._timeout = timeout

        # Connection lives on its own loop thread; per-id futures correlate RPCs.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ws: Any = None
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._start_lock = threading.Lock()

        # In-memory graph index (same shape the mixin/base helpers expect).
        self._graph = nx.DiGraph()
        self._unresolved_links: set[tuple[str, str]] = set()
        self._notes: dict[str, NoteRef] = {}
        self._notes_by_name: dict[str, list[NoteRef]] = {}
        self._is_graph_built = False

    # ------------------------------------------------------------------
    # Connection + RPC transport
    # ------------------------------------------------------------------

    @classmethod
    def attached(cls, ws: Any, loop: asyncio.AbstractEventLoop) -> ObsidianWSBackend:
        """Wrap an already-accepted connection (production: `silica connect`
        hosts the server, the plugin dialed in). The caller owns socket and
        loop — it routes rpc_result/rpc_error frames to `_on_frame` and must
        use `detach()` (never `close()`, which would stop the shared loop)."""
        be = cls(url="", token="")
        be._ws = ws
        be._loop = loop
        be._ready.set()
        return be

    def detach(self, reason: str) -> None:
        """Disconnect an attached backend: fail in-flight RPCs, refuse new ones."""
        self._error = RuntimeError(reason)
        self._ws = None
        self._fail_pending(self._error)

    def _ensure_connected(self) -> None:
        with self._start_lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever, name="silica-ws", daemon=True
                )
                self._thread.start()
                asyncio.run_coroutine_threadsafe(self._run(), self._loop)
        if not self._ready.wait(self._timeout):
            raise RuntimeError(f"WS handshake timed out after {self._timeout:.0f}s ({self._url})")
        if self._error is not None:
            raise RuntimeError(f"WS connection failed: {self._error}")

    async def _run(self) -> None:
        try:
            async with connect(self._url, max_size=MAX_FRAME) as ws:
                self._ws = ws
                await ws.send(json.dumps({
                    "type": "hello", "token": self._token,
                    "protocolVersion": PROTOCOL_VERSION, "role": "driver",
                }))
                welcome = json.loads(await ws.recv())
                if welcome.get("type") != "welcome":
                    raise RuntimeError(f"handshake refused: {welcome.get('reason', welcome)}")
                if welcome.get("protocolVersion") != PROTOCOL_VERSION:
                    raise RuntimeError(f"protocol mismatch: {welcome.get('protocolVersion')}")
                self._ready.set()
                async for raw in ws:
                    self._on_frame(json.loads(raw))
        except Exception as exc:  # unblock the waiter so it sees the failure
            self._error = exc
            self._ready.set()
            self._fail_pending(exc)

    def _on_frame(self, frame: dict) -> None:
        # Only rpc replies concern the driver; chat_*/event frames are the
        # transport's (unit 5) and are ignored here.
        kind = frame.get("type")
        rid = frame.get("id")
        if kind not in ("rpc_result", "rpc_error") or rid is None:
            return
        fut = self._pending.pop(rid, None)
        if fut is None or fut.done():
            return
        if kind == "rpc_result":
            fut.set_result(frame.get("result"))
        else:
            # `file not found: <path>` is the one reply the plugin's handlers
            # give for a missing note (handlers.ts, every path-taking method).
            error = frame.get("error", "rpc_error")
            fut.set_exception((NoteNotFound if "file not found" in error.lower() else RuntimeError)(error))

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _send_rpc(self, method: str, params: dict) -> Any:
        rid = self._next_id
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._ws.send(json.dumps({"type": "rpc", "id": rid, "method": method, "params": params}))
            return await asyncio.wait_for(fut, self._timeout)
        finally:
            # Drop the entry on every exit (reply, timeout, send failure) so a
            # timed-out RPC can't strand its future in _pending forever.
            self._pending.pop(rid, None)

    def _rpc(self, method: str, **params: Any) -> Any:
        """Issue one RPC from the sync caller thread and block on its reply."""
        self._ensure_connected()
        assert self._loop is not None  # set by _ensure_connected
        cf = asyncio.run_coroutine_threadsafe(self._send_rpc(method, params), self._loop)
        # +5s guard: the coroutine's own wait_for is authoritative and self-cleans;
        # this only trips if the loop thread itself is wedged.
        return cf.result(self._timeout + 5)

    def close(self) -> None:
        """Close the socket and tear down the loop thread. Idempotent."""
        loop = self._loop
        if loop is None:
            return
        self._loop = None

        async def _shutdown():
            if self._ws is not None:
                await self._ws.close()

        try:
            asyncio.run_coroutine_threadsafe(_shutdown(), loop).result(self._timeout)
        except Exception:
            pass  # closing a dead socket is fine — we only want the loop stopped
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=self._timeout)
        loop.close()

    # ------------------------------------------------------------------
    # Discovery / Read
    # ------------------------------------------------------------------

    def _path_arg(self, ref: NoteRef | str) -> str:
        """Vault-relative path for a `path` RPC param.

        A NoteRef carries its path; a bare name is resolved through the graph
        index (same name→path lookup the fs backend does), falling back to
        `<name>.md` so a not-yet-indexed note still round-trips.
        """
        if isinstance(ref, NoteRef):
            return ref.path or f"{ref.name}.md"
        if "/" in ref:
            # Path link: Obsidian's getFileByPath needs the extension, so a
            # bare `Folder/Note` (as it arrives from wikilinks/search) gets .md.
            last = ref.rsplit("/", 1)[-1]
            return ref if "." in last else f"{ref}.md"
        if ref.endswith(".md"):
            return ref
        self._ensure_graph()
        matched = self._notes_by_name.get(ref.lower(), [])
        return min(matched, key=lambda r: namesake_order(r.path)).path if matched else f"{ref}.md"

    def read_note(self, ref: NoteRef | str) -> NoteContent:
        path = self._path_arg(ref)
        data = self._rpc("read", path=path)
        name = path.rsplit("/", 1)[-1].removesuffix(".md")
        # Folded to LF here, as the fs backend's read does: every composer
        # assumes `\n`, and the write puts the note's own terminator back
        # (ADR-0093 decision 8). The plugin hands the bytes over as they are.
        return NoteContent(
            ref=NoteRef(name=name, path=data.get("path", path)),
            content=lf(data.get("content", "")),
        )

    def mtime_of(self, ref: NoteRef | str) -> float | None:
        """Abstains: the protocol has no stat and a read of the folder CONFIG
        names would turn the sweep on under a live bridge (`knows_mtimes`)."""
        return None

    def list_files(self, folder: str = "") -> list[NoteRef]:
        rows = self._rpc("list_files", folder=folder)
        # Obsidian indexes every .md including Silica's own vault-root output
        # (log.md, GRAPH_REPORT.md). Exclude it here — the single seam fs/ws
        # already gate on — so it reaches no metric that reads list_files: the
        # graph node set (_ensure_graph), embed + cooccurrence builds, mentions.
        return [
            NoteRef(name=r["name"], path=r["path"])
            for r in (rows or [])
            # sources/ leaves: retrieval-invisible (same seam as fs backend)
            if not is_vault_artifact(r["path"]) and not is_source_leaf(r["path"])
        ]

    def list_inbox_files(self) -> list[NoteRef]:
        from silica.kernel.base.manifest import active_inbox_dir
        inbox = active_inbox_dir()
        if not inbox:
            return []
        # all=True: the inbox holds files awaiting conversion (PDFs etc.), not
        # just notes — hiding them made the agent conclude they don't exist.
        # Older plugins ignore the flag and degrade to md-only.
        rows = self._rpc("list_files", folder=inbox, all=True)
        return [NoteRef(name=r["name"], path=r["path"]) for r in (rows or [])]

    def notes_named(self, name: str) -> list[str]:
        self._ensure_graph()
        refs = self._notes_by_name.get((name or "").removesuffix(".md").lower(), [])
        return sorted((r.path for r in refs), key=namesake_order)

    def search_names(self, query: str) -> list[NoteRef]:
        q = query.lower()
        return [r for r in self.list_files() if q in r.name.lower()]

    def props_of(self, ref: NoteRef | str) -> dict:
        data = self._rpc("props_of", path=self._path_arg(ref))
        return data if isinstance(data, dict) else {}

    def outline(self, ref: NoteRef | str) -> list[Heading]:
        rows = self._rpc("outline", path=self._path_arg(ref))
        return [
            Heading(level=h.get("level", 1), text=str(h.get("text", "")), position=h.get("position", 0))
            for h in (rows or [])
        ]

    def search_context(self, query: str) -> list[Hit]:
        return self._hits_from_groups(self._rpc("search_context", query=query))

    def search_context_batch(self, queries: list[str]) -> dict[str, list[Hit]]:
        if not queries:
            return {}
        data = self._rpc("search_context_batch", queries=queries) or {}
        return {q: self._hits_from_groups(data.get(q, [])) for q in queries}

    @staticmethod
    def _hits_from_groups(groups) -> list[Hit]:
        """Flatten PROTOCOL's `[{path, name, matches:[{line, content}]}]` into Hits."""
        hits: list[Hit] = []
        for g in (groups or []):
            if is_source_leaf(g.get("path", "")):  # leaves are search-invisible
                continue
            ref = NoteRef(name=g.get("name", ""), path=g.get("path", ""))
            for m in g.get("matches", []):
                hits.append(Hit(ref=ref, line=m.get("line", 0), snippet=str(m.get("content", ""))))
        return hits

    # ------------------------------------------------------------------
    # Graph — built from two bulk RPCs, read from the in-memory index
    # ------------------------------------------------------------------

    def _ensure_graph(self) -> None:
        """Populate the in-memory index from `resolved_links` + `mention_index`.

        One bulk edge map and one bulk mention map replace N per-note
        round-trips. `list_files` seeds the
        node set so orphans (no links either way) are still present.
        """
        if self._is_graph_built:
            return
        self._graph.clear()
        self._unresolved_links.clear()
        self._notes.clear()
        self._notes_by_name.clear()

        for ref in self.list_files():
            self._notes[ref.path] = ref
            self._graph.add_node(ref.path, ref=ref)
            self._notes_by_name.setdefault(ref.name.lower(), []).append(ref)

        data = self._rpc("resolved_links") or {}
        for source, targets in (data.get("resolved") or {}).items():
            if source not in self._notes:
                continue
            for target in targets:
                if target in self._notes:
                    self._graph.add_edge(source, target)
        for source, targets in (data.get("unresolved") or {}).items():
            if source not in self._notes:
                continue
            for target in targets:
                self._unresolved_links.add((source, target))

        self._is_graph_built = True

    def _path_of(self, ref: NoteRef | str) -> str | None:
        if isinstance(ref, NoteRef):
            return ref.path or None
        if ref.endswith(".md"):
            return ref
        matched = self._notes_by_name.get(ref.lower(), [])
        return min(matched, key=lambda r: namesake_order(r.path)).path if matched else None

    def links(self, ref: NoteRef | str) -> list[NoteRef]:
        self._ensure_graph()
        path = self._path_of(ref)
        if not path:
            return []
        results = [self._node_ref(t) for t in self._graph.successors(path)] if path in self._graph else []
        for s, t in self._unresolved_links:
            if s == path:
                name = t.rsplit("/", 1)[-1].removesuffix(".md")
                results.append(NoteRef(name=name, path=f"{name}.md"))
        return results

    def backlinks(self, ref: NoteRef | str) -> list[NoteRef]:
        self._ensure_graph()
        path = self._path_of(ref)
        if not path or path not in self._graph:
            return []
        return [self._node_ref(s) for s in self._graph.predecessors(path)]

    def file_refs_of(self, ref: NoteRef | str) -> dict:
        # ponytail: no asset census over the WS API yet — empty shape is the
        # declared degrade; implement when a WS session needs note↔file edges.
        return {"embeds": [], "documents": [], "unresolved": []}

    def file_backlinks(self, path: str) -> dict:
        # Same degrade as file_refs_of.
        return {"embeds": [], "documents": []}

    def orphans(self) -> list[NoteRef]:
        self._ensure_graph()
        return [self._graph.nodes[n]["ref"] for n, d in self._graph.in_degree() if d == 0]

    def unresolved(self) -> list[Link]:
        self._ensure_graph()
        return [Link(source=self._node_ref(s), target=t.removesuffix(".md"))
                for s, t in self._unresolved_links]

    def graph_snapshot(self, refs: list[NoteRef] | None = None) -> GraphSnapshot:
        self._ensure_graph()
        neighborhood = None
        if refs is not None:
            neighborhood = set()
            for r in refs:
                if not r.path:
                    continue
                neighborhood.add(r.path)
                if r.path in self._graph:
                    neighborhood.update(self._graph.successors(r.path))
                    neighborhood.update(self._graph.predecessors(r.path))

        paths = self._notes.keys() if neighborhood is None else neighborhood
        # One pass, grouped by source, as in the fs backend (ADR-0092 point 4).
        by_source: dict[str, list[str]] = {}
        for s, t in self._unresolved_links:
            by_source.setdefault(s, []).append(t)
        link_counts: dict[str, int] = {}
        backlink_counts: dict[str, int] = {}
        for path in paths:
            if path not in self._notes:
                continue
            resolved = self._graph.out_degree(path) if path in self._graph else 0
            unresolved = len(by_source.get(path, ()))
            key = path.removesuffix(".md")
            link_counts[key] = resolved + unresolved
            backlink_counts[key] = self._graph.in_degree(path) if path in self._graph else 0

        orphans = [self._notes[p] for p in paths
                   if p in self._notes and (p not in self._graph or self._graph.in_degree(p) == 0)]
        unresolved_links = [Link(source=self._node_ref(s), target=t.removesuffix(".md"))
                            for s, targets in by_source.items() for t in targets
                            if s in self._notes and (neighborhood is None or s in neighborhood)]
        return GraphSnapshot(
            orphans=orphans, unresolved=unresolved_links,
            link_counts=link_counts, backlink_counts=backlink_counts,
        )

    # ------------------------------------------------------------------
    # Write primitives: one RPC each; the reply IS the settle (§2.4)
    # ------------------------------------------------------------------

    def claim(self, path: str) -> str:
        return path   # the plugin refuses a taken path, on the create itself

    def write_new(self, rel: str, content: str, landing) -> NoteRef:
        data = self._rpc("create", path=rel, content=content)
        ref = NoteRef(name=data["name"], path=data["path"])
        # The plugin's path is the authoritative one, and it is only known once
        # the reply is in, so this one lands after the write.
        landing(ref.path)
        self._patch_create(ref.path, ref, content)
        return ref

    def prior_for_overwrite(self, path: str) -> NoteBytes:
        return NoteBytes(path, *_disk_prior(path))

    def write_over(self, note: NoteBytes, content: str) -> NoteRef:
        self._rpc("overwrite", path=note.rel, content=encode(content, note.crlf))
        name = note.rel.rsplit("/", 1)[-1].removesuffix(".md")
        ref = NoteRef(name=name, path=note.rel)
        self._patch_graph_add(note.rel, ref, content)
        return ref

    def prior_for_props(self, ref: NoteRef | str) -> NoteBytes:
        return self.prior_for_overwrite(self._path_arg(ref))

    def write_prop(self, note: NoteBytes, name: str, value: Any, type_: str,
                   composed: str | None) -> None:
        self._rpc("set_prop", path=note.rel, name=name, value=value, type=type_)

    def move_note(self, ref: NoteRef | str, to: str, *, moving, moved, retarget) -> None:
        path = self._path_arg(ref)
        # `retarget` unused: the referrers Obsidian rewrites on its own side stay
        # out of the Changes list, because the plugin never says which notes it
        # touched and guessing them would be a lie in a diff.
        moving(path, to, _disk_prior(path)[0])
        self._rpc("move", path=path, to=to)
        moved(path, to)
        # Obsidian rewrites incoming wikilinks on move; patching every referrer
        # over the wire isn't worth it — reinvalidate, rebuild lazily on next read.
        self._is_graph_built = False

    def prior_for_delete(self, ref: NoteRef | str) -> NoteBytes:
        return self.prior_for_overwrite(self._path_arg(ref))

    def remove(self, note: NoteBytes) -> None:
        self._rpc("delete", path=note.rel)
        self._patch_delete(note.rel)

    def autolink(self, path: str, candidates: list[str] | None) -> list[str]:
        added = self._rpc("autolink_note", path=path, candidates=candidates) or []
        if added:
            self._is_graph_built = False  # body rewritten plugin-side; cheapest correct patch
        return list(added)

    def alias_index(self) -> list[tuple[str, list[str]]]:
        # Empty by design: this backend does not run the autolink kernel, so it
        # has nothing to feed an alias map. Obsidian resolves frontmatter
        # aliases natively when the plugin autolinks.
        return []

    # ------------------------------------------------------------------
    # In-memory index patching after writes (shared half: GraphIndexMixin)
    # ------------------------------------------------------------------

    def _patch_graph_add(self, path: str, ref: NoteRef, content: str) -> None:
        """Patch the index after create/overwrite so same-session reads stay correct.

        No-op if the graph was never built (nothing cached to patch).
        """
        if not self._is_graph_built:
            return
        self._forget_links_from(path)
        self._enter_note(path, ref)
        self._link_from(path, extract_links_typed(content))

    def _patch_delete(self, path: str) -> None:
        """Drop the note, then re-index every note that linked it.

        The fs backend's repair (ADR-0092), with each referrer's body read
        from disk: the plugin's delete replies as soon as `trashFile` returns,
        before Obsidian's metadata cache settles, so asking it for links now
        would answer with no settled timing. The referrers' bytes did not
        change, and the plugin has the folder CONFIG points at open (the
        assumption `session_changes.touched_from_disk` already makes).
        """
        if not self._is_graph_built:
            return
        referrers = self._referrers_of(path)
        self._forget_links_from(path)
        self._leave_note(path)
        self._repatch_from_disk(referrers)

    def _patch_create(self, path: str, ref: NoteRef, content: str) -> None:
        """`_patch_graph_add`, then the notes its name now reaches: a bare name
        ranks its notes by `namesake_order` (ADR-0114), so a shallower new note
        can take a link that resolved elsewhere (ADR-0092 point 1)."""
        self._patch_graph_add(path, ref, content)
        if not self._is_graph_built:
            return
        self._repatch_from_disk(self._sources_reached_by(path, ref.name.lower()))

    def _repatch_from_disk(self, paths: list[str]) -> None:
        from silica.config import CONFIG

        root = (CONFIG.vault_path or "").strip()
        for path in paths:
            try:
                if not root:
                    raise OSError("no vault path configured")
                content = (Path(root) / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                # The disk does not hold what the plugin has open: drop the
                # index and let the next read rebuild it from the plugin, the
                # same fallback move() and autolink_note() take.
                logger.debug("index repair: %s not re-read (%s); rebuilding", path, exc)
                self._is_graph_built = False
                return
            self._patch_graph_add(path, self._node_ref(path), content)

    def _resolve_target(self, target: str, source_path: str = "") -> NoteRef | None:
        """Resolve a link target against the index: by path, else by name.
        Why this is not the fs rule: GraphIndexMixin."""
        from silica.kernel.base.markdown import resolve_relative

        # A `./`/`../` target is relative to the source note and invisible to
        # the suffix matcher until normalized, as on fs.
        normalized = resolve_relative(target, source_path)
        if normalized is None:
            return None  # escapes the vault root
        t = normalized.removesuffix(".md")
        if "/" in t:
            exact = self._notes.get(t + ".md")
            if exact is not None:
                return exact
            suffix = "/" + t.lower() + ".md"
            matches = [r for p, r in self._notes.items() if p.lower().endswith(suffix)]
            return min(matches, key=lambda r: (r.path.count("/"), r.path.lower())) if matches else None
        matched = self._notes_by_name.get(t.lower(), [])
        return min(matched, key=lambda r: namesake_order(r.path)) if matched else None
