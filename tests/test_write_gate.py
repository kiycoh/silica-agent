"""The gate's own seams: journaled_write, record_landed, land_batch, in_run."""
import logging

import pytest

from silica.kernel import contested_register
from silica.kernel.write import gate, undo_journal
from silica.kernel.write.ops import InverseOp, InverseOpKind, Op, OpType
from silica.kernel.write.undo_journal import UndoJournalStore, content_hash


@pytest.fixture
def journal(tmp_path, monkeypatch):
    store = UndoJournalStore(tmp_path / "j.db")
    monkeypatch.setattr(undo_journal, "get_undo_journal", lambda: store)
    return store


def test_journaled_write_records_the_read_back_and_undoes_to_the_prior_bytes(tmp_vault, journal):
    from silica.driver import DRIVER

    tmp_vault.note("N.md", "---\nAI: true\n---\nbefore\n")
    run = gate.journaled_write("N.md", lambda t: t.replace("before", "after"), source="learn")

    [(inv, post_hash)] = journal.inverses_for(run)
    assert inv.kind == InverseOpKind.restore_version and "before" in inv.prior_content
    assert post_hash == content_hash(DRIVER.read_note("N.md").content)
    assert gate.revert_run(run, store=journal)["reverted"] == ["N.md"]
    assert "before" in DRIVER.read_note("N.md").content


def test_a_write_that_changes_nothing_opens_no_run(tmp_vault, journal):
    tmp_vault.note("N.md", "---\nAI: true\n---\nsame\n")
    assert gate.journaled_write("N.md", lambda t: t, source="learn") == ""
    assert journal.last_active_run() is None


def test_journaled_write_joins_the_run_it_is_given(tmp_vault, journal):
    tmp_vault.note("A.md", "---\nAI: true\n---\na\n")
    tmp_vault.note("B.md", "---\nAI: true\n---\nb\n")
    run = gate.journaled_write("A.md", lambda t: t + "x\n", source="migrate")
    assert gate.journaled_write("B.md", lambda t: t + "y\n", source="migrate", run=run) == run
    assert {inv.path for inv, _ in journal.inverses_for(run)} == {"A.md", "B.md"}


def test_record_landed_says_when_the_revert_guard_is_lost(tmp_vault, journal, caplog):
    run = journal.start_run("x")
    with caplog.at_level(logging.WARNING, logger="silica.kernel.write.gate"):
        gate.record_landed(run, gate.undo_of("Gone.md", None))
    assert journal.inverses_for(run)[0][1] is None
    assert "/revert guard disabled" in caplog.text


def test_record_landed_is_quiet_about_a_delete_and_hashes_a_move_where_it_landed(tmp_vault, journal, caplog):
    landed = tmp_vault.note("To.md", "---\nAI: true\n---\nmoved\n")
    run = journal.start_run("x")
    with caplog.at_level(logging.WARNING, logger="silica.kernel.write.gate"):
        gate.record_landed(run, InverseOp(kind=InverseOpKind.recreate_deleted, path="Gone.md",
                                          prior_content="x"))
    assert caplog.text == ""
    gate.record_landed(run, InverseOp(kind=InverseOpKind.move_back, path="From.md", to_path="To.md"))
    assert journal.inverses_for(run)[0][1] == content_hash(tmp_vault.read(landed))


def _write(path, heading):
    return Op(op=OpType.write, heading=heading, source_basename="s.md", path=path,
              snippet="body", hub="Hub")


def test_a_batch_that_may_not_be_partial_rolls_back_what_did_land(tmp_vault):
    taken = tmp_vault.note("Taken.md", "---\nAI: true\n---\nheld\n")
    ops = [_write("Fresh.md", "Fresh"), _write("Taken.md", "Taken")]  # create refuses the second

    landing = gate.land_batch(ops, lint=False, partial_ok=False)

    assert landing.failure == {"error": "1 of 2 write ops failed"}
    assert not gate.vault_file("Fresh.md").exists()
    assert "held" in tmp_vault.read(taken)


def test_a_batch_that_may_be_partial_stands_on_one_landed_op(tmp_vault):
    tmp_vault.note("Taken.md", "---\nAI: true\n---\nheld\n")
    ops = [_write("Fresh.md", "Fresh"), _write("Taken.md", "Taken")]

    landing = gate.land_batch(ops, lint=False, partial_ok=True)

    assert landing.failure is None and landing.bulk.successful == 1
    assert gate.vault_file("Fresh.md").exists()


def test_a_contradiction_reaches_the_digest_worklist_from_where_it_is_written(tmp_vault):
    tmp_vault.note("Claim.md", "---\nAI: true\nhub: \"[[Hub]]\"\n---\nA claim, [[Hub]].\n")
    op = Op(op=OpType.patch, heading="Claim", source_basename="s.md", path="Claim.md",
            snippet="The opposite claim.", contested_by="source: s.md")

    from silica.kernel.write.bulk import execute_one
    execute_one(op)

    assert "Claim.md" in contested_register.entries()


def test_in_run_scopes_the_ambient_run_and_leaves_what_it_was_not_given():
    with gate.in_run(undo="outer"):
        with gate.in_run(ledger="inner"):
            assert gate._current_undo_run.get() == "outer"
            assert gate._current_ledger_run.get() == "inner"
        assert gate._current_ledger_run.get() is None
    assert gate._current_undo_run.get() is None
