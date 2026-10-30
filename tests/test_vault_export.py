"""The vault's two downloads (ADR-0142)."""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TOP = "Silica 2026-10-30 164207"


@pytest.fixture
def hosted(tmp_vault, monkeypatch):
    from silica.ui.web import server

    monkeypatch.setenv("SILICA_HOSTED", "1")
    server._reset_session()
    return TestClient(server.app)


def _vault() -> Path:
    from silica.config import CONFIG

    return Path(CONFIG.vault_path)


def _zip(body: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        return {n: z.read(n) for n in z.namelist()}


def _plan(roots=(), sent=None):
    from silica.ui.web import vault_export as ve

    return ve.plan_additions(_vault(), {"roots": list(roots), "sent": sent or {}, "last": None}, TOP)


def _texts(plan) -> dict[str, str]:
    return {name.removeprefix(f"{TOP}/"): data.decode() for name, data, _ in plan.files if isinstance(data, bytes)}


# --- point 1: the server's only ----------------------------------------------

def test_a_laptop_has_no_downloads(tmp_vault, monkeypatch):
    from silica.ui.web import server

    monkeypatch.delenv("SILICA_HOSTED", raising=False)
    tc = TestClient(server.app)
    assert tc.get("/vault/export/summary").status_code == 404
    assert tc.get("/vault/export").status_code == 404
    assert tc.post("/vault/export/added").status_code == 404


# --- point 2: the vault whole --------------------------------------------------

def test_the_vault_archive_holds_every_file_under_one_folder_and_follows_no_link(hosted, tmp_vault, tmp_path):
    tmp_vault.note("Inbox/MyVault/a.md", "a")
    tmp_vault.note("Biology/Cell.md", "cell")
    tmp_vault.note(".silica/instructions.md", "be brief")
    secret = tmp_path / "secret.txt"
    secret.write_text("not the vault's")
    os.symlink(secret, _vault() / "leak.md")
    os.symlink(tmp_path, _vault() / "outside")

    r = hosted.get("/vault/export")
    assert r.status_code == 200
    top = r.headers["content-disposition"].split('filename="')[1].removesuffix('.zip"')
    got = _zip(r.content)
    assert set(got) == {f"{top}/Inbox/MyVault/a.md", f"{top}/Biology/Cell.md", f"{top}/.silica/instructions.md"}
    assert got[f"{top}/Biology/Cell.md"] == b"cell"


# --- point 3: what Silica added ------------------------------------------------

def test_the_additions_hold_silicas_notes_and_none_of_the_persons_material(tmp_vault):
    tmp_vault.note("Inbox/MyVault/Corso/Lezione 1.md", "theirs")
    tmp_vault.note("Done/MyVault/Corso/Lezione 2.md", "theirs, archived")
    tmp_vault.note("sources/MyVault/Corso/Lezione 2.md", "verbatim copy")
    tmp_vault.note(".silica/instructions.md", "hidden")
    tmp_vault.note("vault.yaml", "write_dir: ''")
    tmp_vault.note("Biology/Cell.md", "made by Silica")

    plan = _plan(roots=["MyVault"])
    assert [name for name, _, _ in plan.files] == [f"{TOP}/Biology/Cell.md"]
    assert plan.notes == 1 and plan.left == 0


def test_a_second_download_holds_only_what_is_new(tmp_vault):
    import hashlib

    sha = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731
    tmp_vault.note("Biology/Sent.md", "edited by Silica since it was sent")
    tmp_vault.note("Biology/Moved.md", "moved bytes")
    tmp_vault.note("Biology/Twin.md", "same bytes")
    tmp_vault.note("Biology/Kept.md", "same bytes")
    tmp_vault.note("Biology/New.md", "new")
    earlier = "Silica 2026-10-23 090000"
    sent = {
        "Biology/Sent.md": {"sha256": sha("as it was sent"), "folder": earlier},
        "Old/Moved.md": {"sha256": sha("moved bytes"), "folder": earlier},  # its path is gone
        "Biology/Kept.md": {"sha256": sha("same bytes"), "folder": earlier},  # its path is still there
    }

    plan = _plan(sent=sent)
    # A changed note is never resent, a move is not resent, and a distinct note
    # with the bytes of one still in place is not mistaken for it.
    assert sorted(name for name, _, _ in plan.files) == [f"{TOP}/Biology/New.md", f"{TOP}/Biology/Twin.md"]


def test_the_sent_list_moves_only_once_the_last_byte_is_handed_over(tmp_vault):
    from silica.ui.web import vault_export as ve

    tmp_vault.note("Biology/Cell.md", "cell")
    vault = _vault()

    ve.BUSY.acquire()
    stream = ve.added(vault, _plan())
    next(stream)
    stream.close()  # the request dropped before its first byte
    assert ve.load_state(vault)["sent"] == {}
    assert ve.BUSY.acquire(blocking=False)  # released all the same

    stream = ve.added(vault, _plan())
    assert _zip(b"".join(stream))
    assert set(ve.load_state(vault)["sent"]) == {"Biology/Cell.md"}
    assert not ve.BUSY.locked()


def test_the_download_route_answers_once_then_has_nothing_new(hosted, tmp_vault):
    tmp_vault.note("Biology/Cell.md", "cell")

    r = hosted.post("/vault/export/added")
    assert r.status_code == 200
    folder = r.headers["x-silica-folder"]
    assert folder.startswith("Silica ") and r.headers["x-silica-files"] == "1"
    assert set(_zip(r.content)) == {f"{folder}/Biology/Cell.md"}
    assert hosted.post("/vault/export/added").status_code == 204
    s = hosted.get("/vault/export/summary").json()
    assert s["added"]["files"] == 0 and s["added"]["last"]
    assert s["vault"]["files"] == 1


def test_a_second_download_while_one_streams_is_refused(hosted, tmp_vault):
    from silica.ui.web import vault_export as ve

    tmp_vault.note("Biology/Cell.md", "cell")
    ve.BUSY.acquire()
    try:
        assert hosted.post("/vault/export/added").status_code == 409
    finally:
        ve.BUSY.release()


# --- point 4: links written for the root of the Obsidian vault -----------------

def test_links_point_at_the_persons_own_files_and_the_rest_travel(tmp_vault):
    earlier = "Silica 2026-10-23 090000"
    tmp_vault.note("Done/MyVault/Corso/Lezione 1.md", "theirs")
    tmp_vault.note("sources/MyVault/Corso/Lezione 1.md", "verbatim")
    tmp_vault.note("Done/MyVault/Corso/slides.md", "---\nsource_file: slides.pdf\n---\nconverted")
    tmp_vault.note("sources/MyVault/Corso/slides.md", "verbatim slides")
    tmp_vault.note("sources/web/Page.md", "a page the person has no copy of")
    tmp_vault.note("Inbox/Images/fig.png", "png")
    tmp_vault.note("Biology/Other.md", "other")
    tmp_vault.note("Biology/Sent before.md", "sent")
    tmp_vault.note("Biology/Cell.md", "\n".join([
        "---", "related: \"[[Biology/Other]]\"", "---",
        "[^a]: [[sources/MyVault/Corso/Lezione 1]]",
        "[^b]: [[sources/MyVault/Corso/slides|the slides]]",
        "[^c]: [[sources/web/Page#Intro]]",
        "![[fig.png]]",
        "[[Biology/Sent before]] and [[Bare name]]",
        "[[Inbox/nowhere/ghost]]",
        "`[[sources/web/Page]]` stays as code",
    ]))
    import hashlib

    sent = {"Biology/Sent before.md": {"sha256": hashlib.sha256(b"sent").hexdigest(), "folder": earlier}}
    plan = _plan(roots=["MyVault"], sent=sent)
    text = _texts(plan)["Biology/Cell.md"]

    assert f'related: "[[{TOP}/Biology/Other]]"' in text
    assert "[^a]: [[Corso/Lezione 1]]" in text
    assert "[^b]: [[Corso/slides.pdf|the slides]]" in text
    assert f"[^c]: [[{TOP}/sources/web/Page#Intro]]" in text
    assert "![[fig.png]]" in text
    assert f"[[{earlier}/Biology/Sent before]] and [[Bare name]]" in text
    assert "[[Inbox/nowhere/ghost]]" in text and plan.left == 1
    assert "`[[sources/web/Page]]` stays as code" in text
    names = {name.removeprefix(f"{TOP}/") for name, _, _ in plan.files}
    # The leaf of a source the person lacks, and the image a note shows, travel;
    # the person's own lecture and its leaf do not.
    assert names == {"Biology/Cell.md", "Biology/Other.md", "sources/web/Page.md", "Inbox/Images/fig.png"}
    assert plan.notes == 3


# --- point 5: the + remembers a folder that was an Obsidian vault --------------

def _import(tc, folder, obsidian):
    files = [("files", ("a.md", b"a note", "text/markdown"))]
    return tc.post("/vault/import", files=files,
                   data={"paths": [f"{folder}/a.md"], "last": "1", "obsidian": obsidian})


def test_the_plus_marks_a_folder_that_held_obsidian(hosted, monkeypatch):
    from silica.ui.web import server
    from silica.ui.web import vault_export as ve

    monkeypatch.setattr(server, "_embed_imported", lambda folders: None)
    assert _import(hosted, "MyVault", "MyVault").status_code == 200
    assert _import(hosted, "Other", "NotInThisBatch").status_code == 200
    assert ve.load_state(_vault())["roots"] == ["MyVault"]


def test_a_laptop_marks_nothing(tmp_vault, monkeypatch):
    from silica.ui.web import server
    from silica.ui.web import vault_export as ve

    monkeypatch.delenv("SILICA_HOSTED", raising=False)
    monkeypatch.setattr(server, "_embed_imported", lambda folders: None)
    server._reset_session()
    assert _import(TestClient(server.app), "MyVault", "MyVault").status_code == 200
    assert ve.load_state(_vault())["roots"] == []


def test_the_help_names_the_downloads_on_the_server_only():
    from silica.ui.web.app_help import panel

    def terms(on_server):
        return [term for _, rows in panel(on_server) for term, _ in rows]

    assert "download" in terms(True) and "download" not in terms(False)
