# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0179: a path the model names never leaves the vault, and a path the
person types leaves it only on their own computer. The probes of 2026-11-14,
each turned around: a folder `outside/` beside the vault holds a marker."""
from __future__ import annotations

from pathlib import Path

import pytest

from silica.config import CONFIG

MARK = "s3cr3t"


@pytest.fixture
def outside(tmp_vault, tmp_path) -> Path:
    d = tmp_path / "outside"
    d.mkdir()
    (d / "data.csv").write_text(f"key,value\nmark,{MARK}\n", encoding="utf-8")
    (d / "cookies.txt").write_text(f"cookie {MARK}\n", encoding="utf-8")
    (Path(CONFIG.vault_path) / "Inbox").mkdir()
    return d


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("SILICA_HOSTED", "1")


def _vault_holds_mark() -> bool:
    return any(MARK in p.read_text(encoding="utf-8", errors="replace")
               for p in Path(CONFIG.vault_path).rglob("*") if p.is_file())


def test_the_model_cannot_convert_a_file_beside_the_vault(outside, monkeypatch):
    from silica.sources import ingest

    def _no(*a, **k):
        raise AssertionError("converted a file the model named outside the vault")

    monkeypatch.setattr("silica.sources.convert.convert", _no)
    for named in ("../outside/data.csv", str(outside / "data.csv"), str(outside / "cookies.txt")):
        res = ingest.run_injector([named], "T")
        assert "outside the vault" in res["error"]
    assert not _vault_holds_mark()


def test_a_bare_name_the_model_gives_is_read_in_the_vault_not_the_cwd(outside, monkeypatch):
    """The check took `data.csv` against the vault, where it is not, and
    convert() then resolved it again and fell back to the cwd beside it."""
    from silica.sources import ingest

    def _no(*a, **k):
        raise AssertionError("distilled a file found in the cwd")

    monkeypatch.setattr(ingest, "distill", _no)
    monkeypatch.chdir(outside)
    res = ingest.run_injector(["data.csv"], "T")
    assert "file not found" in res["error"]
    assert not _vault_holds_mark()


def test_a_folder_listing_stays_in_the_vault(outside):
    from silica.kernel.recall.listing import unconverted_under, vault_files_under

    assert unconverted_under("../outside") == []
    assert vault_files_under("../outside") == []


def test_a_text_file_is_read_from_the_vault_only(outside):
    from silica.kernel.forms import read_source_text
    from silica.kernel.write.provenance import content_sha256
    from silica.sources.prose import PROSE

    with pytest.raises(ValueError, match="outside the vault"):
        PROSE.read("../outside/cookies.txt")
    with pytest.raises(ValueError, match="outside the vault"):
        read_source_text(str(outside / "cookies.txt"))
    assert content_sha256(str(outside / "cookies.txt")) == ""
    # A .txt in the inbox still reads: the fallback exists for it.
    (Path(CONFIG.vault_path) / "Inbox" / "talk.txt").write_text("spoken words\n", encoding="utf-8")
    assert read_source_text("Inbox/talk.txt") == "spoken words\n"
    assert PROSE.read("Inbox/talk.txt").text == "spoken words\n"


def test_read_note_keeps_its_refusal(outside):
    from silica.driver import DRIVER

    with pytest.raises(RuntimeError, match=r"is outside the vault"):
        DRIVER.read_note(str(outside / "cookies.txt"))


def test_on_their_own_computer_the_person_still_converts_a_file_beside_the_vault(outside):
    """The owner's call, 2026-11-14: a PDF sits where the person stands."""
    from silica.sources.convert import convert

    assert convert("../outside/data.csv") == ["Inbox/data.md"]


def test_on_the_server_a_typed_path_stays_in_the_vault(outside, server):
    from silica.sources import ingest
    from silica.sources.convert import OUTSIDE_YOUR_VAULT, OutsideVault, convert, resolve_input
    from silica.ui.web import jobs

    for typed in ("../outside/data.csv", str(outside / "data.csv"), str(outside / "nothing.pdf")):
        with pytest.raises(OutsideVault):
            resolve_input(typed)  # before the existence check: nothing said about the host
    with pytest.raises(OutsideVault):
        convert("../outside/data.csv")
    # A file missing from the vault is still "not found", not "outside".
    with pytest.raises(ValueError, match="file not found") as missing:
        resolve_input("Inbox/missing.pdf")
    assert not isinstance(missing.value, OutsideVault)

    queued = jobs.from_command("/convert ../outside/data.csv ../outside", None)
    assert queued["jobs"] == []
    assert {s["name"]: s["reason"] for s in queued["skipped"]} == {
        "../outside/data.csv": OUTSIDE_YOUR_VAULT, "../outside": OUTSIDE_YOUR_VAULT}

    from rich.console import Console

    assert ingest._nucleate_globs(["../outside/*"], Console(quiet=True)) == ([], True)
    assert not _vault_holds_mark()
