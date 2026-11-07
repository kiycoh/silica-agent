# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Obsidian Driver — L0 abstraction over the vault I/O substrate.

From SILICA.md §3 L0:
  Adapter typed by DOMAIN, not by transport. Everything else talks to the
  Driver, never to disk directly. Two interchangeable transports:
  - fs: direct filesystem + index (derived from Hermes scripts)
  - ws: the Obsidian bridge plugin, installed live by `silica connect`

A transport moves bytes and keeps its index. What a write must do around the
bytes (stamp, preservation check, Changes row, provenance, embed vector) is
`silica.kernel.vault.Vault`, the one object DRIVER hands out; this package
imports nothing above `kernel.base` and only ever sees it through
`kernel.base.vault_seam`.

This module defines the Protocols (`Transport` below the wrapper,
`ObsidianDriver` above it) and the domain types.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, NamedTuple, Protocol, runtime_checkable

from silica.kernel.base.syntax import ADR_REF_RE

# Both ends of the bridge read these, here because the server (ui/connect.py)
# sits above this package and may not be imported from it.
PROTOCOL_VERSION = 1

# websockets' 1 MiB default max_size would sever the connection on any note
# body over ~1 MB (rpc read replies, bulk-nucleate creates). 32 MiB matches the
# no-ceiling behavior of the fs backend.
MAX_FRAME = 2**25


# ---------------------------------------------------------------------------
# Domain types & Exceptions
# ---------------------------------------------------------------------------

class NoteNotFound(RuntimeError):
    """The note is not there, and nothing else went wrong.

    A RuntimeError because that is what "cannot read this" has always been, so
    a caller that tolerates every failed read keeps tolerating this one. Catch
    this instead when absence means something a dead bridge must not: a bridge
    down and a path outside the vault stay plain RuntimeError.
    """


@dataclass(frozen=True)
class NoteRef:
    """Reference to a note in the vault."""
    name: str           # wikilink-style name (no extension)
    path: str = ""      # relative path within vault (folder/note.md)


@dataclass
class NoteContent:
    """Full content of a note."""
    ref: NoteRef
    content: str


class NoteBytes(NamedTuple):
    """A note as a transport found it just before a write, handed back to the
    transport's write so the two agree on the place."""
    rel: str                  # vault-relative path, as the Changes list keys it
    text: str | None          # on LF; None when absent or undecodable (ws only)
    crlf: bool                # the bytes were CRLF: the write puts that back
    at: Path | None = None    # fs: the file the text came from


@dataclass
class Hit:
    """Search result with context."""
    ref: NoteRef
    line: int = 0
    snippet: str = ""
    # Total occurrences of the query in this note's body — the density signal.
    # Backends cap how many Hits they materialize per note, so len(hits) alone
    # saturates at the cap and cannot rank two heavy notes. 0 = not measured
    # (backends without the counter): consumers fall back to len(hits).
    note_matches: int = 0


@dataclass
class Heading:
    """A heading in a note's outline."""
    level: int          # 1-6
    text: str
    position: int = 0   # char offset


@dataclass(frozen=True)
class Link:
    """A link between notes."""
    source: NoteRef
    target: str         # wikilink target (may be unresolved)


@dataclass
class GraphSnapshot:
    """Snapshot of the vault graph for non-regression diffing."""
    orphans: list[NoteRef] = field(default_factory=list)
    unresolved: list[Link] = field(default_factory=list)
    link_counts: dict[str, int] = field(default_factory=dict)   # note -> outgoing count
    backlink_counts: dict[str, int] = field(default_factory=dict)  # note -> incoming count


def namesake_order(path: str) -> tuple[int, str]:
    """Which of several notes sharing a name a bare name means: the shallowest
    path, then alphabetical, the rank the fs link resolver always used.

    Reads took the first note the index met instead, which is the filesystem's
    directory order: copying one vault from ext4 to tmpfs changed the note read
    for 30 of its 31 shared names, and on 29 of them the read disagreed with
    the link (ADR-0114).
    """
    return (path.count("/"), path.lower())


