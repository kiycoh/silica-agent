# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What a write means, whichever transport carries it.

`Vault` is what `silica.driver.DRIVER` hands out. A transport moves bytes and
keeps its index; everything a write owes the rest of Silica is here, once: the
OKF `type` stamp, the preservation check (ADR-0093), the Changes row, the
ledger following a move, and the embedding vector leaving with a note. Reads
pass straight through.

The steps run in the order each backend ran them before they shared this. The
two places that order cannot be one are the transport's to place, and it says
so in `Transport`: when a create's path is final, and when a move's referrers
are rewritten.
"""
from __future__ import annotations

import logging
from typing import Any

from silica.driver.base import NoteRef
from silica.kernel.base import frontmatter as fm
from silica.kernel.write import preserve, session_changes
from silica.kernel.write.notetype import stamp_type

logger = logging.getLogger(__name__)

# Buffered embed-vector deletes per npz save. A move/delete sweep on a big
# vault was paying one whole-index serialization per note; buffering trades
# that for a bounded staleness window: another process sees the phantom
# vector until the threshold or exit flush lands. In-process reads stay
# exact (the store's memory is updated per op).
_EMBED_FLUSH_EVERY = 32


# Deferred embed-delete flush state (see _drop_embed_vector).
_embed_deletes_pending = 0
_embed_flush_registered = False


def _flush_pending_embed_deletes() -> None:
    """Persist buffered embed deletes; safe to call with nothing pending."""
    global _embed_deletes_pending
    if _embed_deletes_pending <= 0:
        return
    try:
        from silica.kernel.recall.embed import get_store
        get_store().save()
        _embed_deletes_pending = 0
    except Exception as exc:
        logger.debug("deferred embed flush failed (non-fatal): %s", exc)


def _register_embed_flush() -> None:
    global _embed_flush_registered
    if not _embed_flush_registered:
        import atexit
        atexit.register(_flush_pending_embed_deletes)
        _embed_flush_registered = True


def _drop_embed_vector(rel_path: str) -> None:
    """Remove a note's embedding vector when it is deleted/renamed, so
    cosine_top_k stops returning it as a phantom candidate before the next
    full /embed rebuild (audit A13). Best-effort: retrieval quality, never fatal.

    The in-memory store updates per op (this process never sees the
    phantom); the npz save is buffered every _EMBED_FLUSH_EVERY deletes
    plus once at exit, because each save serializes the whole index and a
    bulk /organize was paying that per note.
    """
    global _embed_deletes_pending
    try:
        from silica.kernel.recall.embed import get_store
        store = get_store()
        key = rel_path.removesuffix(".md")
        if store.get_vec(key) is not None:  # skip non-embedding vaults / unindexed notes
            store.delete(key)
            _embed_deletes_pending += 1
            if _EMBED_FLUSH_EVERY is None or _embed_deletes_pending >= _EMBED_FLUSH_EVERY:
                store.save()
                _embed_deletes_pending = 0
            else:
                _register_embed_flush()
    except Exception as exc:
        logger.debug("embed vector cleanup failed for %s (non-fatal): %s", rel_path, exc)


def _baseline(rel: str, prior: str | None) -> None:
    """The Changes row for a note about to change. No prior in hand is the ws
    side's absent or undecodable note, which the ledger reads for itself."""
    if prior is None:
        session_changes.touched_from_disk(rel)
    else:
        session_changes.touched(rel, prior)


def _retarget(rel: str, text: str, old: str, new: str, *,
              name_links: bool, path_form: bool) -> str | None:
    """A referrer's text with its links on `new`, None when it has none to
    change. The third write target of a move, judged like the other two."""
    from silica.kernel.link.rename import rewrite_links

    rewritten, n = rewrite_links(text, old, new, rewrite_name_links=name_links, path_form=path_form)
    if n == 0:
        return None
    preserve.check_write(rel, text, rewritten, loss=preserve.Loss.RENAME)
    session_changes.touched(rel, text)
    return rewritten


