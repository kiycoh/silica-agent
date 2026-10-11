"""A delete the user ran with /delete is journaled apart from a chat delete.

ADR-0092 point 2: a linked delete is the user's act, so it lands under its own
source. /undo finds either kind; ADR-0093's re-create check reads only the
chat's own deletes, so `last_chat_delete` stays chat-only.
"""
from __future__ import annotations

from silica.kernel.write.ops import InverseOp, InverseOpKind
from silica.kernel.write.undo_journal import (
    ABSENT_HASH,
    CHAT_DELETE_SOURCE,
    USER_DELETE_SOURCE,
    UndoJournalStore,
)


def _journal_delete(store: UndoJournalStore, source: str, path: str) -> str:
    run_id = store.start_run(source=source, vault="/v")
    store.record(run_id, InverseOp(kind=InverseOpKind.recreate_deleted, path=path,
                                   prior_content="body"), post_hash=ABSENT_HASH)
    return run_id


def test_the_user_delete_source_is_its_own_string():
    assert USER_DELETE_SOURCE == "user:/delete"
    assert USER_DELETE_SOURCE != CHAT_DELETE_SOURCE


def test_last_delete_finds_a_user_delete_and_a_chat_delete(tmp_path):
    store = UndoJournalStore(tmp_path / "uj.db")
    chat = _journal_delete(store, CHAT_DELETE_SOURCE, "A.md")
    user = _journal_delete(store, USER_DELETE_SOURCE, "B.md")

    assert store.last_delete("/v", "B")[0] == user
    assert store.last_delete("/v", "A.md")[0] == chat
    assert store.last_delete("/v")[0] == user  # the newest of either


def test_last_chat_delete_never_returns_a_user_delete(tmp_path):
    store = UndoJournalStore(tmp_path / "uj.db")
    chat = _journal_delete(store, CHAT_DELETE_SOURCE, "A.md")
    _journal_delete(store, USER_DELETE_SOURCE, "B.md")

    assert store.last_chat_delete("/v", "B.md") is None
    assert store.last_chat_delete("/v")[0] == chat