# ---------------------------------------------------------------------------
# GraphIndexMixin — helpers shared verbatim by both backends
# ---------------------------------------------------------------------------

class GraphIndexMixin:
    """Graph-index helpers shared by the fs and ws backends.

    Subclasses build the in-memory index in ``_ensure_graph()`` and expose
    the ``_notes``/``_notes_by_name``/``_unresolved_links``/``_graph``
    attributes it populates, plus the read ``exists`` is answered from.

    Both halves of that contract are declared below rather than left implicit:
    a mixin that reads names it does not define is only safe while every
    subclass happens to define them, and nothing was checking that.

    ``_resolve_target`` is the one half that stays two. On fs it IS the index:
    every edge comes from it, so it carries Obsidian's rules (`#` and `^` to
    the note itself, `ADR-NNNN` to its record, the referrer's folder before
    the shallowest path). On ws the plugin's own `resolved_links` is the index
    and this only keeps a write's patch honest until the next rebuild, so it
    is by path, else by `namesake_order`. Merging them moves which note a
    patched ws edge points at, and which notes the graph gate sees.
    """

    # Built by the subclass's _ensure_graph(); read by every helper here.
    _notes: dict[str, NoteRef]
    _notes_by_name: dict[str, list[NoteRef]]
    _unresolved_links: set
    _graph: Any

    def _ensure_graph(self) -> None:
        raise NotImplementedError

    def _resolve_target(self, target: str, source_path: str = "") -> NoteRef | None:
        raise NotImplementedError

    def _repatch_from_disk(self, paths: list[str]) -> None:
        raise NotImplementedError

    def _add_link_edge(self, source: str, target: str, *, scaffold: bool) -> None:
        """One resolved wikilink edge, carrying its class (kernel.link.ast).

        Two spellings can resolve to one note (an alias in the frontmatter and
        the title in prose); prose wins, so a scaffold occurrence never
        downgrades an edge the prose already justified.
        """
        if self._graph.has_edge(source, target):
            if not scaffold:
                self._graph[source][target]["scaffold"] = False
            return
        self._graph.add_edge(source, target, scaffold=scaffold)

    def _link_from(self, path: str, typed: dict[str, bool]) -> None:
        """A note's links into the index: an edge, or an unresolved pair."""
        for target, scaffold in typed.items():
            resolved = self._resolve_target(target, source_path=path)
            if resolved is not None:
                self._add_link_edge(path, resolved.path, scaffold=scaffold)
            else:
                self._unresolved_links.add((path, target))

    def _forget_links_from(self, path: str) -> None:
        """What a note linked, before it is indexed again or leaves."""
        if path in self._graph:
            self._graph.remove_edges_from(list(self._graph.out_edges(path)))
        self._unresolved_links = {(s, t) for s, t in self._unresolved_links if s != path}

    def _enter_note(self, path: str, ref: NoteRef) -> None:
        self._notes[path] = ref
        self._graph.add_node(path, ref=ref)
        by_name = self._notes_by_name.setdefault(ref.name.lower(), [])
        if ref not in by_name:
            by_name.append(ref)

    def _leave_note(self, path: str) -> None:
        if path in self._graph:
            self._graph.remove_node(path)
        old_ref = self._notes.pop(path, None)
        if old_ref and old_ref.name.lower() in self._notes_by_name:
            name = old_ref.name.lower()
            self._notes_by_name[name] = [r for r in self._notes_by_name[name] if r.path != path]

    def _referrers_of(self, path: str) -> list[str]:
        return [p for p in self._graph.predecessors(path) if p != path] if path in self._graph else []

    def _sources_reached_by(self, path: str, name: str, adr: str = "") -> list[str]:
        """The notes to re-index after a new note took `name`.

        (a) Every source of an unresolved pair that now resolves to it, filtered
        by name before resolving, so a create pays for the pairs that could
        match and not for all 546 of the test vault. (b) Every referrer of a
        namesake, because a new note can take a name link that resolved
        elsewhere. A `NNNN-` note also answers `ADR-NNNN` where the resolver
        knows that (`adr`), so for it the namesakes are the notes sharing the
        number.
        """
        sources: set[str] = set()
        for source, target in list(self._unresolved_links):
            base = target.rsplit("/", 1)[-1].removesuffix(".md")
            if base.lower() != name and not (
                    adr and (m := ADR_REF_RE.fullmatch(base)) and m.group(1) == adr):
                continue
            hit = self._resolve_target(target, source_path=source)
            if hit is not None and hit.path == path:
                sources.add(source)
        namesakes = [r.path for r in self._notes_by_name.get(name, ())]
        if adr:
            namesakes += [r.path for n, rs in self._notes_by_name.items()
                          if n.startswith(adr + "-") for r in rs]
        for other in namesakes:
            if other != path and other in self._graph:
                sources.update(self._graph.predecessors(other))
        sources.discard(path)
        return sorted(sources)

    def read_note(self, ref: NoteRef | str) -> Any:
        raise NotImplementedError

    def exists(self, ref: NoteRef | str) -> bool:
        """Whether `read_note(ref)` would find a note. What else that read
        raises is raised here too: a dead bridge is not an absent note."""
        try:
            self.read_note(ref)
        except NoteNotFound:
            return False
        return True

    def _node_ref(self, path: str) -> NoteRef:
        if path in self._notes:
            return self._notes[path]
        name = path.rsplit("/", 1)[-1].removesuffix(".md")
        return NoteRef(name=name, path=path)

    def graph_data(self) -> tuple[dict, set, Any]:
        """Return (notes, unresolved_links, graph) for in-process consumers."""
        self._ensure_graph()
        return self._notes, self._unresolved_links, self._graph


