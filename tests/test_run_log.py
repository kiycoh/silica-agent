"""Tests for silica.kernel.recall.run_log — the human-readable <vault>/log.md journal.

Pure kernel helper: format the nucleate-completion event, append idempotently
per run_id, and read back the tail for vault-map injection.
"""
from __future__ import annotations

from pathlib import Path

from silica.kernel.recall.run_log import (
    DEFAULT_LOG_FILENAME,
    append_log_line,
    format_nucleate_event,
    tail_log,
)


def test_format_nucleate_event_matches_brief_shape():
    assert format_nucleate_event("lezione-03.md", 7, 3, 2) == (
        "nucleate `lezione-03.md` → 7 new, 3 patch, 2 deferred"
    )


def test_append_creates_file(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    log_path = vault / DEFAULT_LOG_FILENAME
    assert not log_path.exists()

    ok = append_log_line(
        "nucleate `a.md` → 1 new, 0 patch, 0 deferred",
        "deadbeef1234",
        vault_path=str(vault),
    )

    assert ok is True
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert content.startswith("- ")
    assert "nucleate `a.md`" in content
    assert "run deadbeef" in content


def test_two_appends_two_lines_in_order(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()

    append_log_line(
        "nucleate `a.md` → 1 new, 0 patch, 0 deferred",
        "runidone1234",
        vault_path=str(vault),
    )
    append_log_line(
        "nucleate `b.md` → 2 new, 0 patch, 0 deferred",
        "runidtwo5678",
        vault_path=str(vault),
    )

    lines = (vault / DEFAULT_LOG_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "a.md" in lines[0]
    assert "b.md" in lines[1]


def test_same_run_id_idempotent_no_duplicate(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()

    first = append_log_line(
        "nucleate `a.md` → 1 new, 0 patch, 0 deferred",
        "samerunid123",
        vault_path=str(vault),
    )
    second = append_log_line(
        "nucleate `a.md` → 1 new, 0 patch, 0 deferred",
        "samerunid123",
        vault_path=str(vault),
    )

    assert first is True
    assert second is False
    lines = (vault / DEFAULT_LOG_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_same_run_id_different_dedup_keys_appends_both(tmp_path):
    """Multi-file run: one run_id, one line per file. dedup_key scopes the
    idempotency check to (run_id, key); re-appending the same key is a no-op."""
    vault = tmp_path / "vault"
    vault.mkdir()

    first = append_log_line(
        "nucleate `a.md` → 1 new, 0 patch, 0 deferred",
        "sharedrunid1",
        vault_path=str(vault),
        dedup_key="`a.md`",
    )
    second = append_log_line(
        "nucleate `b.md` → 2 new, 0 patch, 0 deferred",
        "sharedrunid1",
        vault_path=str(vault),
        dedup_key="`b.md`",
    )
    resumed = append_log_line(  # resume of file a under the same run
        "nucleate `a.md` → 1 new, 0 patch, 0 deferred",
        "sharedrunid1",
        vault_path=str(vault),
        dedup_key="`a.md`",
    )

    assert first is True
    assert second is True
    assert resumed is False
    lines = (vault / DEFAULT_LOG_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "a.md" in lines[0]
    assert "b.md" in lines[1]


def test_missing_vault_path_is_noop(monkeypatch):
    import silica.config as config_mod

    monkeypatch.setattr(config_mod.CONFIG, "vault_path", "")
    ok = append_log_line("event", "runid12345678")
    assert ok is False


def test_append_falls_back_to_config_vault_path(tmp_vault):
    from silica.config import CONFIG

    ok = append_log_line("nucleate `a.md` → 1 new, 0 patch, 0 deferred", "cfgrunid1234")

    assert ok is True
    assert (Path(CONFIG.vault_path) / DEFAULT_LOG_FILENAME).exists()


def test_tail_log_returns_last_n_lines(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    for i in range(7):
        append_log_line(
            f"nucleate `f{i}.md` → 1 new, 0 patch, 0 deferred",
            f"run{i:05d}abc",
            vault_path=str(vault),
        )

    tail = tail_log(5, vault_path=str(vault))

    assert len(tail) == 5
    assert "f2.md" in tail[0]
    assert "f6.md" in tail[-1]


def test_tail_log_missing_file_returns_empty(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    assert tail_log(5, vault_path=str(vault)) == []


def test_format_revert_event_names_source_and_counts():
    """log.md said "8 new notes" after a /revert had taken 4 of them back out —
    the journal must narrate the revert too, or it lies by omission."""
    from silica.kernel.recall.run_log import format_revert_event

    assert format_revert_event("nucleate", 4, 6) == \
        "revert (nucleate) → 4 note(s) restored, 6 kept (modified since)"
    assert format_revert_event("", 2, 0) == "revert → 2 note(s) restored"


# --- write boundary ---------------------------------------------------------
#
# Pointing Silica at a 5 GB library of scanned books (write_dir: silica) left a
# `log.md` in the library root next to the user's own README.md and INDEX.md.
# The journal is something Silica creates, so it belongs inside the boundary.

def test_append_lands_inside_write_dir(tmp_path, monkeypatch):
    import silica.config
    import silica.kernel.vault_manifest as vm

    vault = tmp_path / "vault"
    (vault / "silica").mkdir(parents=True)
    monkeypatch.setattr(silica.config.CONFIG, "vault_path", str(vault))
    monkeypatch.setattr(vm, "active_write_dir", lambda: "silica")

    append_log_line("nucleate `a.md` → 1 new, 0 patch, 0 deferred", "deadbeef1234",
                    vault_path=str(vault))

    assert (vault / "silica" / DEFAULT_LOG_FILENAME).exists()
    assert not (vault / DEFAULT_LOG_FILENAME).exists()


def test_tail_still_reads_a_legacy_root_log(tmp_path, monkeypatch):
    """Vaults written before the boundary fix keep their journal at the root."""
    import silica.config
    import silica.kernel.vault_manifest as vm

    vault = tmp_path / "vault"
    (vault / "silica").mkdir(parents=True)
    (vault / DEFAULT_LOG_FILENAME).write_text("- 2026-01-01 · old · run abc\n",
                                              encoding="utf-8")
    monkeypatch.setattr(silica.config.CONFIG, "vault_path", str(vault))
    monkeypatch.setattr(vm, "active_write_dir", lambda: "silica")

    assert tail_log(5, vault_path=str(vault)) == ["- 2026-01-01 · old · run abc"]


def test_provenance_store_also_lands_inside_write_dir(tmp_path, monkeypatch):
    """Same leak as the journal: pointing Silica at a library dropped both
    `log.md` and `provenance.json` in the user's root."""
    import silica.config
    import silica.kernel.vault_manifest as vm
    from silica.kernel.write.provenance import DEFAULT_PROVENANCE_FILENAME, _store_path

    vault = tmp_path / "vault"
    (vault / "silica").mkdir(parents=True)
    monkeypatch.setattr(silica.config.CONFIG, "vault_path", str(vault))
    monkeypatch.setattr(vm, "active_write_dir", lambda: "silica")

    assert _store_path(str(vault)) == vault / "silica" / DEFAULT_PROVENANCE_FILENAME


def test_provenance_store_keeps_a_legacy_root_file(tmp_path, monkeypatch):
    import silica.config
    import silica.kernel.vault_manifest as vm
    from silica.kernel.write.provenance import DEFAULT_PROVENANCE_FILENAME, _store_path

    vault = tmp_path / "vault"
    (vault / "silica").mkdir(parents=True)
    (vault / DEFAULT_PROVENANCE_FILENAME).write_text("[]", encoding="utf-8")
    monkeypatch.setattr(silica.config.CONFIG, "vault_path", str(vault))
    monkeypatch.setattr(vm, "active_write_dir", lambda: "silica")

    assert _store_path(str(vault)) == vault / DEFAULT_PROVENANCE_FILENAME
