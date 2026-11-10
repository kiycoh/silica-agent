# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0155's server: a note written by hand reaches the index with no chat
model, `/note/new` creates it as one run whose undo is the trash, and a pasted
image lands in the vault."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32
GIF = b"GIF89a" + b"\0" * 32
WEBP = b"RIFF\x24\0\0\0WEBPVP8 " + b"\0" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


class Vectors:
    """One axis per word of `WORDS`: the query lands on the note that holds the
    word, so a found note proves its own text was embedded."""
    model = "test-embedder"
    WORDS = ("zebra", "lighthouse")

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts, *, query=False):
        self.calls.append(list(texts))
        return [[1.0 if w in t.lower() else 0.0 for w in self.WORDS] for t in texts]


@pytest.fixture
def client(tmp_vault, tmp_path, monkeypatch):
    from silica.config import CONFIG
    from silica.ui.web import server
    from silica.ui.web import note_ops
    from silica.ui.web.session import SESSION

    monkeypatch.setattr("silica.kernel.recall.embed._index_path", lambda: tmp_path / "embeddings.json")
    tmp_vault.note("Corso/Lezione.md", "# Lezione\n\nsee [[Nuova]]\n")
    tmp_vault.note("Altro/Preso.md", "# Preso\n")
    SESSION.reset()
    yield TestClient(server.app), server, Path(CONFIG.vault_path)
    with note_ops._INDEX_LOCK:
        pending = list(note_ops._INDEX_PENDING.values())
        note_ops._INDEX_PENDING.clear()
    for timer in pending:
        timer.cancel()
    SESSION.release()


@pytest.fixture
def embedder(monkeypatch):
    """An embedder that answers, and a chat model that fails the test if asked."""
    vectors = Vectors()
    monkeypatch.setattr("silica.model.providers.get_embedder", lambda config: vectors)

    def down(*a, **kw):
        raise AssertionError("hand writing must not reach a chat model")

    monkeypatch.setattr("silica.model.llm.call_llm", down)
    return vectors


def new(tc, **payload):
    return tc.post("/note/new", json=payload)


# -- the index follows the person (point 18) ----------------------------------

def test_hand_written_text_is_found_after_a_let_go_with_the_chat_model_down(client, embedder):
    from silica.tools.graph import silica_semantic_search

    tc, *_ = client
    made = new(tc, folder="Corso", name="Faro", text="A note about a lighthouse.\n").json()
    saved = tc.post("/note/save", json={"path": made["path"], "etag": made["etag"],
                                        "text": "A note about a lighthouse, typed by hand.\n"}).json()
    assert saved["ok"]
    assert not silica_semantic_search(query="lighthouse", k=3).get("results")  # a refresh is due, not done
    assert tc.post("/note/let-go", json={"path": made["path"]}).json() == {"ok": True}
    hits = silica_semantic_search(query="lighthouse", k=3)["results"]
    assert hits and hits[0]["path"].removesuffix(".md") == "Corso/Faro"


def test_a_save_schedules_one_refresh_and_each_save_moves_it_on(client, embedder, monkeypatch):
    from silica.ui.web import note_ops
    tc, server, _ = client
    monkeypatch.setattr(note_ops, "_INDEX_AFTER_S", 0.2)
    etag = tc.get("/note/raw", params={"path": "Corso/Lezione"}).json()["etag"]
    for i in range(3):
        etag = tc.post("/note/save", json={"path": "Corso/Lezione", "etag": etag, "text": f"zebra {i}\n"}).json()["etag"]
        time.sleep(0.05)
    assert embedder.calls == []            # 60 s after the LAST save, never per save
    deadline = time.monotonic() + 3
    while not embedder.calls and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.3)
    assert len(embedder.calls) == 1 and "zebra 2" in embedder.calls[0][0]
    assert not note_ops._INDEX_PENDING