# ---------------------------------------------------------------------------
# Protocols: the domain interface (SILICA.md §3 L0)
# ---------------------------------------------------------------------------

@runtime_checkable
class NoteReader(Protocol):
    """The read half of the domain interface, served by a transport as it is
    and passed through the Vault untouched.

    Freshness contract (NORMATIVE from SILICA.md):
      The Driver MUST declare read-after-write semantics. After a create/
      set_prop/move/delete, the Driver guarantees that the next read reflects
      the mutation, graph reads included: a delete leaves its referrers' links
      unresolved and a create resolves the links that were waiting for it
      (ADR-0092). If the underlying cache updates asynchronously, the backend
      MUST wait/poll until settled. A method that doesn't respect the same
      freshness contract on both backends is a bug, not a difference.
    """

    # What a caller may ask of this transport, declared so nothing has to probe
    # for a method to find out. `kind` names it for a person ("fs", "ws").
    kind: str
    # `mtime_of` answers: a cheap change signal exists. Without it a sweep has
    # nothing to compare, and the bridge's own write path keeps indexes fresh.
    knows_mtimes: bool
    # Properties are composed on this side and land as text, so a block that
    # does not parse is refused here and `unset_prop` can run. The plugin takes
    # a name and a value, parses for itself, and has no removal.
    composes_props: bool

    # -- discovery / read --------------------------------------------------

    def search_names(self, query: str) -> list[NoteRef]:
        """Search vault note names matching query."""
        ...

    def notes_named(self, name: str) -> list[str]:
        """Paths of every note named `name` (case-insensitive), in
        `namesake_order`. More than one means the bare name does not say which
        note it is; a caller that must not guess asks the user, or the model,
        to name the folder (ADR-0114)."""
        ...

    def search_context(self, query: str) -> list[Hit]:
        """Search vault content with line-level context snippets."""
        ...

    def search_context_batch(self, queries: list[str]) -> dict[str, list[Hit]]:
        """Like search_context, but for many queries in one call.

        Key = query, value = the Hits (ref + line + snippet) the corresponding
        single search_context call would return. Additive: single-query callers
        stay on search_context.
        """
        ...

    def read_note(self, ref: NoteRef | str) -> NoteContent:
        """Read a note's full content by name or ref. `NoteNotFound` when it is
        not there; any other failure stays what it was."""
        ...

    def exists(self, ref: NoteRef | str) -> bool:
        """Whether `read_note(ref)` would find a note. Absence is False; a
        failure to find out (a dead bridge) raises, it is not an answer."""
        ...

    def mtime_of(self, ref: NoteRef | str) -> float | None:
        """Last-modified epoch seconds, or None to abstain: the note cannot be
        stat'd, or the transport has no clock to read (`knows_mtimes`)."""
        ...

    def props_of(self, ref: NoteRef | str) -> dict:
        """Read frontmatter properties (~hundreds of tokens, no body)."""
        ...

    def outline(self, ref: NoteRef | str) -> list[Heading]:
        """Get the heading tree of a note."""
        ...

    # -- graph -------------------------------------------------------------

    def links(self, ref: NoteRef | str) -> list[NoteRef]:
        """Outgoing links from a note."""
        ...

    def backlinks(self, ref: NoteRef | str) -> list[NoteRef]:
        """Incoming links to a note."""
        ...

    def orphans(self) -> list[NoteRef]:
        """Notes with no incoming links."""
        ...

    def unresolved(self) -> list[Link]:
        """Unresolved wikilinks in the vault."""
        ...

    def file_refs_of(self, ref: NoteRef | str) -> dict:
        """Files a note references: body embeds/links plus `documents:`.
        Shape: {"embeds": [...], "documents": [...], "unresolved": [...]} —
        a backend without an asset index serves the empty shape, never None."""
        ...

    def file_backlinks(self, path: str) -> dict:
        """Notes referencing a file, by embed and by `documents:`.
        Shape: {"embeds": [...], "documents": [...]}."""
        ...

    def graph_snapshot(self, refs: list[NoteRef] | None = None) -> GraphSnapshot:
        """Graph snapshot for non-regression gating.

        If refs is provided, performs an incremental snapshot covering only
        the touched notes and their 1-hop neighborhood.
        """
        ...

    def alias_index(self) -> list[tuple[str, list[str]]]:
        """(title, aliases) pairs for notes declaring frontmatter aliases.

        Feeds autolink.build_alias_map so a mention of a note's alias links to
        the note itself. Backends that delegate autolinking to Obsidian return
        an empty list: Obsidian resolves aliases on its own.
        """
        ...

    # -- advanced ----------------------------------------------------------

    def list_files(self, folder: str = "") -> list[NoteRef]:
        """List all markdown files, optionally filtered by folder."""
        ...

    def list_inbox_files(self) -> list[NoteRef]:
        """List all files in the inbox directory."""
        ...

    # -- graph data (in-process, avoids O(N) subprocess calls) -------------

    def graph_data(self) -> tuple[dict, set, Any]:
        """Return (notes, unresolved_links, graph) for in-process consumers.

        Ensures the graph index is populated first. Used by graph_export to
        avoid O(N) CLI calls while keeping the contract explicit.
        """
        ...