class Vault:
    """The domain interface over one transport."""

    def __init__(self, transport: Any):
        self.transport = transport

    def __getattr__(self, name: str) -> Any:
        return getattr(self.transport, name)

    def create(self, path: str, content: str, *, loss: preserve.Loss | None = None,
               free=()) -> NoteRef:
        """Create a new note at the given vault-relative path."""
        rel = self.transport.claim(path)
        content = stamp_type(rel, content)   # OKF §4.1 `type`, if absent
        # A path the chat deleted earlier in this turn: the body that delete
        # saved is the before-state (ADR-0093 decision 4). If the plugin folds
        # the path into a key note_key does not, the lookup misses and the
        # create is unchecked.
        preserve.check_recreate(rel, content, loss=loss, free=free)
        # No baseline: the note is new.
        return self.transport.write_new(rel, content, lambda landed: session_changes.touched(landed, None))

    def overwrite(self, path: str, content: str, *, loss: preserve.Loss | None = None,
                  free=()) -> NoteRef:
        """Overwrite an existing note in-place."""
        note = self.transport.prior_for_overwrite(path)
        content = stamp_type(note.rel, content)   # OKF §4.1 `type`, if absent
        preserve.check_write(note.rel, note.text, content, loss=loss, free=free)
        _baseline(note.rel, note.text)
        return self.transport.write_over(note, content)

    def upsert(self, path: str, content: str, *, loss: preserve.Loss | None = None,
               free=()) -> NoteRef:
        if self.exists(path):
            return self.overwrite(path, content, loss=loss, free=free)
        return self.create(path, content, loss=loss, free=free)

    def set_prop(self, ref: NoteRef | str, name: str, value: Any, type_: str = "text") -> None:
        """Set a frontmatter property on a note."""
        note = self.transport.prior_for_props(ref)
        composed = None
        if note.text is not None:
            data, raw, body = fm.split(note.text)
            # `data` None with a block means YAML refused it, not that there is
            # no block: reading it as empty replaced every key a person typed.
            if data is None and raw is not None and self.transport.composes_props:
                raise ValueError(f"the properties of {note.rel} do not parse; fix them first")
            # On ws the plugin's own result is not visible before it writes, so
            # the rule judges what the fs side would write. No ws caller today
            # (2026-10-11); it stays checked for the first one.
            composed = fm.dump({**(data or {}), name: value}, body)
            preserve.check_write(note.rel, note.text, composed, free=(name,))
        _baseline(note.rel, note.text)
        self.transport.write_prop(note, name, value, type_, composed)

    def unset_prop(self, ref: NoteRef | str, name: str) -> None:
        """Remove a frontmatter property, and the block with it when it was the
        last: a property set and then unset leaves the note as it was, which
        set_prop(name, None) would not (it writes `name: null`). No type stamp,
        for the same reason set_prop has none: the person changed one key."""
        if not self.transport.composes_props:
            raise RuntimeError(f"cannot remove `{name}:` over the Obsidian bridge")
        note = self.transport.prior_for_props(ref)
        data, _raw, body = fm.split(note.text)
        if not data or name not in data:
            return
        data.pop(name)
        composed = fm.dump(data, body) if data else body.lstrip("\n")
        preserve.check_write(note.rel, note.text, composed, free=(name,))
        _baseline(note.rel, note.text)
        self.transport.write_prop(note, name, None, "text", composed)

    def move(self, ref: NoteRef | str, to: str) -> None:
        """Move/rename a note. Updates wikilinks (graph-safe)."""
        self.transport.move_note(ref, to, moving=self._moving, moved=self._moved, retarget=_retarget)

    def _moving(self, old: str, new: str, prior: str | None) -> None:
        # The row follows the file: baseline first (in case this is the note's
        # first touch this session), then move it onto the new key.
        _baseline(old, prior)
        session_changes.renamed(old, new)

    def _moved(self, old: str, new: str) -> None:
        # The ledger follows the file the way the session row does: it keys
        # notes by path, and a moved note the ledger still knows under its old
        # name is re-appended into on the next nucleate of its source.
        # Never raises; a failure is logged with what it costs.
        from silica.kernel.write.provenance import rename_note

        root = self.transport.vault_path
        rename_note(old, new, vault_path=None if root is None else str(root))
        # The old key's vector now points at a gone path; drop it or a rename makes
        # the note appear twice in candidates (stale old key + fresh new key). The
        # new path re-embeds lazily on the next build_index (A13).
        _drop_embed_vector(old)

    def delete(self, ref: NoteRef | str) -> None:
        """Delete a note from the vault."""
        note = self.transport.prior_for_delete(ref)
        _baseline(note.rel, note.text)
        self.transport.remove(note)
        _drop_embed_vector(note.rel)

    def autolink_note(
        self,
        path: str,
        candidates: list[str] | None = None,
        title_index: list[str] | None = None,
    ) -> list[str]:
        """Wrap unlinked mentions of vault titles in `path` with links, in place.

        Obsidian's engine runs the plugin's way: it resolves titles against its
        live graph, so `title_index` is accepted for parity and ignored there.
        Otherwise the pure-Python kernel autolink and a plain overwrite;
        `title_index`, when given, is used as-is (caller-built, e.g. LINKING's
        one-per-chunk index) instead of rebuilding via build_title_index(
        self.list_files()) on every call.
        """
        if self.transport.links_natively:
            session_changes.touched_from_disk(path)  # the plugin rewrites the body
            return self.transport.autolink(path, candidates)

        import os

        from silica.kernel.link.autolink import autolink, build_alias_map, build_title_index

        nc = self.read_note(path)
        body = nc.content or ""
        if not body.strip():
            return []
        if title_index is None:
            title_index = build_title_index(self.list_files())
        self_title = os.path.splitext(os.path.basename(path))[0]
        aliases = build_alias_map(self.alias_index(), title_index)
        new_body, added = autolink(
            body, title_index, candidates=candidates, self_title=self_title, aliases=aliases
        )
        if added:
            self.overwrite(path, new_body)
        return added