def test_a_save_that_changes_nothing_schedules_nothing(client, embedder):
    from silica.ui.web import note_ops
    tc, server, vault = client
    raw = tc.get("/note/raw", params={"path": "Corso/Lezione"}).json()
    tc.post("/note/save", json={"path": raw["path"], "etag": raw["etag"], "text": raw["text"]})
    assert not note_ops._INDEX_PENDING


def test_a_let_go_with_nothing_pending_is_free_and_answers_ok(client, embedder):
    tc, *_ = client
    for path in ("Corso/Lezione.md", "Not/There.md", ""):
        assert tc.post("/note/let-go", json={"path": path}).json() == {"ok": True}
    assert embedder.calls == []


def test_a_second_let_go_runs_nothing(client, embedder):
    tc, *_ = client
    path = new(tc, folder="", name="Solo", text="zebra\n").json()["path"]
    tc.post("/note/let-go", json={"path": path})
    tc.post("/note/let-go", json={"path": path})
    assert len(embedder.calls) == 1


def test_a_failing_embedder_is_left_to_embed_and_the_save_stands(client, monkeypatch, caplog):
    class Down:
        model = "down"

        def embed(self, texts, *, query=False):
            raise ConnectionError("no embedder here")

    monkeypatch.setattr("silica.model.providers.get_embedder", lambda config: Down())
    tc, _, vault = client
    made = new(tc, folder="", name="Bozza", text="zebra\n").json()
    assert tc.post("/note/let-go", json={"path": made["path"]}).json() == {"ok": True}
    assert (vault / "Bozza.md").read_text(encoding="utf-8") == "zebra\n"
    assert "embedding failed: ConnectionError" in caplog.text


def test_no_embedder_does_nothing(client, monkeypatch):
    def none(config):
        raise RuntimeError("no embedder configured")

    built = []
    monkeypatch.setattr("silica.model.providers.get_embedder", none)
    monkeypatch.setattr("silica.kernel.recall.cooccurrence.build_index", lambda *a, **kw: built.append(a))
    tc, *_ = client
    path = new(tc, folder="", name="Bozza", text="zebra\n").json()["path"]
    assert tc.post("/note/let-go", json={"path": path}).json() == {"ok": True}
    assert not built


def test_a_note_deleted_before_its_refresh_is_left_alone(client, embedder):
    tc, _, vault = client
    path = new(tc, folder="", name="Via", text="zebra\n").json()["path"]
    (vault / path).unlink()
    assert tc.post("/note/let-go", json={"path": path}).json() == {"ok": True}
    assert embedder.calls == []


def test_a_timer_from_another_vault_reads_nothing_here(client, embedder):
    from silica.ui.web import note_ops
    _, server, _ = client
    note_ops._index_due("Corso/Lezione", "/some/other/vault")
    assert embedder.calls == []


# -- /note/new (points 7 to 9) ------------------------------------------------

def test_a_new_note_holds_exactly_the_text_and_answers_as_a_save_does(client, embedder):
    tc, _, vault = client
    body = new(tc, folder="Corso", name="Nuova", text="# Mine\n\n[[Assente]]\n").json()
    assert (vault / "Corso/Nuova.md").read_bytes() == b"# Mine\n\n[[Assente]]\n"
    assert body["ok"] is True and body["path"] == "Corso/Nuova.md" and body["name"] == "Nuova"
    assert body["text"] == "# Mine\n\n[[Assente]]\n" and body["broken"] == ["Assente"] and body["run"]
    assert "same_name" not in body
    # the etag is the disk's: the editor's next save is not refused
    saved = tc.post("/note/save", json={"path": body["path"], "etag": body["etag"], "text": "more\n"})
    assert saved.status_code == 200


