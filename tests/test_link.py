"""A Silica on a computer links to a hosted account and publishes only through it (ADR-0157 points
4, 9, 10 and 11). The token it keeps can publish in the person's name: it is in one file, 0600,
and nowhere else."""
from __future__ import annotations

import io
import json
import logging
import stat
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_public_site import PNG

TOKEN = "tok_" + "A1b2C3d4" * 5
POLL = "poll_" + "z" * 38
CODE = "KQ7-4MX"
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


class Gateway:
    """The gateway's /link routes as Task 9 answers them, and a tenant's /public/receive behind
    /link/publish, scripted by each test. Every request is kept: path, headers and body."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict, bytes]] = []
        self.confirmed = False
        self.handed = False
        self.handle = "marta"
        self.start = (200, {"code": CODE, "poll": POLL, "expires_in": 600}, {})
        self.publish = (200, {"slug": "memory", "url": "https://wikis.test/@marta/memory/", "notes": 3,
                              "published_at": "2026-11-08T10:00:00+00:00"}, {})

    def answer(self, path: str, body: bytes) -> tuple[int, dict, dict]:
        if path == "/link/start":
            return self.start
        if path == "/link/poll":
            if json.loads(body or b"{}").get("poll") != POLL or self.handed:
                return 200, {"state": "gone"}, {}
            if not self.confirmed:
                return 200, {"state": "pending"}, {}
            self.handed = True  # handed out once: the gateway forgets the code with it
            return 200, {"state": "linked", "token": TOKEN, "handle": self.handle}, {}
        if path == "/link/publish":
            return self.publish
        return 404, {}, {}

    def posts(self, path: str) -> list[tuple[dict, bytes]]:
        return [(h, b) for p, h, b in self.seen if p == path]


@pytest.fixture
def gateway(monkeypatch):
    stub = Gateway()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            stub.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            status, data, extra = stub.answer(self.path, body)
            out = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            for k, v in extra.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stub.host = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("SILICA_LINK_HOST", stub.host)
    yield stub
    srv.shutdown()
    srv.server_close()


def _link_file() -> Path:
    from silica.kernel.recall import paths

    return paths._SILICA_HOME / "link.json"


# --- the host a token may travel to ------------------------------------------------------

def test_the_host_is_https_or_this_machine_and_nothing_else(monkeypatch):
    from silica.kernel import account_link as link

    monkeypatch.delenv("SILICA_LINK_HOST", raising=False)
    assert link.host() == "https://app.silica-agent.dev"
    for ok, said in (("https://gate.test/", "https://gate.test"), ("http://127.0.0.1:8123", "http://127.0.0.1:8123"),
                     ("http://localhost:9", "http://localhost:9")):
        monkeypatch.setenv("SILICA_LINK_HOST", ok)
        assert link.host() == said
    for bad in ("http://gate.test", "http://127.0.0.1.evil.test", "https://gate.test/path", "ftp://gate.test",
                "https://me@gate.test", "https://gate.test/?x=1", "gate.test", "https://gate.test:99999"):
        monkeypatch.setenv("SILICA_LINK_HOST", bad)
        assert link.host() == "", bad
        with pytest.raises(ValueError, match="https"):
            link.start()


# --- linking -------------------------------------------------------------------------------

def test_start_and_poll_link_this_computer_and_keep_the_token_in_one_0600_file(gateway):
    import datetime

    from silica.kernel import account_link as link

    assert link.state() == {"linked": False, "host": gateway.host, "handle": "", "since": "", "expires": ""}
    out = link.start()
    assert out == {"code": CODE, "poll": POLL, "expires_in": 600, "url": f"{gateway.host}/link/confirm?code={CODE}"}
    assert link.poll(POLL) == {"state": "pending"}
    assert not _link_file().exists()
    gateway.confirmed = True
    got = link.poll(POLL)
    assert got["state"] == "linked" and got["linked"] is True and got["handle"] == "marta"
    assert TOKEN not in json.dumps(got)
    # the gateway refuses a request with Origin or Sec-Fetch-Site: the app's server sends neither
    for headers, _ in gateway.posts("/link/start") + gateway.posts("/link/poll"):
        assert "origin" not in headers and "sec-fetch-site" not in headers
    f = _link_file()
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    row = json.loads(f.read_text(encoding="utf-8"))
    assert set(row) == {"token", "handle", "host", "linked_at", "expires"}
    assert (row["token"], row["handle"], row["host"]) == (TOKEN, "marta", gateway.host)
    since = datetime.datetime.fromisoformat(row["linked_at"])
    assert datetime.datetime.fromisoformat(row["expires"]) - since == datetime.timedelta(days=90)
    st = link.state()
    assert st["linked"] and st["handle"] == "marta" and st["since"] == row["linked_at"]
    assert TOKEN not in json.dumps(st)
    # handed out once: the next poll finds the code gone
    assert link.poll(POLL) == {"state": "gone"}
    link.unlink()
    assert not f.exists() and link.state()["linked"] is False
    link.unlink()  # unlinking twice is no error


def test_a_refused_start_says_when_to_try_again_and_a_dead_gateway_says_so(gateway, monkeypatch):
    from silica.kernel import account_link as link

    gateway.start = (429, {"detail": "Too many link codes from this network. Try again later."},
                     {"Retry-After": "121"})
    with pytest.raises(ValueError, match="Too many link codes from this network. Try again in 3 minutes."):
        link.start()
    gateway.start = (200, {"code": "<script>", "poll": POLL, "expires_in": 600}, {})
    with pytest.raises(ValueError, match="link code"):
        link.start()
    monkeypatch.setenv("SILICA_LINK_HOST", "http://127.0.0.1:9")
    with pytest.raises(ValueError, match="could not be reached"):
        link.start()


def test_an_expired_unreadable_or_foreign_link_reads_as_unlinked():
    import datetime

    from silica.kernel import account_link as link, publish

    link._keep(TOKEN, "marta", "https://gate.test")
    assert link.state()["linked"] and publish.can_publish()
    row = json.loads(_link_file().read_text(encoding="utf-8"))
    past = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)).isoformat()
    for broken in ({**row, "expires": past}, {**row, "host": "http://gate.test"}, {**row, "token": ""},
                   {**row, "expires": "soon"}, {k: v for k, v in row.items() if k != "token"}, [row]):
        _link_file().write_text(json.dumps(broken), encoding="utf-8")
        assert link.state()["linked"] is False, broken
        assert not publish.can_publish()
    _link_file().write_text("half a fi", encoding="utf-8")
    assert link.state()["linked"] is False


# --- an unlinked computer only saves files -------------------------------------------------

@pytest.fixture
def memory(tmp_vault, tmp_path, monkeypatch):
    """A computer's vault with a wiki in a nested folder: notes of its own, one made from a book
    answered open, one struck by its globe, a private note outside, and images of every kind."""
    from silica.kernel.publish import add_vault, update_vault
    from silica.kernel.rights import save_answer
    from silica.kernel.vault_manifest import reset_manifest_cache
    from silica.kernel.write.provenance import append_record

    for k in ("SILICA_HOSTED", "SILICA_VIEWS", "SILICA_PUBLIC_BASE", "SILICA_HOST_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SILICA_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("SILICA_PUBLIC_HANDLE", "marta")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    reset_manifest_cache()
    root = Path(tmp_path) / "vault"
    v = tmp_vault
    v.note("Study/Memory/Spacing effect.md",
           "---\ntags: [memory]\n---\n# Spacing effect\n\nIt follows from the [[Forgetting curve]]; "
           "see [[Exam plan June]]. %% my remark %%\n\n![[Pasted image 1.png]]\n\n![hypno](hypnogram.png)\n\n"
           '<img src="hypnogram.png" width="200">\n\n![far](https://example.org/pic.png)\n\n![[diagram.svg]]\n')
    v.note("Study/Memory/Forgetting curve.md", "Ebbinghaus measured it.\n")
    v.note("Study/Memory/Deep/Interleaving.md", "Mixing topics. Back to [[Spacing effect]].\n\n![[held.png]]\n")
    v.note("Study/Memory/Draft.md", "---\npublish: false\n---\nNot ready.\n")
    v.note("Courses/Exam plan June.md", "Private plan.\n")
    v.note("sources/Ebbinghaus.md", '---\nsource_file: "/home/u/Ebbinghaus.pdf"\n---\nMemory decays.\n')
    (root / "Pasted image 1.png").write_bytes(PNG)
    (root / "Study" / "Memory" / "hypnogram.png").write_bytes(JPG)
    (root / "Study" / "Memory" / "diagram.svg").write_text("<svg><script>x()</script></svg>", encoding="utf-8")
    (root / "Study" / "Memory" / "Deep" / "held.png").write_bytes(PNG + b"held")
    assert append_record("Ebbinghaus.md", "Ebbinghaus.md", "run-1", ["Study/Memory/Forgetting curve"],
                         vault_path=str(root)) == "present"
    add_vault("Study/Memory")
    update_vault("memory", {"title": "Memoria", "line": "How remembering works.", "license": "CC BY 4.0"})
    save_answer("folder:Study/Memory", "mine")
    save_answer("Ebbinghaus.pdf", "open", license="CC BY", credit="Ebbinghaus 1885")
    yield root
    reset_manifest_cache()


@pytest.fixture
def client(memory):
    from silica.ui.web import server

    server._reset_session()
    return TestClient(server.app)


def test_an_unlinked_computer_cannot_make_a_wiki_public_or_private(memory, client):
    from silica.kernel import publish
    from silica.ui.web.public_site import build

    for vis in ("public", "private"):
        with pytest.raises(ValueError, match="^Link a Silica account to publish.$"):
            publish.set_visibility("memory", vis)
        r = client.post("/public/vault/memory", json={"visibility": vis})
        assert (r.status_code, r.json()["detail"]) == (400, "Link a Silica account to publish.")
    with pytest.raises(ValueError, match="Link a Silica account"):
        build("memory")
    r = client.post("/public/publish", json={"vault": "memory"})
    assert (r.status_code, r.json()["detail"]) == (400, "Link a Silica account to publish.")
    assert publish.find("memory")["visibility"] == "off"
    view = client.get("/public").json()
    assert view["can_publish"] is False and view["link"]["linked"] is False and "dir" not in view


def test_a_public_wiki_reads_as_off_until_linked_and_its_served_folder_stays(memory, client):
    from silica.kernel import account_link as link, publish

    items = publish.vaults()
    items[0]["visibility"] = "public"
    publish._save_vaults(items, memory)
    live = memory.parent / "public" / "marta" / "live" / "memory"
    live.mkdir(parents=True)
    (live / "index.html").write_text("served by the person", encoding="utf-8")
    assert client.get("/public").json()["vaults"][0]["visibility"] == "off"
    # a title edit writes the row back as it is on disk, never as the view reads it
    client.post("/public/vault/memory", json={"title": "Memoria 2"})
    assert publish.find("memory")["visibility"] == "public"
    # a Public row the person cannot turn Off is still theirs to remove from My wikis
    assert client.post("/public/vault/memory/remove").status_code == 200
    assert (live / "index.html").read_text(encoding="utf-8") == "served by the person"
    publish._save_vaults(items, memory)
    link._keep(TOKEN, "marta", "https://gate.test")
    assert client.get("/public").json()["vaults"][0]["visibility"] == "public"


def test_an_unlinked_computer_still_saves_a_wiki_as_a_file(memory, client):
    r = client.post("/public/vault/memory/file")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["name"] == "Memoria by marta.zip" and out["notes"] == 3
    assert Path(out["path"]).is_file()


# --- what a Publish sends ------------------------------------------------------------------

def _sent(gateway) -> tuple[dict, zipfile.ZipFile]:
    headers, body = gateway.posts("/link/publish")[-1]
    return headers, zipfile.ZipFile(io.BytesIO(body))


def test_send_packs_the_notes_that_go_out_as_written_with_their_images_and_answers(memory, gateway):
    from silica.kernel import account_link as link, publish
    from silica.sources import saved_page

    link._keep(TOKEN, "marta", gateway.host)
    publish.set_visibility("memory", "private")
    publish.update_vault("memory", {"readers": ["sara", "luca"], "listed": True})
    out = link.send("memory")
    assert out == {"vault": "memory", "notes": 3, "url": "https://wikis.test/@marta/memory/",
                   "published_at": "2026-11-08T10:00:00+00:00", "sent": True, "off": False}
    assert publish.find("memory")["published"]
    headers, z = _sent(gateway)
    assert headers["authorization"] == f"Bearer {TOKEN}" and headers["content-type"] == "application/zip"
    assert "origin" not in headers
    names = set(z.namelist())
    assert names == {"wiki.json", "notes/Spacing effect.md", "notes/Forgetting curve.md",
                     "notes/Deep/Interleaving.md", "assets/1-hypnogram.png", "assets/2-pasted-image-1.png"}
    assert not any(n.endswith((".html", ".htm", ".js")) for n in names)
    meta = json.loads(z.read("wiki.json"))
    assert meta == {"link": "", "folder": "Memory", "slug": "memory", "title": "Memoria",
                    "line": "How remembering works.", "license": "CC BY 4.0", "visibility": "private",
                    "answers": {"Forgetting curve.md": {"answer": "open", "license": "CC BY",
                                                        "credit": "Ebbinghaus 1885"}},
                    "readers": ["luca", "sara"], "listed": True}
    # what the receiver checks, run on what was sent
    notes = {n.removeprefix("notes/") for n in names if n.startswith("notes/")}
    head = publish._head(meta, notes)
    assert (head["answers"], head["readers"], head["listed"]) == (meta["answers"], ["luca", "sara"], True)
    assert all(saved_page._ASSET.fullmatch(n.removeprefix("assets/")) for n in names if n.startswith("assets/"))
    assert z.read("assets/1-hypnogram.png") == JPG and z.read("assets/2-pasted-image-1.png") == PNG
    # as written, but each image let out points at its copy; a remote one, an svg the receiver
    # refuses and one the check held back stay as written, and their files never go
    raw = (memory / "Study/Memory/Spacing effect.md").read_text(encoding="utf-8")
    want = (raw.replace("![[Pasted image 1.png]]", "![[Memory/assets/2-pasted-image-1.png]]")
            .replace("![hypno](hypnogram.png)", "![[Memory/assets/1-hypnogram.png|hypno]]")
            .replace('<img src="hypnogram.png" width="200">', "![[Memory/assets/1-hypnogram.png]]"))
    assert z.read("notes/Spacing effect.md").decode() == want
    assert "%% my remark %%" in want and "[[Exam plan June]]" in want and "https://example.org/pic.png" in want
    assert z.read("notes/Deep/Interleaving.md").decode() == \
        (memory / "Study/Memory/Deep/Interleaving.md").read_text(encoding="utf-8")
    assert "Private plan" not in str([z.read(n) for n in names])


def test_send_refuses_past_100_mb_before_anything_leaves(memory, gateway, monkeypatch):
    from silica.kernel import account_link as link, publish
    from silica.sources import saved_page

    link._keep(TOKEN, "marta", gateway.host)
    publish.set_visibility("memory", "public")
    monkeypatch.setattr(saved_page, "WIKI_FILE_MAX", 30)
    with pytest.raises(ValueError, match="past the"):
        link.send("memory")
    assert gateway.posts("/link/publish") == []


def test_each_answer_of_the_account_is_a_sentence_the_view_shows(memory, gateway, monkeypatch):
    from silica.kernel import account_link as link, publish

    link._keep(TOKEN, "marta", gateway.host)
    publish.set_visibility("memory", "public")
    said = {
        409: ({"detail": "A folder named Memory already holds notes of yours: rename one of them to publish "
                         "this wiki."}, "A folder named Memory already holds notes of yours: rename one"),
        503: ({"detail": "this account is busy"}, "busy.*Try again in a minute"),
        413: ({"detail": "too big"}, "past the 100 MB"),
        400: ({"detail": "you can publish 3 wikis at most"}, "Your account refused this wiki: you can publish 3"),
        500: ({"detail": "this account could not write the wiki (No space left on device)"},
              "could not write the wiki \\(No space left on device\\)"),
        418: ({}, "answered 418"),
    }
    for status, (body, match) in said.items():
        gateway.publish = (status, body, {})
        with pytest.raises(ValueError, match=match):
            link.send("memory")
        assert link.state()["linked"]
    gateway.publish = (401, {"detail": "not linked"}, {})
    with pytest.raises(ValueError, match="Link it again"):
        link.send("memory")
    # the account no longer knows the token: the view reads unlinked at once
    assert link.state()["linked"] is False and not _link_file().exists()
    with pytest.raises(ValueError, match="^Link a Silica account to publish.$"):
        link.send("memory")
    link._keep(TOKEN, "marta", "http://127.0.0.1:9")
    with pytest.raises(ValueError, match="could not be reached"):
        link.send("memory")


def test_the_token_is_in_no_log_no_error_and_no_answer_to_the_page(memory, gateway, client, caplog):
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []
    r = client.post("/link/start")
    assert r.status_code == 200 and "poll" not in r.json() and r.json()["code"] == CODE
    seen.append(r.text)
    seen.append(client.post("/link/poll").text)
    gateway.confirmed = True
    r = client.post("/link/poll")
    assert r.json()["state"] == "linked"
    seen += [r.text, client.get("/link/state").text, client.get("/public").text]
    # the account's refusals, the last of which unlinks this computer
    for status in (500, 409, 401):
        gateway.publish = (status, {"detail": "no"}, {})
        r = client.post("/public/publish", json={"vault": "memory"})
        assert r.status_code == 400
        seen.append(r.text)
    from silica.kernel import account_link as link

    link._keep(TOKEN, "marta", "http://127.0.0.1:9")
    try:
        link.send("memory")
    except ValueError as exc:
        seen.append(repr(exc) + str(exc.__cause__) + str(exc.__context__))
    for text in seen + [caplog.text]:
        assert TOKEN not in text and POLL not in text


def test_linking_takes_the_account_s_handle_so_files_and_publish_say_the_same(memory, gateway, client, monkeypatch):
    from silica.kernel import account_link as link, publish

    monkeypatch.delenv("SILICA_PUBLIC_HANDLE")
    assert publish.handle() == "vault" and publish.handle_state()["ask"] is True
    link.start()
    gateway.confirmed = True
    assert link.poll(POLL)["state"] == "linked"
    assert publish._block(memory)["handle"] == "marta"
    assert publish.handle() == "marta" and publish.handle_state() == {"ask": False}
    assert client.post("/public/vault/memory/file").json()["name"] == "Memoria by marta.zip"


def test_an_off_wiki_goes_out_off_and_stays_off_here(memory, gateway, client):
    from silica.kernel import account_link as link, publish

    link._keep(TOKEN, "marta", gateway.host)
    assert client.post("/public/publish", json={"vault": "memory"}).status_code == 200
    day = publish.find("memory")["published"]
    assert day and _sent(gateway)[1].read("wiki.json") and json.loads(_sent(gateway)[1].read("wiki.json"))["visibility"] == "public"
    client.post("/public/vault/memory", json={"visibility": "off"})
    r = client.post("/public/publish", json={"vault": "memory", "off": True})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["off"] is True
    _, z = _sent(gateway)
    assert json.loads(z.read("wiki.json"))["visibility"] == "off" and "notes/Spacing effect.md" in z.namelist()
    # Off here, and the day it last went out stays the day it was published
    assert publish.find("memory")["visibility"] == "off" and publish.find("memory")["published"] == day
    # without the word, Publish on an Off wiki turns it on, as before
    client.post("/public/publish", json={"vault": "memory"})
    assert json.loads(_sent(gateway)[1].read("wiki.json"))["visibility"] == "public"


def test_two_wikis_landing_in_one_folder_of_the_account_and_too_many_readers_are_refused(memory, gateway, tmp_vault):
    from silica.kernel import account_link as link, publish

    link._keep(TOKEN, "marta", gateway.host)
    publish.set_visibility("memory", "public")
    publish.update_vault("memory", {"readers": [f"r{i}" for i in range(101)]})
    with pytest.raises(ValueError, match="names 100 readers at most"):
        link.send("memory")
    publish.update_vault("memory", {"readers": []})
    tmp_vault.note("Other/Memory/Note.md", "Another wiki of the same name.\n")
    publish.add_vault("Other/Memory")
    with pytest.raises(ValueError, match="^Two of your wikis would publish into the folder Memory on your account: rename one.$"):
        link.send("memory")
    assert gateway.posts("/link/publish") == []


def test_a_refused_publish_leaves_an_off_wiki_off(memory, gateway, client, tmp_vault, monkeypatch):
    """Publish on an Off wiki turns it Public only once the account took it (fix round 1, 3c)."""
    from silica.kernel import account_link as link, publish
    from silica.sources import saved_page

    def refused(said: str) -> None:
        r = client.post("/public/publish", json={"vault": "memory"})
        assert (r.status_code, said in r.json()["detail"]) == (400, True), r.text
        assert publish.find("memory")["visibility"] == "off"

    refused("Link a Silica account to publish")
    link._keep(TOKEN, "marta", gateway.host)
    publish.update_vault("memory", {"readers": [f"r{i}" for i in range(101)]})
    refused("100 readers at most")
    publish.update_vault("memory", {"readers": []})
    with monkeypatch.context() as m:
        m.setattr(saved_page, "WIKI_FILE_MAX", 30)
        refused("past the")
    gateway.publish = (409, {"detail": "A folder named Memory already holds notes of yours: rename one of them"}, {})
    refused("rename one of them")
    tmp_vault.note("Other/Memory/Note.md", "Another wiki of the same name.\n")
    publish.add_vault("Other/Memory")
    refused("Two of your wikis")
    assert len(gateway.posts("/link/publish")) == 1  # the 409 alone reached the account


def test_no_exception_carries_the_request_and_the_gateway_s_answers_are_bounded(memory, gateway):
    from silica.kernel import account_link as link

    link._keep(TOKEN, "marta", "http://127.0.0.1:9")
    with pytest.raises(ValueError) as caught:
        link.send("memory")
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    gateway.start = (200, {"code": CODE, "poll": POLL, "expires_in": 99999}, {})
    assert link.start()["expires_in"] == 600
    gateway.start = (200, {"code": CODE, "poll": POLL, "expires_in": 600, "pad": "x" * 70000}, {})
    with pytest.raises(ValueError, match="answered with more than Silica reads"):
        link.start()


def test_a_vault_yaml_that_is_not_utf8_keeps_the_link(memory, gateway, monkeypatch):
    from silica.kernel import account_link as link

    (memory / "vault.yaml").write_bytes(b"public:\n  title: \xff\xfe\n")
    link.start()
    gateway.confirmed = True
    assert link.poll(POLL)["state"] == "linked"
    assert link.state()["linked"] and _link_file().exists()


def test_a_linked_computer_shows_the_listing_request_its_publish_carries():
    js = (Path(__file__).parents[1] / "silica" / "ui" / "web" / "static" / "app-public.js").read_text(encoding="utf-8")
    assert 'v.visibility === "public" && (st.url || st.link)) grid.append(listingBox(v))' in js


def test_an_unlinked_removal_keeps_the_served_folder_through_a_later_page_write(memory, client):
    from silica.kernel import publish

    items = publish.vaults()
    items[0]["visibility"] = "public"
    publish._save_vaults(items, memory)
    (memory / "Explore").mkdir()
    (memory / "Explore" / "A.md").write_text("a\n", encoding="utf-8")
    publish.add_vault("Explore")
    live = memory.parent / "public" / "marta" / "live"
    (live / "memory").mkdir(parents=True)
    (live / "memory" / "index.html").write_text("served by the person", encoding="utf-8")
    assert client.post("/public/vault/memory/remove").status_code == 200
    # a visibility change elsewhere rewrites the Page, which sweeps a folder only for a row it has
    assert client.post("/public/vault/explore", json={"visibility": "off"}).status_code == 200
    assert (live / "memory" / "index.html").read_text(encoding="utf-8") == "served by the person"


def test_an_unlinked_computer_writes_and_sweeps_nothing_of_its_served_page(memory, client):
    from silica.kernel import publish

    live = memory.parent / "public" / "marta" / "live"
    (live / "_s").mkdir(parents=True)
    (live / "_s" / "pages.css").write_text("css", encoding="utf-8")
    (live / "index.html").write_text("the person's page", encoding="utf-8")
    (live / "old-wiki").mkdir()
    assert not any(v["visibility"] == "public" for v in publish.vaults())
    assert client.post("/public/vault/memory", json={"visibility": "off"}).status_code == 200
    assert (live / "index.html").read_text(encoding="utf-8") == "the person's page"
    assert (live / "_s" / "pages.css").is_file() and (live / "old-wiki").is_dir()


def test_a_listing_asked_on_a_computer_waits_for_the_next_publish_to_reach_the_account(memory, gateway, client):
    from silica.kernel import account_link as link

    link._keep(TOKEN, "marta", gateway.host)
    client.post("/public/vault/memory", json={"visibility": "public"})
    v = client.post("/public/vault/memory", json={"listed": True}).json()["vaults"][0]
    assert (v["listed"], v["sent_listed"]) == (True, False)
    assert client.post("/public/publish", json={"vault": "memory"}).status_code == 200
    assert json.loads(_sent(gateway)[1].read("wiki.json"))["listed"] is True
    assert client.get("/public").json()["vaults"][0]["sent_listed"] is True
    js = (Path(__file__).parents[1] / "silica" / "ui" / "web" / "static" / "app-public.js").read_text(encoding="utf-8")
    box = js[js.index("function listingBox("):js.index("// --- the check before Publish")]
    assert 'box("Asked", listingRow("Asked. It reaches your account with your next Publish.", "Withdraw", false, v))' in box


def _listing_box() -> str:
    js = (Path(__file__).parents[1] / "silica" / "ui" / "web" / "static" / "app-public.js").read_text(encoding="utf-8")
    return js[js.index("function listingBox("):js.index("// --- the check before Publish")]


def test_a_withdrawal_on_a_computer_also_waits_for_the_next_publish(memory, gateway, client):
    from silica.kernel import account_link as link

    link._keep(TOKEN, "marta", gateway.host)
    client.post("/public/vault/memory", json={"visibility": "public", "listed": True})
    client.post("/public/publish", json={"vault": "memory"})
    v = client.post("/public/vault/memory", json={"listed": False}).json()["vaults"][0]
    # the account still holds the request the last Publish carried
    assert (v["listed"], v["sent_listed"]) == (False, True)
    box = _listing_box()
    # before the plain "Not listed", which would say search engines leave it alone
    assert box.index("v.listed !== v.sent_listed") < box.index('if (!v.listed) return box("Not listed"')
    assert '"Withdrawn. It reaches your account with your next Publish.", "Ask to list", true, v' in box
    client.post("/public/publish", json={"vault": "memory"})
    assert json.loads(_sent(gateway)[1].read("wiki.json"))["listed"] is False
    assert client.get("/public").json()["vaults"][0]["sent_listed"] is False


def test_a_hosted_wiki_a_computer_keeps_says_where_its_listing_changes():
    box = _listing_box()
    assert 'v.computer ? listingRow(`${text} Change it from your computer.`)' in box
    # every state with a button goes through that row, so a computer's wiki offers none here; the
    # three other rows are a computer's own view of its request, behind pubState.link
    assert box.count("listingRow(") == 6 and box.count('row("') + box.count("row(`") == 4
    assert box.count("if (pubState.link && v.listed") == 2


def test_a_linked_computer_writes_and_sweeps_nothing_of_its_served_page(memory, gateway, client):
    from silica.kernel import account_link as link
    from silica.ui.web.public_site import write_page

    link._keep(TOKEN, "marta", gateway.host)
    live = memory.parent / "public" / "marta" / "live"
    (live / "_s").mkdir(parents=True)
    for name, text in (("index.html", "the person's page"), ("vaults.json", "[]"), ("_s/pages.css", "css")):
        (live / name).write_text(text, encoding="utf-8")
    client.post("/public/vault/memory", json={"visibility": "public"})
    client.post("/public/vault/memory", json={"visibility": "off"})
    write_page()
    assert {p.relative_to(live).as_posix(): p.read_text(encoding="utf-8") for p in live.rglob("*") if p.is_file()} \
        == {"index.html": "the person's page", "vaults.json": "[]", "_s/pages.css": "css"}


def test_a_linked_computer_moves_and_writes_nothing_in_its_served_folders(memory, gateway, client):
    from silica.kernel import account_link as link

    link._keep(TOKEN, "marta", gateway.host)
    root = memory.parent / "public" / "marta"
    (root / "live" / "memory").mkdir(parents=True)
    (root / "paused" / "old").mkdir(parents=True)
    manifest = b'{"slug": "memory", "listed": false, "the person": "serving this"}'
    (root / "live" / "memory" / "vault.json").write_bytes(manifest)
    (root / "paused" / "old" / "index.html").write_bytes(b"kept aside by hand")

    def files() -> dict[str, bytes]:
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    before = files()
    for change in ({"visibility": "public"}, {"listed": True}, {"listed": False}, {"visibility": "private"},
                   {"visibility": "off"}, {"visibility": "public"}):
        assert client.post("/public/vault/memory", json=change).status_code == 200, change
        assert files() == before, change
    client.post("/public/vault/memory", json={"visibility": "off"})
    assert client.post("/public/vault/memory/remove").status_code == 200
    assert files() == before


def test_unlink_forgets_what_the_account_was_told(memory, gateway, client, tmp_vault):
    from silica.kernel import account_link as link, publish

    (memory / "Explore").mkdir()
    (memory / "Explore" / "A.md").write_text("a\n", encoding="utf-8")
    publish.add_vault("Explore")
    items = publish.vaults()
    items[1]["published"] = "2026-10-01"  # a build of this computer's own, before ADR-0157
    publish._save_vaults(items, memory)
    link._keep(TOKEN, "marta", gateway.host)
    client.post("/public/vault/memory", json={"visibility": "public", "listed": True})
    client.post("/public/publish", json={"vault": "memory"})
    v = publish.find("memory")
    assert v["sent"] and v["sent_listed"] and v["published"]
    assert client.post("/link/unlink").json()["linked"] is False
    v, e = publish.find("memory"), publish.find("explore")
    assert (v["sent"], v["sent_listed"], v["published"], v["listed"]) == (False, False, "", True)
    assert e["published"] == "2026-10-01"
    # linked again, to any account: the request reads as one it has not seen
    link._keep(TOKEN, "marta", gateway.host)
    assert client.get("/public").json()["vaults"][0]["sent_listed"] is False


def test_a_link_to_another_account_forgets_what_the_last_one_was_told(memory, gateway, client):
    """Renewed for the same account, the rows keep what it holds; linked to another, they read as
    never sent, so an Off wiki offers no "Turn off on your account" for a copy that account never had."""
    from silica.kernel import account_link as link, publish

    link._keep(TOKEN, "marta", gateway.host)
    client.post("/public/vault/memory", json={"visibility": "public", "listed": True})
    assert client.post("/public/publish", json={"vault": "memory"}).status_code == 200
    sent = publish.find("memory")
    assert sent["sent"] and sent["sent_listed"] and sent["published"]

    def relink(handle: str) -> dict:
        gateway.confirmed, gateway.handed, gateway.handle = True, False, handle
        link.start()
        assert link.poll(POLL)["state"] == "linked"
        return publish.find("memory")

    assert relink("marta") == sent
    v = relink("luca")
    assert (v["sent"], v["sent_listed"], v["published"], v["listed"]) == (False, False, "", True)
    assert client.get("/public").json()["vaults"][0]["sent"] is False


def test_a_computer_shows_no_publication_of_its_own_from_before_the_link(memory, gateway, client):
    """A computer's state.json is from a build before ADR-0157: what its account published is the
    account's to say, so the view reads no date, no count of new notes and no blocked reason from it."""
    from silica.kernel import account_link as link

    root = memory.parent / "public" / "marta"
    root.mkdir(parents=True)
    (root / "state.json").write_text(json.dumps({"vaults": {"memory": {
        "published_at": "2026-10-01T10:00:00+00:00", "notes": ["Memory/Old.md"], "blocked": ["a reason"]}}}))
    link._keep(TOKEN, "marta", gateway.host)
    client.post("/public/vault/memory", json={"visibility": "public"})
    v = client.get("/public").json()["vaults"][0]
    assert (v["published_at"], v["new"], v["blocked"], v["review"], v["sent"]) == ("", 0, [], {}, False)
    # with no review to read, a request the account holds says where its answer is, never "Waiting"
    box = _listing_box()
    assert box.index("if (pubState.link && v.listed) return") < box.index('return box("Waiting"')
    assert "Asked. Your account's review decides; the result shows in Wikis → My wikis on ${linkHost(" in box


