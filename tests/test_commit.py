"""Tests for the commit_ops micro-gate (silica/agent/commit.py)."""
from unittest.mock import patch

import pytest

from silica.agent.commit import commit_ops
from silica.agent.bounds import dedup_bounds
from silica.kernel.write.ops import Op, OpType
import silica.kernel.write.checkpoints as checkpoints


def _patch_op(path="Concepts/Big.md"):
    return Op(op=OpType.patch, heading="Concept", source_basename="i.md",
              path=path, snippet="appended info")


def test_commit_happy_path():
    with patch("silica.tools.composed.silica_validate_ops", return_value={"validated_count": 1, "success": True}), \
         patch("silica.tools.wrapped.silica_snapshot", return_value={"txn_id": "t1", "inverses": []}), \
         patch("silica.tools.composed.silica_bulk_write", return_value={"successful": 1, "total": 1, "failed": []}), \
         patch("silica.tools.composed.silica_lint", return_value={"success": True}), \
         patch("silica.tools.wrapped.silica_restore") as restore:
        res = commit_ops([_patch_op()], target_dir="Concepts")
    assert res["status"] == "committed"
    assert res["committed"] == 1
    restore.assert_not_called()


def test_commit_rolls_back_on_lint_failure():
    with patch("silica.tools.composed.silica_validate_ops", return_value={"validated_count": 1, "success": True}), \
         patch("silica.tools.wrapped.silica_snapshot", return_value={"txn_id": "t9", "inverses": [{"kind": "restore_version", "path": "Concepts/Big.md"}]}), \
         patch("silica.tools.composed.silica_bulk_write", return_value={"successful": 1, "total": 1, "failed": []}), \
         patch("silica.tools.composed.silica_lint", return_value={"success": False, "errors": ["bad frontmatter"]}), \
         patch("silica.tools.wrapped.silica_restore", return_value={"success": True}) as restore:
        res = commit_ops([_patch_op()], target_dir="Concepts")
    assert res["status"] == "rolled_back"
    assert res["lint_failures"]
    restore.assert_called_once()


def test_commit_bounds_drops_forbidden_op_before_write():
    # An overwrite is outside the dedup bounds → dropped before any tool runs.
    bounds = dedup_bounds("Concepts/Big.md")
    overwrite = Op(op=OpType.overwrite, heading="C", source_basename="i.md",
                   path="Concepts/Big.md", content="x")
    with patch("silica.tools.composed.silica_validate_ops") as validate:
        res = commit_ops([overwrite], target_dir="Concepts", bounds=bounds)
    # No actionable ops survived the bounds → validate never called.
    validate.assert_not_called()
    assert res["status"] == "no_ops"
    assert len(res["rejected_by_bounds"]) == 1


def test_commit_no_ops_when_all_skip():
    skip = Op(op=OpType.skip, heading="C", source_basename="i.md", reason="noop")
    res = commit_ops([skip], target_dir="X")
    assert res["status"] == "no_ops"


def test_delete_op_path_is_leased():
    """A delete op must acquire a path lease so concurrent writes are serialized."""
    from contextlib import contextmanager

    leased_paths = []

    @contextmanager
    def fake_path_lease(path):
        leased_paths.append(path)
        yield

    with patch("silica.kernel.workqueue.path_lease", side_effect=fake_path_lease), \
         patch("silica.tools.composed.silica_validate_ops",
               return_value={"validated_count": 1, "status": "ok", "rejected_ops": []}), \
         patch("silica.tools.wrapped.silica_snapshot", return_value={"txn_id": "t1", "inverses": []}), \
         patch("silica.tools.composed.silica_bulk_write", return_value={"successful": 1, "total": 1, "failed": []}), \
         patch("silica.tools.composed.silica_lint", return_value={"success": True}):
        delete_op = Op(op=OpType.delete, heading="Foo", source_basename="inbox.md", path="notes/Foo.md")
        commit_ops([delete_op], target_dir="notes")

    assert "notes/Foo.md" in leased_paths, (
        f"Delete op path 'notes/Foo.md' was not leased; got: {leased_paths}"
    )


def test_commit_unlinks_staging_file():
    """The ~/.silica/tmp ops file must not survive the call (no disk leak)."""
    from silica.kernel.recall.paths import silica_tmp_dir

    before = set(silica_tmp_dir().glob("*.json"))
    with patch("silica.tools.composed.silica_validate_ops", return_value={"validated_count": 1, "success": True}), \
         patch("silica.tools.wrapped.silica_snapshot", return_value={"txn_id": "t1", "inverses": []}), \
         patch("silica.tools.composed.silica_bulk_write", return_value={"successful": 1, "total": 1, "failed": []}), \
         patch("silica.tools.composed.silica_lint", return_value={"success": True}):
        commit_ops([_patch_op()], target_dir="Concepts")
    assert set(silica_tmp_dir().glob("*.json")) == before


# ---------------------------------------------------------------------------
# commit_derived — machine-generated notes with prior metadata preservation
# ---------------------------------------------------------------------------


