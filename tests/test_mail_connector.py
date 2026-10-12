"""ADR-0090 "GUI and TUI": the Mail row of Settings › Connectors (the shell is
ADR-0089 point 8's). The block answers the panel; the file arrives through the
inbox inside the write boundary before the dry run."""
from __future__ import annotations

import pytest

from silica.sources import mail
from silica.sources.mail_connector import MAIL_BLOCK
from tests.mail_fixtures import ME, ME_FULL, PROF, msg, write_mbox


@pytest.fixture
def inbox_box(tmp_vault, tmp_path) -> str:
    """An export the GUI uploaded into the inbox: one thread Ada took part in,
    one newsletter. Returns its vault-relative path, as the upload route does."""
    (tmp_path / "vault" / "Inbox").mkdir()
    write_mbox(tmp_path / "vault" / "Inbox" / "export.mbox", [
        msg(frm=ME_FULL, to=PROF, mid="<a1@x.example>", plain="Can we meet?"),
        msg(mid="<a2@x.example>", irt="<a1@x.example>", refs=("<a1@x.example>",),
            subject="Re: Thesis draft", date="2026-09-02 09:00", plain="Thursday.", delivered_to=ME),
        msg(frm="Weekly <news@list.example>", mid="<n1@x.example>", subject="Offers",
            date="2026-09-03 09:00", plain="Buy.", delivered_to=ME),
    ])
    return "Inbox/export.mbox"


def test_the_status_is_the_row_the_adr_draws(tmp_vault):
    s = MAIL_BLOCK.status()
    assert (s["name"], s["title"]) == ("mail", "Mail")
    assert s["detail"] == "Keep chosen mail threads as notes"
    assert s["actions"] == ["plan", "import", "undo"]
    assert s["account"] == "" and s["state"] == "local-only" and s["last"] is None
    mail.remember_addresses([ME])
    assert MAIL_BLOCK.status()["account"] == ME


def test_plan_is_the_dry_run_with_every_thread_listed(inbox_box):
    out = MAIL_BLOCK.act("plan", {"path": inbox_box})
    assert out["ok"] and out["in_file"] == 2 and out["took_part"] == 1
    assert out["guessed"] and out["me"] == [ME] and out["added_by_all"] == 1
    [row] = out["threads"]
    assert row["subject"] == "Thesis draft" and row["messages"] == 2
    assert row["path"].startswith("sources/mail/2026-09-01 Thesis draft ")
    assert not mail.recorded_paths()                       # a dry run writes nothing


def test_import_waits_for_the_address_then_writes_once(inbox_box, tmp_path):
    refused = MAIL_BLOCK.act("import", {"path": inbox_box})
    assert not refused["ok"] and "Confirm your address" in refused["error"]

    out = MAIL_BLOCK.act("import", {"path": inbox_box, "me": ME})
    assert out == {"ok": True, "written": 1, "run_id": out["run_id"]}
    assert len(list((tmp_path / "vault" / "sources" / "mail").glob("*.md"))) == 1
    assert MAIL_BLOCK.act("import", {"path": inbox_box})["written"] == 0


def test_the_block_reads_only_files_inside_the_vault(inbox_box, tmp_path):
    outside = write_mbox(tmp_path / "elsewhere.mbox", [msg(mid="<o@x.example>", plain="x")])
    for path in (str(outside), "../elsewhere.mbox", "Inbox/../../elsewhere.mbox"):
        out = MAIL_BLOCK.act("plan", {"path": path})
        assert not out["ok"] and "inside the vault" in out["error"], path


def test_an_unknown_action_is_refused(tmp_vault):
    assert MAIL_BLOCK.act("sync", {}) == {"ok": False, "error": "unknown action"}


# --- the upload route: into the inbox inside the write boundary, no stage() --------

@pytest.fixture
def web(tmp_vault, tmp_path):
    from fastapi.testclient import TestClient

    from silica.ui.web import server

    server._reset_session()
    return TestClient(server.app), tmp_path / "vault"


def _upload(tc, name: str, data: bytes):
    """The row's request: the file as the body, its name in the query."""
    return tc.post("/mail/upload", params={"name": name}, content=data,
                   headers={"Content-Type": "application/octet-stream"})


def test_an_uploaded_export_lands_in_the_inbox_and_nothing_is_written(web, tmp_path):
    from tests.mail_fixtures import write_eml

    tc, vault = web
    eml = write_eml(tmp_path / "one.eml", msg(frm=ME_FULL, mid="<u@x.example>", plain="Hi."))
    r = _upload(tc, "one.eml", eml.read_bytes())
    assert r.status_code == 200, r.text
    rel = r.json()["path"]
    assert rel.endswith("/one.eml") and (vault / rel).exists()
    assert not list(vault.rglob("sources/mail/*.md"))       # the dry run comes first
    assert _upload(tc, "x.pdf", b"%PDF").status_code == 400
    assert _upload(tc, "../../escape.eml", eml.read_bytes()).json()["path"].endswith("/escape.eml")