@pytest.mark.parametrize("status", [502, 504, 524])
def test_an_edge_that_cuts_the_publish_says_to_check_the_account_first(memory, gateway, client, status):
    """The edge cut a 53 MB POST at 125 s (2026-11-02): the account may have taken the wiki."""
    from silica.kernel import account_link as link

    link._keep(TOKEN, "marta", gateway.host)
    gateway.publish = (status, {}, {})
    client.post("/public/vault/memory", json={"visibility": "public"})
    r = client.post("/public/publish", json={"vault": "memory"})
    h = gateway.host.removeprefix("http://")
    assert (r.status_code, r.json()["detail"]) == (400, (
        f"The connection to {h} was cut before your account answered; a large wiki may need a faster "
        f"connection. Check Wikis → My wikis on {h} before publishing again."))


def test_the_account_s_tier_refusal_reaches_the_view_as_its_sentence(memory, gateway, client):
    from silica.kernel import account_link as link

    link._keep(TOKEN, "marta", gateway.host)
    gateway.publish = (400, {"detail": "Private needs a paid account on Silica hosted"}, {})
    assert client.post("/public/vault/memory", json={"visibility": "private"}).status_code == 200
    r = client.post("/public/publish", json={"vault": "memory"})
    assert (r.status_code, r.json()["detail"]) == \
        (400, "Your account refused this wiki: Private needs a paid account on Silica hosted.")