def test_an_empty_note_is_written_empty_for_an_unresolved_links_door(client, embedder):
    tc, _, vault = client
    body = new(tc, folder="Corso", name="Nuova", text="").json()
    assert (vault / "Corso/Nuova.md").read_bytes() == b""
    # the link that named it resolves now
    assert tc.get("/note/raw", params={"path": "Corso/Lezione"}).json()["broken"] == []
    assert body["text"] == ""


def test_text_with_no_name_is_untitled_then_untitled_2_in_that_folder(client, embedder):
    tc, _, vault = client
    assert new(tc, folder="Corso", name="", text="a").json()["path"] == "Corso/Untitled.md"
    second = new(tc, folder="Corso", name="", text="b").json()
    assert second["path"] == "Corso/Untitled 2.md" and second["name"] == "Untitled 2"
    assert new(tc, folder="Altro", name="", text="c").json()["path"] == "Altro/Untitled.md"


def test_the_root_is_the_empty_folder_and_a_missing_folder_is_made(client, embedder):
    tc, _, vault = client
    assert new(tc, folder="", name="Radice", text="x").json()["path"] == "Radice.md"
    assert new(tc, folder="Nuovo/Sotto", name="Foglia", text="x").json()["path"] == "Nuovo/Sotto/Foglia.md"
    assert (vault / "Nuovo/Sotto/Foglia.md").is_file()


@pytest.mark.parametrize("payload", [
    {"folder": "", "name": "", "text": ""},
    {"folder": "", "name": "a/b", "text": "x"},
    {"folder": "", "name": ".hidden", "text": "x"},
    {"folder": "", "name": "a:b", "text": "x"},
    {"folder": "../up", "name": "a", "text": "x"},
    {"folder": ".silica", "name": "a", "text": "x"},
    {"folder": "Corso/a:b", "name": "a", "text": "x"},
    {"folder": "", "name": "n" * 201, "text": "x"},
])
def test_a_bad_name_is_refused_with_the_sentence_shown_under_the_title(client, embedder, payload):
    tc, _, vault = client
    r = new(tc, **payload)
    assert r.status_code == 400 and r.json()["detail"]
    assert not list(vault.rglob("*.md"))[2:]


def test_a_taken_path_is_409_and_leaves_the_note_alone(client, embedder):
    from silica.kernel.write.undo_journal import USER_WRITE_SOURCE, get_undo_journal

    tc, _, vault = client
    before = (vault / "Altro/Preso.md").read_bytes()
    r = new(tc, folder="Altro", name="Preso", text="over it")
    assert r.status_code == 409 and "Altro/Preso.md" in r.json()["detail"]
    assert (vault / "Altro/Preso.md").read_bytes() == before
    conn = get_undo_journal()._conn()
    assert not conn.execute("SELECT 1 FROM runs WHERE source = ? AND reverted_at IS NULL",
                            (USER_WRITE_SOURCE,)).fetchall()


@pytest.mark.parametrize("folder", ["Inbox", "Inbox/sotto", "sources"])
def test_silicas_own_folders_are_refused(client, embedder, folder):
    tc, _, vault = client
    r = new(tc, folder=folder, name="Mia", text="x")
    assert r.status_code == 400 and "own folder" in r.json()["detail"]
    assert not (vault / folder / "Mia.md").exists()


def test_a_name_another_folder_holds_is_allowed_and_said(client, embedder):
    tc, *_ = client
    body = new(tc, folder="Corso", name="Preso", text="x").json()
    assert body["path"] == "Corso/Preso.md" and body["same_name"] == "Altro/Preso.md"


def test_a_full_vault_is_refused_as_the_saves_are(client, embedder, monkeypatch):
    monkeypatch.setenv("SILICA_VAULT_MAX_NOTES", "2")
    tc, _, vault = client
    r = new(tc, folder="Corso", name="Di troppo", text="x")
    assert r.status_code == 400 and "this vault may hold" in r.json()["detail"]
    assert not (vault / "Corso/Di troppo.md").exists()


