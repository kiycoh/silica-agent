# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A note has several keys, and two stores that join on the wrong one lose the
note without a sound: `learner.prerequisites_of` looked a folded key up in a
map that keeps case and found nothing for months (2fb885e).

    key         case         `.md`   the keyspace it speaks
    store_key   kept         off     embeddings, co-occurrence and lexical indexes
    ledger_key  lower()      off     quiz.jsonl joins, the report's node ids
    gate_key    lower()      off     the graph regression gate; `Foo.MD` folds too
    note_key    casefold()   off     the provenance ledger, the undo journal
    stem        kept         off     a note's name, as a wikilink says it

Stored files keep their spelling: `store_key` is written into three index files
and must never start folding, `note_key` matches `note_ref`'s spelling inside
the provenance ledger. `lower()` and `casefold()` part on ß and the Turkish
dotted I, which is why neither fold replaces the other: `quiz.jsonl` holds raw
paths and folds at read, so changing it is safe for the file and moves every
history join that crosses a vault's ß.
"""
from __future__ import annotations

import re
from pathlib import Path

# The tag a shadow library appends to every file it serves. A textbook keeps it
# in each of its ~60 segment names, their spine notes and the hub's headings
# ("Deep Learning (Ian Goodfellow, Yoshua Bengio, Aaron Courville) (Z-Library)").
_DOWNLOAD_TAG_RE = re.compile(
    r"\s*[\(\[]\s*(?:z-?library|z-?lib(?:\.\w+)?|libgen(?:\.\w+)?|library genesis"
    r"|anna'?s[\s_-]archive|pdfdrive(?:\.com)?)\s*[\)\]]",
    re.IGNORECASE,
)


def store_key(path: str | None) -> str:
    """Graph ids carry `.md` and the stores do not: normalising at the store's
    own boundary means the two can no longer diverge into ghost keys. Idempotent."""
    return (path or "").replace("\\", "/").strip("/").removesuffix(".md")


def ledger_key(path) -> str:
    """Comparison key: the report's node ids carry `.md` and real-world case."""
    return str(path or "").replace("\\", "/").removesuffix(".md").lower()


def gate_key(path: str) -> str:
    # Case-fold before stripping the suffix so a hand-written "[[Foo.MD]]"
    # folds onto the same key as "Foo.md" (the old normalize_ref did not).
    return path.replace("\\", "/").strip("/").lower().removesuffix(".md")


def note_ref(path: str | None) -> str:
    """A note reference in the provenance ledger's own spelling: vault-relative
    POSIX, no `.md`. A caller may pass an absolute or `.md`-suffixed path, so
    relativize when possible and degrade to a plain strip otherwise."""
    ref = path or ""
    try:
        from silica.kernel.base.layout import to_vault_relative

        ref = to_vault_relative(ref, ensure_md=False)
    except Exception:
        ref = ref.replace("\\", "/").strip("/")
    return ref.removesuffix(".md")


def note_key(path: str | None) -> str:
    """The comparable key of a note reference: `note_ref`, casefolded.

    Public because the undo journal scopes a revert by the ledger's note set
    and must fold its own (possibly absolute) inverse paths the same way;
    two spellings of this rule would disagree on exactly the rename cases
    the ledger exists to survive.
    """
    return note_ref(path).casefold()


def stem(path: str) -> str:
    """A note's name: `Folder/Node.js.md` is `Node.js`. Not `Path.stem`, which
    takes `.js` off a store key that has already lost its `.md`."""
    return path.replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".md")


def source_stem(src: Path) -> str:
    """The source's name as its notes carry it: the file stem without a download
    site's tag. The same for a re-convert, as the image prefix needs."""
    return _DOWNLOAD_TAG_RE.sub("", src.stem).strip() or src.stem