@runtime_checkable
class Transport(NoteReader, Protocol):
    """What carries a note to the vault and back. No stamp, no check, no
    Changes row: every write verb here runs under a `Vault`, which decides what
    a write means. Named apart from the `ObsidianDriver` verbs on purpose, so a
    write sent to a bare transport fails instead of landing without its policy.
    """

    vault_path: Path | None   # the ledger's vault; None: whatever CONFIG names

    # Obsidian's own engine wraps links in place, so the wrapper's kernel pass
    # does not apply: a transport that sets this has `autolink(path, candidates)`.
    links_natively: bool

    def claim(self, path: str) -> str:
        """The path a create lands on. Raises if it is taken and the transport
        can tell before writing."""
        ...

    def write_new(self, rel: str, content: str, landing: Callable[[str], None]) -> NoteRef:
        """Land a new note. `landing(path)` runs once the final path is known:
        before the bytes on fs, on the reply on ws, whose plugin names it."""
        ...

    def prior_for_overwrite(self, path: str) -> NoteBytes:
        """The note as it stands; `NoteNotFound` on fs, whose prior is the
        bytes. On ws a missing note answers `text=None` and the plugin raises."""
        ...

    def write_over(self, note: NoteBytes, content: str) -> NoteRef:
        """`content`, composed on LF, in place of `note`, in its terminator."""
        ...

    def prior_for_props(self, ref: NoteRef | str) -> NoteBytes:
        """`prior_for_overwrite` by name or ref, contained against symlinks."""
        ...

    def write_prop(self, note: NoteBytes, name: str, value: Any, type_: str,
                   composed: str | None) -> None:
        """One property. fs writes `composed`; ws sends the name and the value
        and the plugin composes, so `composed` is only what the check judged."""
        ...

    def prior_for_delete(self, ref: NoteRef | str) -> NoteBytes:
        """The note about to go. A symlink is the user's and may be deleted, so
        this one does not contain."""
        ...

    def remove(self, note: NoteBytes) -> None:
        ...

    def move_note(self, ref: NoteRef | str, to: str, *,
                  moving: Callable[[str, str, str | None], None],
                  moved: Callable[[str, str], None],
                  retarget: Callable[..., str | None]) -> None:
        """Move a note and leave no link behind. `moving(old, new, prior)` runs
        before the file moves and `moved(old, new)` right after. `retarget`
        composes a referrer's new text, None to leave it, for a transport that
        rewrites referrers itself (fs); Obsidian does its own."""
        ...