def test_it_is_written_while_a_turn_runs(client, embedder):
    from silica.ui.web.session import SESSION
    tc, server, _ = client
    assert SESSION.claim()
    assert new(tc, folder="", name="Durante", text="x").status_code == 200


def test_a_foreign_origin_cannot_write_or_paste(client, embedder):
    tc, _, vault = client
    evil = {"Origin": "http://evil.example"}
    assert tc.post("/note/new", json={"name": "x", "text": "y"}, headers=evil).status_code == 403
    assert tc.post("/note/let-go", json={"path": "x"}, headers=evil).status_code == 403
    assert tc.post("/note/asset", data={"note": "Corso/Lezione"}, files={"file": ("a.png", PNG)},
                   headers=evil).status_code == 403
    assert not (vault / "x.md").exists()


# -- undo is the trash ---------------------------------------------------------

def test_undo_sends_the_note_to_the_trash_with_what_it_holds_by_then(client, embedder):
    from silica.kernel.write.undo_journal import USER_WRITE_SOURCE, get_undo_journal

    tc, _, vault = client
    made = new(tc, folder="Corso", name="Nuova", text="first\n").json()
    assert get_undo_journal().run_info(made["run"])["source"] == USER_WRITE_SOURCE
    tc.post("/note/save", json={"path": made["path"], "etag": made["etag"], "text": "typed for an hour\n"})

    undone = tc.post("/tree/undo", json={"run": made["run"]}).json()
    assert undone == {"restored": ["Corso/Nuova.md"], "skipped": []}
    assert not (vault / "Corso/Nuova.md").exists()
    item = next(t for t in tc.get("/trash").json()["items"] if t["path"] == "Corso/Nuova.md")

    assert tc.post("/trash/restore", json={"run": item["run"], "path": item["path"]}).status_code == 200
    assert (vault / "Corso/Nuova.md").read_text(encoding="utf-8") == "typed for an hour\n"


def test_undo_of_an_empty_note_leaves_it_in_the_trash_too(client, embedder):
    tc, _, vault = client
    made = new(tc, folder="", name="Vuota", text="").json()
    assert tc.post("/tree/undo", json={"run": made["run"]}).json()["restored"] == ["Vuota.md"]
    assert [t["path"] for t in tc.get("/trash").json()["items"]] == ["Vuota.md"]


def test_undo_is_refused_twice_and_on_a_note_gone_says_so(client, embedder):
    tc, _, vault = client
    made = new(tc, folder="", name="Una", text="x").json()
    assert tc.post("/tree/undo", json={"run": made["run"]}).status_code == 200
    assert tc.post("/tree/undo", json={"run": made["run"]}).status_code == 409

    gone = new(tc, folder="", name="Due", text="x").json()
    (vault / "Due.md").unlink()
    assert tc.post("/tree/undo", json={"run": gone["run"]}).json() == {"restored": [], "skipped": []}


def test_undo_waits_for_the_turn_as_the_other_tree_undos_do(client, embedder):
    from silica.ui.web.session import SESSION
    tc, server, vault = client
    made = new(tc, folder="", name="Una", text="x").json()
    assert SESSION.claim()
    assert tc.post("/tree/undo", json={"run": made["run"]}).status_code == 409
    assert (vault / "Una.md").exists()


# -- /note/asset (point 15) ----------------------------------------------------

def paste(tc, data, note="Corso/Lezione", name="anything.png"):
    return tc.post("/note/asset", data={"note": note}, files={"file": (name, data)})


@pytest.mark.parametrize("data, ext", [(PNG, "png"), (JPEG, "jpg"), (GIF, "gif"), (WEBP, "webp")])
def test_a_pasted_image_lands_beside_the_note_and_asset_serves_it(client, data, ext):
    tc, _, vault = client
    # the bytes name the type, whatever the name says
    body = paste(tc, data, name="clip.bin").json()
    assert body["ok"] is True and body["path"].startswith("Corso/attachments/Pasted image ")
    assert body["path"].endswith(f".{ext}")
    assert (vault / body["path"]).read_bytes() == data
    name = body["path"].rsplit("/", 1)[-1]
    assert body["embed"] == f"![[{name}]]"
    for ref in (body["path"], name):       # the embed names it by its bare name
        served = tc.get("/asset", params={"path": ref})
        assert served.status_code == 200 and served.content == data