@pytest.fixture
def derived_vault(tmp_path, monkeypatch):
    """Isolated fs vault for commit_derived tests; patches DRIVER + checkpoint store."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "Wiki.md").write_text(
        "---\ntags:\n  - custom\npinned: yes\n---\n\n# Wiki\n\nold\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SILICA_BACKEND", "fs")
    monkeypatch.setattr("silica.config.CONFIG.backend", "fs")
    monkeypatch.setattr("silica.config.CONFIG.vault_path", str(vault_dir))
    monkeypatch.setattr("silica.driver._driver", None)
    monkeypatch.setattr("silica.kernel.write.checkpoints._store", None)
    checkpoints.get_checkpoint_store(tmp_path / "checkpoints.db")
    yield vault_dir
    monkeypatch.setattr("silica.driver._driver", None)
    monkeypatch.setattr("silica.kernel.write.checkpoints._store", None)


def test_commit_derived_floors_bare_regen_with_prior_frontmatter(derived_vault):
    """A derived regen that omits the YAML block keeps the note's prior
    metadata and lands lint-green instead of rolling back."""
    from silica.agent.commit import commit_derived
    res = commit_derived("Wiki.md", "# Wiki\n\nregenerated\n")
    assert res["status"] == "committed", res
    landed = (derived_vault / "Wiki.md").read_text(encoding="utf-8")
    head = landed.split("\n---\n")[0]
    assert "tags:\n  - custom" in head and "pinned: yes" in head
    assert "AI: true" in head
    assert "regenerated" in landed and "old" not in landed


def test_commit_derived_first_write_gets_minimal_floor(derived_vault):
    from silica.agent.commit import commit_derived
    res = commit_derived("Fresh.md", "# Fresh\n\nbody\n")
    assert res["status"] == "committed", res
    landed = (derived_vault / "Fresh.md").read_text(encoding="utf-8")
    assert landed.startswith("---\nAI: true\nlast modified: ")


# --- #4: subagent-batch writes are revertable ------------------------------

def test_commit_journals_inverses_when_in_batch_run():
    """Inside a batch run (ctxvar set), a clean commit records its inverses so
    /revert can undo the pass."""
    from silica.agent.commit import _current_undo_run

    inverses = [{"kind": "restore_version", "path": "Concepts/Big.md"}]
    recorded = []

    class _FakeJournal:
        def record(self, run_id, inv, post_hash):
            recorded.append((run_id, inv.path, inv.kind.value))

    token = _current_undo_run.set("RUNX")
    try:
        with patch("silica.tools.composed.silica_validate_ops", return_value={"validated_count": 1, "success": True}), \
             patch("silica.tools.wrapped.silica_snapshot", return_value={"txn_id": "t1", "inverses": inverses}), \
             patch("silica.tools.composed.silica_bulk_write", return_value={"successful": 1, "total": 1, "failed": []}), \
             patch("silica.tools.composed.silica_lint", return_value={"success": True}), \
             patch("silica.kernel.write.undo_journal.get_undo_journal", return_value=_FakeJournal()), \
             patch("silica.driver.DRIVER.read_note", side_effect=RuntimeError("absent")):
            res = commit_ops([_patch_op()], target_dir="Concepts")
    finally:
        _current_undo_run.reset(token)

    assert res["status"] == "committed"
    assert recorded == [("RUNX", "Concepts/Big.md", "restore_version")]


def test_commit_does_not_journal_outside_batch_run():
    """Interactive callers (ctxvar unset) journal nothing — behaviour unchanged."""
    recorded = []

    class _FakeJournal:
        def record(self, *a):
            recorded.append(a)

    with patch("silica.tools.composed.silica_validate_ops", return_value={"validated_count": 1, "success": True}), \
         patch("silica.tools.wrapped.silica_snapshot", return_value={"txn_id": "t1", "inverses": [{"kind": "restore_version", "path": "x.md"}]}), \
         patch("silica.tools.composed.silica_bulk_write", return_value={"successful": 1, "total": 1, "failed": []}), \
         patch("silica.tools.composed.silica_lint", return_value={"success": True}), \
         patch("silica.kernel.write.undo_journal.get_undo_journal", return_value=_FakeJournal()):
        res = commit_ops([_patch_op()], target_dir="Concepts")

    assert res["status"] == "committed"
    assert recorded == []


# --- worker writes must be revertible and traceable (2026-08-18) -------------
# The Coordinator's in-run expand/dedup lane commits through commit_ops on pool
# threads. Contextvars do not cross a ThreadPoolExecutor boundary, so
# _current_undo_run was unset there and the `if undo_run_id:` guard silently
# skipped the journal; provenance was never appended at all. Measured on a
# 4-paper library gate: 3 of 39 notes had no inverse and no record.

def _write_op(path="Concepts/Ack.md"):
    return Op(op=OpType.write, heading="Ack", source_basename="3-rq2.md",
              path=path, snippet="body " * 40)


def _committed(fn):
    with patch("silica.tools.composed.silica_validate_ops", return_value={"validated_count": 1, "success": True}), \
         patch("silica.tools.wrapped.silica_snapshot",
               return_value={"txn_id": "t2", "inverses": [{"kind": "delete_created", "path": "Concepts/Ack.md"}]}), \
         patch("silica.tools.composed.silica_bulk_write", return_value={"successful": 1, "total": 1, "failed": []}), \
         patch("silica.tools.composed.silica_lint", return_value={"success": True}):
        return fn()


def test_worker_commit_appends_provenance_for_its_source(tmp_vault):
    from silica.agent import commit as commit_mod
    from silica.kernel.write.provenance import read_records

    tok = commit_mod._current_provenance.set(("3-rq2.md", "sha-abc"))
    try:
        res = _committed(lambda: commit_ops([_write_op()], target_dir="Concepts"))
    finally:
        commit_mod._current_provenance.reset(tok)

    assert res["status"] == "committed"
    rec = [r for r in read_records() if r.get("sha256") == "sha-abc"]
    assert rec, "worker write left no provenance record"
    assert rec[0]["source"] == "3-rq2.md"
    assert any("Concepts/Ack" in n for n in rec[0]["notes"])


def test_worker_commit_without_provenance_context_is_silent(tmp_vault):
    """An ad-hoc commit_ops (no work item behind it) must not invent a record."""
    from silica.kernel.write.provenance import read_records

    before = len(read_records())
    assert _committed(lambda: commit_ops([_write_op()], target_dir="Concepts"))["status"] == "committed"
    assert len(read_records()) == before


def _overwrite_op(path="Concepts/Ack.md"):
    return Op(op=OpType.overwrite, heading="Ack", source_basename="3-rq2.md",
              path=path, content="# Ack\n\n" + "body " * 40)


def test_worker_commit_records_an_overwrite(tmp_vault):
    """`overwrite` is the ONLY op four sub-agent profiles in agent/bounds.py may
    emit, and what all three dedup merges emit. The ledger predicate listed only
    write/patch, so the majority of the lane's writes reached the vault with no
    record: /revert walked past them and check_renucleate reported the source as
    never nucleated."""
    from silica.agent import commit as commit_mod
    from silica.kernel.write.provenance import read_records

    tok = commit_mod._current_provenance.set(("3-rq2.md", "sha-ow", "run-fsm-1"))
    try:
        with patch("silica.tools.composed.silica_validate_ops", return_value={"validated_count": 1, "success": True}), \
             patch("silica.tools.wrapped.silica_snapshot", return_value={"txn_id": "t3", "inverses": []}), \
             patch("silica.tools.composed.silica_bulk_write", return_value={"successful": 1, "total": 1, "failed": []}), \
             patch("silica.tools.composed.silica_lint", return_value={"success": True}):
            res = commit_ops([_overwrite_op()], target_dir="Concepts")
    finally:
        commit_mod._current_provenance.reset(tok)

    assert res["status"] == "committed"
    rec = [r for r in read_records() if r.get("sha256") == "sha-ow"]
    assert rec, "an overwrite left no provenance record"
    assert any("Concepts/Ack" in n for n in rec[0]["notes"])


def test_worker_provenance_is_keyed_on_the_fsm_run_not_the_journal_run(tmp_vault):
    """Every reader of this field — Coordinator._sweep_dangling_links, CLEANUP's
    own writer — matches on `fsm.progress.run_id`. The lane used to stamp the
    undo journal's uuid4 instead, a different keyspace, so the records matched
    nothing and the sweep still missed the notes they exist to attribute."""
    from silica.agent import commit as commit_mod
    from silica.kernel.write.provenance import read_records

    tok = commit_mod._current_provenance.set(("3-rq2.md", "sha-key", "run-fsm-2"))
    journal_tok = commit_mod._current_undo_run.set("undo-uuid-deadbeef")
    try:
        with patch("silica.tools.composed.silica_validate_ops", return_value={"validated_count": 1, "success": True}), \
             patch("silica.tools.wrapped.silica_snapshot", return_value={"txn_id": "t4", "inverses": []}), \
             patch("silica.tools.composed.silica_bulk_write", return_value={"successful": 1, "total": 1, "failed": []}), \
             patch("silica.tools.composed.silica_lint", return_value={"success": True}), \
             patch("silica.agent.commit._journal_inverses"):
            commit_ops([_write_op()], target_dir="Concepts")
    finally:
        commit_mod._current_undo_run.reset(journal_tok)
        commit_mod._current_provenance.reset(tok)

    rec = [r for r in read_records() if r.get("sha256") == "sha-key"]
    assert rec and rec[0]["run_id"] == "run-fsm-2"


def test_a_two_tuple_provenance_context_still_works(tmp_vault):
    """The run id is optional: older callers set (source, sha)."""
    from silica.agent import commit as commit_mod
    from silica.kernel.write.provenance import read_records

    tok = commit_mod._current_provenance.set(("3-rq2.md", "sha-2tuple"))
    try:
        _committed(lambda: commit_ops([_write_op()], target_dir="Concepts"))
    finally:
        commit_mod._current_provenance.reset(tok)
    assert [r for r in read_records() if r.get("sha256") == "sha-2tuple"]