# --- the app's routes ----------------------------------------------------------------------

def test_the_link_routes_keep_the_poll_on_the_server_and_publish_sends(memory, gateway, client):
    from silica.kernel import publish

    assert client.post("/link/poll").json() == {"state": "gone"}  # nothing started
    started = client.post("/link/start").json()
    assert started == {"code": CODE, "expires_in": 600, "url": f"{gateway.host}/link/confirm?code={CODE}"}
    assert client.post("/link/poll").json() == {"state": "pending"}
    gateway.confirmed = True
    st = client.post("/link/poll").json()
    assert st["state"] == "linked" and st["host"] == gateway.host
    assert client.post("/link/poll").json() == {"state": "gone"}  # the server forgot its poll
    view = client.get("/public").json()
    assert view["can_publish"] and view["link"]["handle"] == "marta" and view["private"] == "ok"
    r = client.post("/public/publish", json={"vault": "memory"})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["result"]["url"] == "https://wikis.test/@marta/memory/" and res["result"]["sent"]
    assert res["vaults"][0]["visibility"] == "public" and res["vaults"][0]["published"]
    assert publish.find("memory")["visibility"] == "public"
    # a computer writes no pages of its own
    assert not (memory.parent / "public" / "marta" / "live" / "memory").exists()
    assert client.post("/link/unlink").json()["linked"] is False
    assert client.get("/link/state").json()["linked"] is False
    assert client.post("/link/start", headers={"Origin": "https://evil.test"}).status_code == 403


def test_the_hosted_service_has_no_link_of_its_own(memory, client, monkeypatch):
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "all")
    assert client.get("/link/state").status_code == 404
    for path in ("/link/start", "/link/poll", "/link/unlink"):
        assert client.post(path).status_code == 404
    # the graph's own /link is untouched
    assert client.get("/link", params={"from": "x", "to": "y"}).status_code != 405