@pytest.mark.parametrize("data", [SVG, b"<?xml version='1.0'?>" + SVG, b"not an image at all", b""])
def test_anything_else_is_refused_whatever_its_name_says(client, data):
    tc, _, vault = client
    r = paste(tc, data, name="innocent.png")
    assert r.status_code == 400 and "PNG, JPEG, GIF or WebP" in r.json()["detail"]
    assert not list(vault.rglob("Pasted image*"))


def test_a_clash_takes_the_next_number(client, monkeypatch):
    import datetime as dt

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 6, 12, 0, 1)

    monkeypatch.setattr(dt, "datetime", Frozen)
    tc, *_ = client
    first, second = (paste(tc, PNG).json()["path"] for _ in range(2))
    assert first == "Corso/attachments/Pasted image 20261106120001.png"
    assert second == "Corso/attachments/Pasted image 20261106120001 2.png"


def test_past_20_mb_is_refused(client, monkeypatch):
    from silica.ui.web import drop
    tc, server, vault = client
    monkeypatch.setattr(drop, "MAX_IMAGE_BYTES", len(PNG) - 1)
    assert paste(tc, PNG).status_code == 400
    assert not list(vault.rglob("Pasted image*"))


def test_a_note_that_does_not_resolve_is_404(client):
    tc, *_ = client
    assert paste(tc, PNG, note="Nowhere/Nothing").status_code == 404


def test_a_full_vault_refuses_an_image(client, monkeypatch):
    from silica.ui.web import drop
    monkeypatch.setenv("SILICA_VAULT_MAX_MB", "1")
    tc, server, vault = client
    monkeypatch.setattr(drop, "MAX_IMAGE_BYTES", 8 * 2**20)
    r = paste(tc, PNG + b"\0" * (2 * 2**20))
    assert r.status_code == 400 and "this vault may hold" in r.json()["detail"]


@pytest.mark.parametrize("setting, expected", [
    ("./Immagini", "Corso/Immagini"),
    ("./", "Corso"),
    ("/", ""),
    ("", ""),
    ("Allegati/Foto", "Allegati/Foto"),
])
def test_obsidians_setting_is_read_as_obsidian_reads_it(client, setting, expected):
    tc, _, vault = client
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian/app.json").write_text(json.dumps({"attachmentFolderPath": setting}), encoding="utf-8")
    path = paste(tc, PNG).json()["path"]
    assert path.rpartition("/")[0] == expected
    assert (vault / path).is_file()


@pytest.mark.parametrize("raw", ['{"other": 1}', "not json", '{"attachmentFolderPath": 3}', "[1]"])
def test_a_setting_that_names_no_folder_is_attachments_beside_the_note(client, raw):
    tc, _, vault = client
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian/app.json").write_text(raw, encoding="utf-8")
    assert paste(tc, PNG).json()["path"].startswith("Corso/attachments/")


def test_a_setting_that_leaves_the_vault_does_not_place_a_file_there(client):
    tc, _, vault = client
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian/app.json").write_text('{"attachmentFolderPath": "../outside"}', encoding="utf-8")
    assert paste(tc, PNG).json()["path"].startswith("Corso/attachments/")
    assert not (vault.parent / "outside").exists()


def test_a_note_at_the_root_keeps_its_folder_at_the_root(client):
    tc, _, vault = client
    (vault / "Radice.md").write_text("x", encoding="utf-8")
    assert paste(tc, PNG, note="Radice").json()["path"].startswith("attachments/Pasted image ")
