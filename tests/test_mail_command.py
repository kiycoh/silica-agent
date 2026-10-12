"""ADR-0090 "GUI and TUI": /mail in the REPL, a dry run until --apply."""
from __future__ import annotations

from pathlib import Path

import pytest

from silica.cli import _handle_direct_shortcut
from silica.sources import mail
from silica.ui.commands import COMMANDS, command_names
from tests.mail_fixtures import ME, ME_FULL, PROF, msg, write_mbox


@pytest.fixture
def box(tmp_vault, tmp_path):
    """One thread Ada wrote in, one newsletter; delivered to her. The space in
    the name is Takeout's: "All mail Including Spam and Trash.mbox"."""
    (tmp_path / "exports").mkdir()
    return write_mbox(tmp_path / "exports" / "my box.mbox", [
        msg(frm=ME_FULL, to=PROF, mid="<a1@x.example>", plain="Can we meet?", delivered_to=None),
        msg(mid="<a2@x.example>", irt="<a1@x.example>", refs=("<a1@x.example>",),
            subject="Re: Thesis draft", date="2026-09-02 09:00", plain="Thursday.", delivered_to=ME),
        msg(frm="Weekly <news@list.example>", mid="<n1@x.example>", subject="Offers",
            date="2026-09-03 09:00", plain="Buy.", delivered_to=ME),
    ])


def _notes(tmp_path) -> list[Path]:
    return sorted((tmp_path / "vault" / "sources" / "mail").glob("*.md"))


def test_mail_is_a_direct_command_beside_zotero():
    assert "/mail" in command_names()
    cmd = next(c for c in COMMANDS if c.name == "/mail")
    assert cmd.group == "direct"
    assert "import" in cmd.usage and "--apply" in cmd.usage


def test_the_dry_run_lists_the_threads_and_writes_nothing(box, tmp_path, capsys):
    assert _handle_direct_shortcut(f"/mail import {box}", []) is True
    out = capsys.readouterr().out
    assert "2 threads in the file" in out and "1 you took part in" in out
    assert "Thesis draft" in out and "Offers" not in out
    assert f"--me={ME}" in out            # the prefill, shown to confirm
    assert "--all adds 1" in out
    assert _notes(tmp_path) == []


def test_apply_waits_for_the_address_to_be_confirmed(box, tmp_path, capsys):
    _handle_direct_shortcut(f"/mail import {box} --apply", [])
    assert "Confirm your address" in capsys.readouterr().out
    assert _notes(tmp_path) == []


def test_apply_writes_the_kept_threads_in_one_revertible_run(box, tmp_path, capsys):
    _handle_direct_shortcut(f"/mail import {box} --me={ME} --apply", [])
    out = capsys.readouterr().out
    [note] = _notes(tmp_path)
    assert note.parent.name == "mail" and "Thesis draft" in note.name
    assert "Wrote 1 note" in out and "/revert" in out
    assert mail.addresses() == (ME,)
    _handle_direct_shortcut("/revert", [])
    assert _notes(tmp_path) == []


def test_status_names_the_addresses_and_the_imports(box, capsys):
    _handle_direct_shortcut("/mail", [])
    assert "none yet" in capsys.readouterr().out
    _handle_direct_shortcut(f"/mail import {box} --me={ME} --apply", [])
    capsys.readouterr()
    _handle_direct_shortcut("/mail", [])
    out = capsys.readouterr().out
    assert ME in out and "1 thread imported" in out and "my box.mbox" in out


def test_nucleate_of_an_eml_reaches_the_adapter_with_no_mail_branch(tmp_vault, tmp_path, capsys):
    from silica import cli
    from tests.mail_fixtures import write_eml

    mail.remember_addresses([ME])
    eml = write_eml(tmp_path / "thread.eml", msg(frm=ME_FULL, to=PROF, mid="<e@x.example>",
                                                 plain="Can we meet?"))
    assert cli._sc_nucleate([str(eml)]) == ""          # written inline, nothing for the agent
    [note] = _notes(tmp_path)
    assert "Thesis draft" in note.name


def test_nucleate_of_a_many_thread_mbox_points_to_mail_import(box, tmp_path, capsys):
    from silica import cli

    cli._sc_nucleate([str(box)])
    assert "/mail import" in capsys.readouterr().out
    assert _notes(tmp_path) == []


def test_a_subject_is_printed_as_text_never_as_console_markup(tmp_vault, tmp_path, capsys):
    """A stranger writes the subject: Rich markup in it must neither crash the
    dry run ("[/x]" is a MarkupError) nor become a live terminal link."""
    path = write_mbox(tmp_path / "x.mbox", [msg(
        frm=ME_FULL, mid="<m@x.example>", plain="x",
        subject="[/x] see [link=https://evil.example]here[/link]")])
    _handle_direct_shortcut(f"/mail import {path} --me={ME}", [])
    out = capsys.readouterr().out
    assert "[link=https://evil.example]here[/link]" in out


def test_an_unreadable_record_is_reported_not_raised(tmp_vault, capsys):
    mail._state_path().parent.mkdir(parents=True, exist_ok=True)
    mail._state_path().write_text("[]", encoding="utf-8")
    assert _handle_direct_shortcut("/mail", []) is True
    assert "not a mail import record" in capsys.readouterr().out