@runtime_checkable
class ObsidianDriver(NoteReader, Protocol):
    """What the rest of Silica writes through: `DRIVER`, a `Vault` over the
    live transport."""

    # -- write (graph-safe) ------------------------------------------------

    def create(self, path: str, content: str, *, loss: Enum | None = None,
               free=()) -> NoteRef:
        """Create a new note. Path is relative to vault root. Raises if file exists.

        `loss` and `free`, here and on upsert/overwrite, are ADR-0093's: the
        closed reason a call site declares for what its write may remove, and
        the frontmatter keys it may change. Never set from an op or a tool
        argument."""
        ...

    def upsert(self, path: str, content: str, *, loss: Enum | None = None,
               free=()) -> NoteRef:
        """Write content unconditionally: create when missing, overwrite when present.

        For callers whose semantics are "refresh this staging/derived note"
        (inbox conversion, source stubs, undo restore), NOT for vault write
        ops, which must fail loudly on an unexpected existing note (create).
        `loss` and `free` reach whichever of the two runs (ADR-0093 decision 6).
        """
        ...

    def overwrite(self, path: str, content: str, *, loss: Enum | None = None,
                  free=()) -> NoteRef:
        """Overwrite an existing note in-place, preserving history.

        Unlike delete+create, this MUST NOT destroy Obsidian's version history
        or break block-references. Use for patch and overwrite op types.
        The CLI backend uses `obsidian create path=... overwrite=true`.
        The FS backend writes the file directly.
        """
        ...

    def set_prop(self, ref: NoteRef | str, name: str, value: Any, type_: str = "text") -> None:
        """Set a frontmatter property on a note."""
        ...

    def unset_prop(self, ref: NoteRef | str, name: str) -> None:
        """Remove a frontmatter property, and the block with it when it was the
        last. Needs `composes_props`; a transport without it refuses."""
        ...

    def move(self, ref: NoteRef | str, to: str) -> None:
        """Move/rename a note. Updates wikilinks (graph-safe)."""
        ...

    def delete(self, ref: NoteRef | str) -> None:
        """Delete a note from the vault."""
        ...

    def autolink_note(
        self,
        path: str,
        candidates: list[str] | None = None,
        title_index: list[str] | None = None,
    ) -> list[str]:
        """Wrap unlinked mentions of vault titles in `path` with links, in place.

        Returns the list of titles linked. The CLI backend delegates skip-region
        detection, link resolution, and link rendering to Obsidian's own engine
        (respecting the user's link-format preference). The FS backend uses the
        deterministic pure-Python autolink() kernel. `candidates` optionally
        restricts which titles are considered (embedding/cluster-prioritised subset).
        `title_index` optionally supplies a prebuilt disambiguated vault-title
        list so callers batching many notes avoid a per-note rebuild; when
        None the backend builds its own.
        """
        ...
