from pathlib import Path

from silica.kernel.migrate import migrate_adr_namespace


def test_migrate_moves_adrs_and_leaves_tombstone(tmp_path):
    old = tmp_path / "docs" / "adr"
    old.mkdir(parents=True)
    (old / "0006-curation.md").write_text("# ADR 0006\n", encoding="utf-8")
    (old / "0009-zero-trust.md").write_text("# ADR 0009\n", encoding="utf-8")

    moved = migrate_adr_namespace(tmp_path)

    new = tmp_path / "docs" / "silica" / "adr"
    assert (new / "0006-curation.md").is_file()
    assert (new / "0009-zero-trust.md").is_file()
    assert sorted(moved) == ["adr/0006-curation.md", "adr/0009-zero-trust.md"]
    # vault-relative link [[adr/0006-curation]] resolves to the moved file, content intact
    assert (new / "0006-curation.md").read_text(encoding="utf-8") == "# ADR 0006\n"
    # tombstone left at the old location for external references
    assert (tmp_path / "docs" / "adr" / "MOVED.md").is_file()


def test_migrate_noop_when_no_old_adr(tmp_path):
    assert migrate_adr_namespace(tmp_path) == []


def test_migrate_never_overwrites_existing_destination(tmp_path):
    old = tmp_path / "docs" / "adr"
    old.mkdir(parents=True)
    (old / "0006-curation.md").write_text("# new source\n", encoding="utf-8")
    new = tmp_path / "docs" / "silica" / "adr"
    new.mkdir(parents=True)
    (new / "0006-curation.md").write_text("# existing dest\n", encoding="utf-8")

    moved = migrate_adr_namespace(tmp_path)

    # Collision: destination is preserved, source is left as a signal, not moved.
    assert moved == []
    assert (new / "0006-curation.md").read_text(encoding="utf-8") == "# existing dest\n"
    assert (old / "0006-curation.md").is_file()
