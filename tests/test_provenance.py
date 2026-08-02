"""Tests for silica/kernel/write/provenance.py (spec-hermes-coherence §3).

Note<->source drift via sha256 provenance records. Pure filesystem module
(stdlib json/hashlib/datetime), mirroring silica/kernel/recall/run_log.py: append
is best-effort (never raises), absence of the store degrades to "no
records" everywhere.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from silica.kernel.write.provenance import (
    DEFAULT_PROVENANCE_FILENAME,
    append_record,
    check_renucleate,
    content_sha256,
    drifted_notes,
    read_records,
)


# ---------------------------------------------------------------------------
# append_record / read_records — record shape + append-only behaviour
# ---------------------------------------------------------------------------

def test_append_record_writes_exact_shape(tmp_path):
    ok = append_record(
        "lezione-03.md", "sha-v1", "run-abc123", ["Concepts/A", "Concepts/B"],
        vault_path=str(tmp_path), date="2026-07-02",
    )
    assert ok is True

    raw = json.loads((tmp_path / DEFAULT_PROVENANCE_FILENAME).read_text(encoding="utf-8"))
    assert raw == [{
        "source": "lezione-03.md",
        "sha256": "sha-v1",
        "run_id": "run-abc123",
        "date": "2026-07-02",
        "notes": ["Concepts/A", "Concepts/B"],
    }]


def test_append_record_resume_same_run_source_sha_is_idempotent(tmp_path):
    """A resumed run re-firing CLEANUP for the same file hits this seam again
    with the same (source, sha256, run_id) — must not duplicate the record."""
    append_record("a.md", "sha1", "run1", ["N1"], vault_path=str(tmp_path))
    append_record("a.md", "sha1", "run1", ["N1"], vault_path=str(tmp_path))

    records = read_records(vault_path=str(tmp_path))
    assert len(records) == 1


def test_append_record_is_append_only(tmp_path):
    append_record("a.md", "sha1", "r1", ["N1"], vault_path=str(tmp_path))
    append_record("a.md", "sha2", "r2", ["N2"], vault_path=str(tmp_path))

    records = read_records(vault_path=str(tmp_path))
    assert len(records) == 2
    assert [r["sha256"] for r in records] == ["sha1", "sha2"]


def test_read_records_filters_by_source(tmp_path):
    append_record("a.md", "sha1", "r1", ["N1"], vault_path=str(tmp_path))
    append_record("b.md", "sha1", "r1", ["N2"], vault_path=str(tmp_path))

    records = read_records("a.md", vault_path=str(tmp_path))
    assert len(records) == 1
    assert records[0]["source"] == "a.md"


def test_read_records_missing_file_returns_empty(tmp_path):
    assert read_records(vault_path=str(tmp_path)) == []


def test_read_records_corrupt_file_returns_empty(tmp_path):
    (tmp_path / DEFAULT_PROVENANCE_FILENAME).write_text("{not json", encoding="utf-8")
    assert read_records(vault_path=str(tmp_path)) == []


def test_append_record_no_vault_path_returns_false(monkeypatch):
    import silica.config as config_mod
    monkeypatch.setattr(config_mod.CONFIG, "vault_path", "")
    assert append_record("a.md", "sha1", "r1", ["N1"]) is False


def test_append_record_survives_unwritable_store(tmp_path, monkeypatch):
    """Best-effort: an I/O failure on write must not raise."""
    import silica.kernel.recall.paths as paths_mod

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(paths_mod, "atomic_write_bytes", _boom)
    ok = append_record("a.md", "sha1", "r1", ["N1"], vault_path=str(tmp_path))
    assert ok is False


# ---------------------------------------------------------------------------
# drifted_notes — the drift rule from the spec
# ---------------------------------------------------------------------------

def test_no_provenance_file_no_drift(tmp_path):
    assert drifted_notes(vault_path=str(tmp_path)) == []


def test_single_version_no_drift(tmp_path):
    append_record("a.md", "sha1", "r1", ["Nota A", "Nota B"], vault_path=str(tmp_path))
    assert drifted_notes(vault_path=str(tmp_path)) == []


def test_v2_touching_half_the_notes_drifts_the_other_half(tmp_path):
    """Acceptance criterion: nucleate v1 (A,B) -> modify -> re-nucleate v2 (A only)
    -> B is drifted, A is not."""
    append_record("lezione-03.md", "sha-v1", "r1", ["Nota A", "Nota B"], vault_path=str(tmp_path))
    append_record("lezione-03.md", "sha-v2", "r2", ["Nota A"], vault_path=str(tmp_path))

    drift = drifted_notes(vault_path=str(tmp_path))
    assert drift == [("Nota B", "lezione-03.md")]


def test_note_untouched_by_v2_but_present_in_v2_is_not_drifted(tmp_path):
    append_record("a.md", "sha1", "r1", ["A", "B"], vault_path=str(tmp_path))
    append_record("a.md", "sha2", "r2", ["A", "B"], vault_path=str(tmp_path))
    assert drifted_notes(vault_path=str(tmp_path)) == []


def test_drift_scoped_per_source(tmp_path):
    append_record("a.md", "sha1", "r1", ["A1"], vault_path=str(tmp_path))
    append_record("a.md", "sha2", "r2", [], vault_path=str(tmp_path))
    append_record("b.md", "shaB", "r3", ["B1"], vault_path=str(tmp_path))

    drift = drifted_notes(vault_path=str(tmp_path))
    assert drift == [("A1", "a.md")]


def test_v2_touching_nothing_drifts_all_v1_notes(tmp_path):
    """A re-nucleate whose sha changed but produced zero write/patch ops still
    means every v1 note is now stale relative to the new version."""
    append_record("a.md", "sha1", "r1", ["A", "B"], vault_path=str(tmp_path))
    append_record("a.md", "sha2", "r2", [], vault_path=str(tmp_path))

    drift = drifted_notes(vault_path=str(tmp_path))
    assert sorted(drift) == [("A", "a.md"), ("B", "a.md")]


# ---------------------------------------------------------------------------
# check_renucleate — the /nucleate warning seam
# ---------------------------------------------------------------------------

def test_check_renucleate_no_prior_record_no_warning(tmp_path):
    modified, count = check_renucleate("new-source.md", "sha1", vault_path=str(tmp_path))
    assert modified is False
    assert count == 0


def test_check_renucleate_same_sha_no_warning(tmp_path):
    append_record("a.md", "sha1", "r1", ["A", "B"], vault_path=str(tmp_path))
    modified, count = check_renucleate("a.md", "sha1", vault_path=str(tmp_path))
    assert modified is False
    assert count == 0


def test_check_renucleate_different_sha_warns_with_prior_note_count(tmp_path):
    append_record("a.md", "sha1", "r1", ["A", "B"], vault_path=str(tmp_path))
    modified, count = check_renucleate("a.md", "sha2", vault_path=str(tmp_path))
    assert modified is True
    assert count == 2


def test_check_renucleate_uses_most_recent_record(tmp_path):
    append_record("a.md", "sha1", "r1", ["A"], vault_path=str(tmp_path))
    append_record("a.md", "sha2", "r2", ["A", "B"], vault_path=str(tmp_path))
    modified, count = check_renucleate("a.md", "sha2", vault_path=str(tmp_path))
    assert modified is False
    assert count == 0

    modified2, count2 = check_renucleate("a.md", "sha3", vault_path=str(tmp_path))
    assert modified2 is True
    assert count2 == 2


# ---------------------------------------------------------------------------
# content_sha256 — matches orchestrator.run()'s hashing exactly (hash parity
# between the CLEANUP write side and the /nucleate pre-check read side)
# ---------------------------------------------------------------------------

def test_content_sha256_matches_manual_hash(tmp_path, monkeypatch):
    import hashlib
    import silica.config as config_mod
    from silica.driver import fs_backend
    import silica.driver as driver_mod

    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "Inbox" / "a.md"
    note.parent.mkdir(parents=True)
    note.write_text("hello world", encoding="utf-8")

    monkeypatch.setattr(config_mod.CONFIG, "vault_path", str(vault))
    monkeypatch.setattr(driver_mod, "DRIVER", fs_backend.ObsidianFSBackend(str(vault)))

    expected = hashlib.sha256("hello world".encode("utf-8")).hexdigest()
    assert content_sha256("Inbox/a.md") == expected


def test_content_sha256_missing_file_returns_empty(tmp_path, monkeypatch):
    import silica.config as config_mod
    from silica.driver import fs_backend
    import silica.driver as driver_mod

    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(config_mod.CONFIG, "vault_path", str(vault))
    monkeypatch.setattr(driver_mod, "DRIVER", fs_backend.ObsidianFSBackend(str(vault)))

    assert content_sha256("Inbox/does-not-exist.md") == ""


# ---------------------------------------------------------------------------
# read_records memo (bulk._execute_patch calls this once per patch op)
# ---------------------------------------------------------------------------

def test_read_records_parses_once_for_repeated_reads(tmp_path, monkeypatch):
    append_record("a.md", "sha1", "r1", ["N1"], vault_path=str(tmp_path))

    import silica.kernel.write.provenance as prov

    parses = 0
    real_loads = prov.json.loads

    def counting_loads(*a, **kw):
        nonlocal parses
        parses += 1
        return real_loads(*a, **kw)

    monkeypatch.setattr(prov.json, "loads", counting_loads)
    for _ in range(5):
        assert len(read_records(vault_path=str(tmp_path))) == 1
    assert parses == 1


def test_read_records_memo_invalidates_on_write(tmp_path):
    append_record("a.md", "sha1", "r1", ["N1"], vault_path=str(tmp_path))
    assert len(read_records(vault_path=str(tmp_path))) == 1

    append_record("b.md", "sha2", "r2", ["N2"], vault_path=str(tmp_path))
    assert len(read_records(vault_path=str(tmp_path))) == 2

    # an out-of-process writer must be seen too: rewrite the file behind our back
    store = tmp_path / DEFAULT_PROVENANCE_FILENAME
    store.write_text(json.dumps([]), encoding="utf-8")
    assert read_records(vault_path=str(tmp_path)) == []


def test_read_records_caller_cannot_poison_the_memo(tmp_path):
    """append_record mutates the list it gets back; the memo must not see it."""
    append_record("a.md", "sha1", "r1", ["N1"], vault_path=str(tmp_path))

    first = read_records(vault_path=str(tmp_path))
    first.append({"source": "ghost.md"})

    assert [r["source"] for r in read_records(vault_path=str(tmp_path))] == ["a.md"]


def test_append_is_atomic_a_failed_write_keeps_the_prior_ledger(tmp_path, monkeypatch):
    """A crash mid-rewrite must not truncate the store.

    This ledger is authoritative — run_id/sha history is not reconstructible
    from the vault, and read_records quarantines a truncated file — so a torn
    write silently loses every prior record.
    """
    import os

    append_record("a.md", "sha1", "r1", ["N1"], vault_path=str(tmp_path))
    store = tmp_path / DEFAULT_PROVENANCE_FILENAME
    before = store.read_bytes()

    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk full")))
    assert append_record("b.md", "sha2", "r2", ["N2"], vault_path=str(tmp_path)) is False

    assert store.read_bytes() == before
    assert list(tmp_path.iterdir()) == [store]  # no tmp leftovers


def test_concurrent_appends_lose_no_records(tmp_path, monkeypatch):
    """read->append->replace was atomic per write but not per window: two
    parallel nucleate workers each rewrote the ledger from their own read and
    the last one won. The lease serializes the window."""
    import threading

    from silica.kernel.write import provenance

    vault = tmp_path / "vault"
    vault.mkdir()
    barrier = threading.Barrier(8)

    def _append(i):
        barrier.wait()
        provenance.append_record(
            source=f"inbox/s{i}.md", sha256=f"sha{i}", run_id=f"run{i}",
            notes=[f"N{i}.md"], vault_path=str(vault))

    threads = [threading.Thread(target=_append, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    records = provenance.read_records(vault_path=str(vault))
    assert len(records) == 8