def test_the_mail_block_is_a_row_of_the_connectors_section():
    from silica.connectors import connectors, find

    assert "mail" in [c.name for c in connectors()]
    assert find("mail") is MAIL_BLOCK


def test_the_row_runs_end_to_end_through_the_panel_routes(web, tmp_path):
    """Upload, dry run, Import: the three requests the row makes, through the
    shell's routes (ADR-0089 point 8), which run an action off the event loop."""
    tc, vault = web
    box = write_mbox(tmp_path / "export.mbox", [
        msg(frm=ME_FULL, to=PROF, mid="<a1@x.example>", plain="Can we meet?"),
        msg(frm="Weekly <news@list.example>", mid="<n1@x.example>", subject="Offers",
            date="2026-09-03 09:00", plain="Buy.", delivered_to=ME),
    ])
    path = _upload(tc, "export.mbox", box.read_bytes()).json()["path"]
    assert [b["name"] for b in tc.get("/connectors").json()].count("mail") == 1
    plan = tc.post("/connectors/mail/plan", json={"path": path}).json()
    assert plan["ok"] and plan["guessed"] and plan["me"] == [ME] and len(plan["threads"]) == 1
    refused = tc.post("/connectors/mail/import", json={"path": path})
    assert refused.status_code == 200 and refused.json()["ok"] is False   # a guess is not a yes
    done = tc.post("/connectors/mail/import", json={"path": path, "me": ME}).json()
    assert done["ok"] and done["written"] == 1
    assert len(list((vault / "sources" / "mail").glob("*.md"))) == 1


# --- a real export: past the drop cap, under its own (gap 1, 2026-10-12) -----------

def test_an_export_past_the_drop_cap_still_lands(web, tmp_path, monkeypatch):
    from silica.ui.web import server

    tc, vault = web
    monkeypatch.setattr(server, "_UPLOAD_MAX_BYTES", 256)   # a Takeout file is no drop
    box = write_mbox(tmp_path / "big.mbox", [msg(mid=f"<b{i}@x.example>", plain="x" * 200)
                                             for i in range(8)])
    assert box.stat().st_size > 256
    r = _upload(tc, "big.mbox", box.read_bytes())
    assert r.status_code == 200, r.text
    assert (vault / r.json()["path"]).read_bytes() == box.read_bytes()


def test_an_export_past_the_mail_cap_is_refused_and_leaves_no_partial_file(web, monkeypatch):
    from silica.ui.web import server

    tc, vault = web
    monkeypatch.setattr(server, "_MAIL_UPLOAD_MAX_BYTES", 256)
    r = _upload(tc, "huge.mbox", b"From x\n" + b"y" * 4096)
    assert r.status_code == 413 and "/mail import" in r.json()["detail"]
    assert not list(vault.rglob("huge.mbox"))


# --- undo by run id (gap 3, 2026-10-12) ---------------------------------------------

def test_undo_takes_the_import_back_by_its_run_id(inbox_box, tmp_path):
    from silica.kernel.write.undo_journal import get_undo_journal

    run_id = MAIL_BLOCK.act("import", {"path": inbox_box, "me": ME})["run_id"]
    last = MAIL_BLOCK.status()["last"]
    assert last == {"run_id": run_id, "file": "export.mbox", "at": last["at"], "written": 1}
    # A chat write after the import is what a bare /revert would take back.
    get_undo_journal().start_run(source="chat")

    out = MAIL_BLOCK.act("undo", {"run_id": run_id})
    assert out == {"ok": True, "removed": 1, "kept": 0, "gone": 0, "errors": []}
    assert not list((tmp_path / "vault" / "sources" / "mail").glob("*.md"))
    s = MAIL_BLOCK.status()
    assert s["last"] is None and s["imported"] == 0
    assert len(MAIL_BLOCK.act("plan", {"path": inbox_box})["threads"]) == 1   # importable again


def test_undo_reverts_only_a_mail_import_of_this_vault(inbox_box):
    from silica.kernel.write.undo_journal import get_undo_journal

    chat = get_undo_journal().start_run(source="chat")
    for run_id in (chat, "", "nope"):
        out = MAIL_BLOCK.act("undo", {"run_id": run_id})
        assert out == {"ok": False, "error": "that is not a mail import of this vault"}


def test_dry_runs_in_flight_share_one_parse_on_the_parse_thread(inbox_box, tmp_path, monkeypatch):
    """Two dry runs (a blur, then a tick) parsed a big export twice at once,
    and on different pool workers, each keeping its own malloc arena: one
    parse, on mail's own thread (mail.py, `_PARSER`)."""
    import threading
    import time

    real, calls, out = mail.parse, [], []

    def counted(path):
        calls.append(threading.current_thread().name)
        time.sleep(0.2)  # the second caller arrives while the first parse runs
        return real(path)

    monkeypatch.setattr(mail, "parse", counted)
    mail._PARSED.clear()
    src = tmp_path / "vault" / inbox_box
    ts = [threading.Thread(target=lambda: out.append(mail.threads_in(src))) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(calls) == 1 and calls[0].startswith("mail-parse")
    assert out[0] is out[1]
