"""The defects an end-to-end tester found in the mail import (ADR-0090 Part A)
on 2026-10-12, each pinned by the tester's own steps on small synthetic
fixtures (tests/mail_fixtures.py). Gate 1 is the bar: 0 hidden strings and 0
image URLs in a note, and no live markup a stranger wrote."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from silica.cli import _handle_direct_shortcut
from silica.sources import mail
from tests.mail_fixtures import (
    ME, ME_FULL, PROF, STRANGER, TRACK, field_photos, msg, poster_session_html, write_eml,
    write_mbox, write_raw,
)


def _notes(tmp_path) -> list[Path]:
    return sorted((tmp_path / "vault" / "sources" / "mail").glob("*.md"))


def _thread(*messages):
    [thread] = mail.threads_of([mail._mail(m) for m in messages])
    return thread


@pytest.fixture
def web(tmp_vault, tmp_path):
    from fastapi.testclient import TestClient

    from silica.ui.web import server

    server._reset_session()
    return TestClient(server.app), server


def _inbox(tmp_path) -> Path:
    from silica.config import CONFIG
    from silica.kernel.vault_manifest import active_write_dir, resolve_inbox_dir

    rel = resolve_inbox_dir(CONFIG.vault_path, active_write_dir(), CONFIG.inbox_dir or "Inbox")
    inbox = tmp_path / "vault" / rel
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox


# --- 1. BLOCKER: tracking pixels ------------------------------------------------------

def test_opening_an_imported_note_in_the_drawer_requests_no_image(web, tmp_path):
    """The tester's steps: import the "Field photos" thread, open the note in the
    drawer. The drawer draws a note through POST /note/render; three URLs were
    requested from it, and an escaped `&lt;img&gt;` came out a live tag."""
    tc, _ = web
    box = write_mbox(tmp_path / "plain.mbox", field_photos())
    _handle_direct_shortcut(f"/mail import {box} --me={ME} --apply", [])
    [note] = _notes(tmp_path)
    text = note.read_text(encoding="utf-8")
    assert "FIELD-VISIBLE" in text and "Photos attached." in text
    assert "track.example" not in text                    # gate 1: 0 image URLs in the note
    html = tc.post("/note/render", json={"text": text}).json()["html"]
    assert "<img" not in html and "track.example" not in html


def test_no_live_html_tag_reaches_a_mail_note_whatever_the_part_type(web):
    tc, _ = web
    plain = msg(mid="<a@x.example>", plain=(
        'Look: <iframe src="https://evil.example/x"></iframe> <b>bold</b> <!-- note -->\n'
        "<script>alert(1)</script> <details open>x</details> and 3 < 4 > 2"))
    html = msg(mid="<b@x.example>", irt="<a@x.example>", refs=("<a@x.example>",),
               date="2026-09-02 10:00",
               html="<p>Shown &lt;script&gt;alert(2)&lt;/script&gt; and &lt;b&gt;x&lt;/b&gt;</p>")
    body = mail.render(_thread(plain, html))
    assert not re.search(r"<[A-Za-z!/?]", body), body
    assert "iframe" in body and "alert(2)" in body and "3 < 4 > 2" in body  # text, not dropped
    rendered = tc.post("/note/render", json={"text": body}).json()["html"]
    for tag in ("<iframe", "<script", "<b>", "<details"):
        assert tag not in rendered, tag
    assert "&lt;b&gt;bold" in rendered                    # it reads as the text it was


def test_every_form_of_image_reference_is_dropped_whole(web):
    tc, _ = web
    body = mail.render(_thread(msg(mid="<a@x.example>", plain=(
        f"Ref ![logo][r] and ![alt [nested]]({TRACK}/nested.gif) and ![[photo.png]]\n\n"
        f"[r]: {TRACK}/ref.gif\n\n"
        f"Bare {TRACK}/open.png?u=1 and <{TRACK}/auto.jpg> and www.track.example/w.jpeg\n"
        f"<IMG\nSRC='{TRACK}/upper.gif'> data:image/gif;base64,R0lGODlhAQABAAAAACw= "
        f"<picture><source srcset=\"{TRACK}/s.webp\"></picture> KEPT-TEXT"))))
    assert "track.example" not in body and "data:image" not in body
    assert "![" not in body and "KEPT-TEXT" in body
    rendered = tc.post("/note/render", json={"text": body}).json()["html"]
    assert "<img" not in rendered


# --- 2. MAJOR: hidden HTML text slips through ------------------------------------------

@pytest.mark.parametrize("element", [
    '<span style="display:none" style="color:red">HIDDEN-1</span>',
    '<span style="color:red" style="display:none">HIDDEN-1</span>',
    '<span style="/**/display:none">HIDDEN-1</span>',
    '<span style="display:/**/none">HIDDEN-1</span>',
    # `\6c ` is "l". The tester's `disp\61 lay` spells "dispalay", which a
    # browser ignores too: that text is shown, so it is no hidden string.
    r'<span style="disp\6c ay:none">HIDDEN-1</span>',
    r'<span style="visi\000062ility:hidden">HIDDEN-1</span>',
    r'<span style="font-size:\30 px">HIDDEN-1</span>',
    '<span style="opacity:/* x */0">HIDDEN-1</span>',
])
def test_html_hidden_by_a_parser_trick_never_reaches_the_note(element):
    html = f"<p>VISIBLE-1</p>{element}<p>VISIBLE-2</p>"
    body = mail.render(_thread(msg(mid="<a@x.example>", html=html)))
    assert "VISIBLE-1" in body and "VISIBLE-2" in body
    assert "HIDDEN" not in body


# --- 3. MAJOR: a chat drop of mail -------------------------------------------------------

def _capture_turn(server, monkeypatch) -> dict:
    ran: dict = {}

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        ran["text"] = next(m["content"] for m in messages if m["role"] == "user")
        messages.append({"role": "assistant", "content": "ok"})
        return "ok"

    monkeypatch.setattr(server, "run_agent", fake_run_agent)
    return ran


def test_a_mail_file_dropped_into_the_chat_writes_nothing_and_the_turn_says_where_mail_goes(
        web, tmp_path, monkeypatch):
    """The tester's steps: a Mail-row import, then a `.eml` dropped into the
    composer, then /revert. The drop wrote a note outside any undo run, so
    /revert took back the Mail-row import and left the dropped note."""
    from silica.sources.mail_connector import MAIL_BLOCK

    tc, server = web
    ran = _capture_turn(server, monkeypatch)
    inbox = _inbox(tmp_path)
    write_mbox(inbox / "export.mbox", [msg(frm=ME_FULL, to=PROF, mid="<a1@x.example>",
                                           plain="Can we meet?")])
    assert MAIL_BLOCK.act("import", {"path": "Inbox/export.mbox", "me": ME})["written"] == 1
    eml = bytes(msg(frm=ME_FULL, to=PROF, subject="Dropped thread", mid="<d1@x.example>",
                    date="2026-09-22 09:00", plain="DROP-EML"))
    r = tc.post("/nucleate", files=[("files", ("drop.eml", eml, "message/rfc822")),
                                    ("files", ("note.md", b"# Hi\n\nbody", "text/markdown"))])
    assert r.status_code == 200
    assert len(_notes(tmp_path)) == 1                     # the drop wrote no mail note
    assert not (inbox / "drop.eml").exists()               # nor the file itself
    assert "Settings › Connectors › Mail" in ran["text"] and "/mail import" in ran["text"]
    assert "drop.eml" in ran["text"] and "Inbox/note.md" in ran["text"]
    assert "Code skeleton stubs" not in ran["text"]
    _handle_direct_shortcut("/revert", [])
    assert _notes(tmp_path) == []                          # /revert took back the import, all of it


def test_a_dropped_mbox_says_why_instead_of_no_files_could_be_staged(web, tmp_path, monkeypatch):
    tc, server = web
    ran = _capture_turn(server, monkeypatch)
    box = write_mbox(tmp_path / "many.mbox", [
        msg(frm=STRANGER, mid="<s1@x.example>", plain="One."),
        msg(frm=STRANGER, mid="<s2@x.example>", subject="Other", plain="Two."),
    ])
    r = tc.post("/nucleate", files=[("files", ("many.mbox", box.read_bytes(), "application/mbox"))])
    assert r.status_code == 200
    assert "(no files could be staged)" not in ran["text"]
    assert "Settings › Connectors › Mail" in ran["text"] and "many.mbox" in ran["text"]
    assert _notes(tmp_path) == [] and not (_inbox(tmp_path) / "many.mbox").exists()


# --- 4. Security: an upload through a planted symlink ------------------------------------

def test_an_upload_never_writes_through_a_planted_symlink(web, tmp_path, monkeypatch):
    tc, server = web
    monkeypatch.setattr(server, "run_agent", lambda messages, *a, **kw: "ok")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("ORIGINAL", encoding="utf-8")
    inbox = _inbox(tmp_path)
    (inbox / "victim.eml").symlink_to(victim)
    (inbox / "victim.md").symlink_to(victim)

    r = tc.post("/mail/upload", params={"name": "victim.eml"}, content=b"From: a@x.example\n\nbody\n")
    assert r.status_code == 400 and "link" in r.json()["detail"]
    r = tc.post("/nucleate", files=[("files", ("victim.md", b"# overwrite", "text/markdown"))])
    assert r.status_code == 400
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"


# --- 5. MINOR ---------------------------------------------------------------------------

@pytest.fixture
def box(tmp_vault, tmp_path):
    """One thread Ada took part in, one newsletter."""
    return write_mbox(tmp_path / "box.mbox", [
        msg(frm=ME_FULL, to=PROF, mid="<a1@x.example>", plain="Can we meet?"),
        msg(mid="<a2@x.example>", irt="<a1@x.example>", refs=("<a1@x.example>",),
            subject="Re: Thesis draft", date="2026-09-02 09:00", plain="Thursday.", delivered_to=ME),
        msg(frm="Weekly <news@list.example>", mid="<n1@x.example>", subject="Offers",
            date="2026-09-03 09:00", plain="Buy.", delivered_to=ME),
    ])


@pytest.mark.parametrize("limit", ["-1", "0", "abc"])
def test_a_limit_that_is_not_one_or_more_is_refused_by_the_command(box, tmp_path, capsys, limit):
    _handle_direct_shortcut(f"/mail import {box} --me={ME} --limit={limit} --apply", [])
    out = capsys.readouterr().out
    assert "--limit takes a whole number of threads, 1 or more" in out
    assert _notes(tmp_path) == []


@pytest.mark.parametrize("limit", ["abc", -1, "0"])
def test_a_limit_that_is_not_one_or_more_is_refused_by_the_mail_row(tmp_vault, tmp_path, limit):
    from silica.sources.mail_connector import MAIL_BLOCK

    write_mbox(_inbox(tmp_path) / "export.mbox", [msg(frm=ME_FULL, mid="<a@x.example>", plain="x")])
    out = MAIL_BLOCK.act("plan", {"path": "Inbox/export.mbox", "limit": limit})
    assert out == {"ok": False, "error": "--limit takes a whole number of threads, 1 or more"}


def _takeout(tmp_path) -> Path:
    return write_mbox(tmp_path / "takeout.mbox", [
        msg(frm=ME_FULL, to=PROF, mid="<p1@x.example>", subject="Promo reply", plain="Unsubscribe me.",
            labels="Sent,Category Promotions", thrid="1"),
        msg(frm=ME_FULL, to=PROF, mid="<t1@x.example>", subject="Chapter two", plain="Draft.",
            labels="Sent,Thesis", thrid="2", date="2026-09-02 10:00"),
    ])


def test_a_quoted_label_with_a_space_is_one_flag_value(tmp_vault, tmp_path, capsys):
    box = _takeout(tmp_path)
    _handle_direct_shortcut(f'/mail import {box} --label="Category Promotions"', [])
    out = capsys.readouterr().out
    assert "1 threads in the file" in out and "Promo reply" in out and "Chapter two" not in out


def test_a_label_matches_whatever_its_case(tmp_vault, tmp_path):
    p = mail.plan(_takeout(tmp_path), label="thesis")
    assert [t.mails[0].subject for t in p.write] == ["Chapter two"]


@pytest.mark.parametrize("name, data", [
    ("notmail.txt", b"plain text not mail"),
    ("malformed.mbox", b"this is not an mbox\nno From_ line at all\n\x00\x01\x02 binary\n"),
    ("junk.eml", bytes(range(256)) * 4),
    ("empty.eml", b""),
], ids=["txt", "mbox-without-from-line", "binary-eml", "empty-eml"])
def test_a_file_that_is_not_mail_is_named_as_such(tmp_vault, tmp_path, capsys, name, data):
    path = write_raw(tmp_path / name, data)
    _handle_direct_shortcut(f"/mail import {path}", [])
    out = capsys.readouterr().out
    assert f"{name} is not a mail file" in out and "0 threads" not in out


def test_status_and_gate6_stop_counting_threads_a_revert_took_back(tmp_vault, tmp_path, capsys):
    from silica.sources.mail_connector import MAIL_BLOCK

    box = write_mbox(tmp_path / "box.mbox", [msg(
        frm=ME_FULL, to=PROF, mid="<p1@x.example>", plain="Paper attached.",
        attachments=(("paper.pdf", "application/pdf", b"%PDF-1.4"),))])
    _handle_direct_shortcut(f"/mail import {box} --me={ME} --apply", [])
    assert MAIL_BLOCK.status()["imported"] == 1 and mail.gate6()["threads"] == 1
    _handle_direct_shortcut("/revert", [])
    capsys.readouterr()
    _handle_direct_shortcut("/mail", [])
    assert "0 threads imported" in capsys.readouterr().out
    assert MAIL_BLOCK.status()["imported"] == 0
    assert mail.gate6() == {"threads": 0, "with_pdf": 0, "nucleated": 0, "fires": False}
    assert mail.recorded_paths()                           # the taint record keeps the path


def test_a_subject_with_a_wikilink_or_a_tag_makes_no_link_and_no_tag(tmp_vault, tmp_path):
    from silica.driver import DRIVER

    box = write_mbox(tmp_path / "box.mbox", [msg(
        frm=ME_FULL, to=PROF, mid="<p2@x.example>", subject="Notes [[Secret]] #tag ^block | pipe",
        plain="Wikilink-ish subject.")])
    _handle_direct_shortcut(f"/mail import {box} --me={ME} --apply", [])
    [note] = _notes(tmp_path)
    h1 = next(ln for ln in note.read_text(encoding="utf-8").splitlines() if ln.startswith("# "))
    assert h1 == "# Notes Secret tag block | pipe"
    rel = note.relative_to(tmp_path / "vault").as_posix()
    assert DRIVER.links(rel) == []                         # no graph edge to a "Secret" node


def test_a_reply_without_a_message_id_refreshes_its_thread(tmp_vault, tmp_path):
    from silica.sources.registry import stage

    first = write_mbox(tmp_path / "first.mbox", [
        msg(frm=ME_FULL, to=PROF, mid="<a1@x.example>", plain="Mine."),
        msg(mid="<a2@x.example>", irt="<a1@x.example>", refs=("<a1@x.example>",), plain="Reply.",
            date="2026-09-02 10:00"),
    ])
    for t in mail.plan(first, me=(ME,)).write:
        assert stage(mail.MAIL, f"{first}#{t.key}")["status"] == "ok"
    grown = write_mbox(tmp_path / "grown.mbox", [
        msg(frm=ME_FULL, to=PROF, mid="<a1@x.example>", plain="Mine."),
        msg(mid="<a2@x.example>", irt="<a1@x.example>", refs=("<a1@x.example>",), plain="Reply.",
            date="2026-09-02 10:00"),
        msg(irt="<a2@x.example>", refs=("<a1@x.example>", "<a2@x.example>"), plain="NO-ID-REPLY",
            date="2026-09-03 10:00"),
    ])
    p = mail.plan(grown, me=(ME,))
    assert p.grown == 1 and len(p.write) == 1
    out = stage(mail.MAIL, f"{grown}#{p.write[0].key}")
    assert "NO-ID-REPLY" in (tmp_path / "vault" / out["note_path"]).read_text(encoding="utf-8")
    assert mail.plan(grown, me=(ME,)).write == []          # and once in, it is present


def test_two_header_less_messages_are_two_threads(tmp_path):
    from email.message import EmailMessage

    def bare(text):
        m = EmailMessage()
        m.set_content(text)
        return m

    box = write_mbox(tmp_path / "bare.mbox", [bare("FIRST body"), bare("SECOND body")])
    threads = mail.threads_of(mail.parse(box))
    assert len(threads) == 2


def test_nul_and_esc_bytes_never_reach_the_note(tmp_vault, tmp_path):
    path = write_raw(tmp_path / "ctl.eml", (
        b"From: Dr Grey <grey@uni.example>\nTo: " + ME_FULL.encode() + b"\n"
        b"Subject: Bell\x07 and \x1b[31mred\n"
        b"Date: Mon, 01 Sep 2026 10:00:00 +0000\nMessage-ID: <c@x.example>\n"
        b"Content-Type: text/plain; charset=utf-8\n\nNUL\x00here ESC\x1b[2J there\x7f done\n"))
    [thread] = mail.threads_in(path)
    body = mail.render(thread)
    assert not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", body), repr(body)
    assert "NULhere" in body and "done" in body
    assert not re.search(r"[\x00-\x1f\x7f-\x9f]", mail.note_path(thread))


def test_a_raw_8bit_subject_is_read_as_the_text_it_was(tmp_vault, tmp_path):
    path = write_raw(tmp_path / "latin.eml", (
        b"From: =?iso-8859-1?q?Jos=E9_Pe=F1a?= <jose@elsewhere.example>\n"
        b"To: " + ME_FULL.encode() + b"\nSubject: Caf\xe9 tomorrow\n"
        b"Date: Tue, 15 Sep 2026 09:00:00 +0000\nMessage-ID: <e3@x.example>\n"
        b"Content-Type: text/plain; charset=iso-8859-1\n\nMa\xf1ana.\n"))
    [thread] = mail.threads_in(path)
    assert mail.note_path(thread).endswith(f"2026-09-15 Café tomorrow {mail.key6('<e3@x.example>')}.md")
    assert mail.render(thread).startswith("# Café tomorrow\n")
    assert thread.mails[0].sender == "José Peña"


def test_an_unquoted_bracket_in_from_keeps_the_sender_and_the_address(tmp_vault, tmp_path):
    path = write_raw(tmp_path / "rich.eml", (
        b"From: [bold red]Dr Evil[/bold red] [link=https://evil.example]Click[/link] <evil@evil.example>\n"
        b"To: " + ME_FULL.encode() + b"\nSubject: Grant\nDate: Wed, 09 Sep 2026 09:00:00 +0000\n"
        b"Message-ID: <r1@evil.example>\nContent-Type: text/plain\n\nGrant offer body.\n"))
    [thread] = mail.threads_in(path)
    m = thread.mails[0]
    assert m.address == "evil@evil.example" and "Dr Evil" in m.sender
    body = mail.render(thread)
    assert "unknown sender" not in body and "(evil@evil.example), 2026-09-09" in body
    stub = mail.MAIL.to_stub(mail.MAIL.read(f"{path}#{thread.key}"))
    assert "evil@evil.example" in stub.body.split("---", 2)[1]   # mail_from is not ''


@pytest.mark.parametrize("attribution", [
    "On Mon, Sep 1, 2026 at 10:00 AM Ada Student <ada.student@uni.example> wrote:",
    "On Mon, Sep 1, 2026 at 10:00 AM Ada Student <\nada.student@uni.example> wrote:",
    "Il giorno lun 1 set 2026 alle ore 10:00 Ada Student <ada.student@uni.example> ha scritto:",
    "Am Mo., 1. Sept. 2026 um 10:00 Uhr schrieb Ada Student <ada.student@uni.example>:",
])
def test_the_attribution_line_goes_with_the_quote_it_introduces(attribution):
    root = msg(frm=ME_FULL, to=PROF, mid="<a@x.example>", plain="ORIGINAL question about chapter two.")
    reply = msg(mid="<b@x.example>", irt="<a@x.example>", refs=("<a@x.example>",),
                date="2026-09-02 09:00", subject="Re: Thesis draft",
                plain=f"REPLY fine by me.\n\n{attribution}\n> ORIGINAL question about chapter two.\n")
    body = mail.render(_thread(root, reply))
    assert "REPLY fine by me." in body and body.count("ORIGINAL question") == 1
    for word in ("wrote:", "scritto:", "schrieb", "ada.student@uni.example>"):
        assert word not in body, word


def test_an_attribution_stays_when_its_quote_stays():
    reply = msg(mid="<b@x.example>", irt="<gone@x.example>", refs=("<gone@x.example>",),
                plain="REPLY\n\nOn Mon Ada wrote:\n> QUOTED-ONLY-HERE")
    body = mail.render(_thread(reply))
    assert "On Mon Ada wrote:" in body and "QUOTED-ONLY-HERE" in body


def test_nucleate_of_an_eml_names_no_empty_code_ref(tmp_vault, tmp_path, capsys):
    from silica import cli

    mail.remember_addresses([ME])
    eml = write_eml(tmp_path / "thread.eml", msg(frm=ME_FULL, to=PROF, mid="<e@x.example>",
                                                 plain="Can we meet?"))
    cli._sc_nucleate([str(eml)])
    out = capsys.readouterr().out
    assert "Wrote" in out and "code_ref" not in out


def test_supported_types_offers_mail_to_the_mail_row_and_not_to_the_composer(web):
    tc, _ = web
    d = tc.get("/supported_types").json()
    assert d["mail"] == [".eml", ".mbox"]
    assert not {".eml", ".mbox"} & set(d["extensions"])


# --- coordinator, 2026-10-12: links and tags in a body, three more hiding styles ---------

_TAG = re.compile(r"(?<!\S)#(?=[\w/-]*[^\W\d])[\w/-]+")   # Obsidian's inline tag


def test_links_and_tags_in_a_body_make_no_node_no_edge_and_no_tag(web, tmp_vault, tmp_path):
    """A stranger's `[[Secret]]` in a body made a node of the Explore graph and
    an edge from the mail note, and `#urgent` a tag in the vault."""
    from silica.kernel.link import autolink
    from silica.kernel.recall.graph_export import build_graph_data

    tc, _ = web
    tmp_vault.note("Notes/Existing.md", "# Existing\n\nThe owner's own note.\n")
    box = write_mbox(tmp_path / "links.mbox", [msg(
        frm=ME_FULL, to=PROF, mid="<l1@x.example>", subject="Plans",
        plain=("See [[Secret]] and [[Existing|the note]], ![[Existing]], #urgent and (#later).\n"
               "Issue #12 and https://site.example/page#anchor stay as written."))])
    _handle_direct_shortcut(f"/mail import {box} --me={ME} --apply", [])
    [note] = _notes(tmp_path)
    rel = note.relative_to(tmp_path / "vault").as_posix()
    body = note.read_text(encoding="utf-8").split("---", 2)[2]
    assert "Secret" in body and "urgent" in body            # read as text, not dropped
    assert "Issue #12" in body and "page#anchor" in body
    nodes, edges = build_graph_data()
    assert not [n for n in nodes if n["label"] == "Secret"]
    assert not [e for e in edges if e["from"] == rel]
    assert not _TAG.findall(body) and not autolink._INLINE_TAG_RE.findall(body)
    rendered = tc.post("/note/render", json={"text": body}).json()["html"]
    assert 'class="tag"' not in rendered and "note-link" not in rendered


def test_every_link_form_the_graph_reads_is_inert_in_a_body(tmp_vault):
    from silica.kernel.link.ast import extract_links

    body = mail.render(_thread(msg(mid="<a@x.example>", plain=(
        "Wiki [[Secret]] [[Path/To|alias]] [[[Triple]]] and an embed ![[Secret]]\n"
        "Local [one](Secret.md) [two](<Other Note>) [three][r] [four]\n\n"
        "[r]: Hidden.md\n[four]: Elsewhere\n> [q]: Quoted.md\n\n"
        r"Escaped \[\[Esc\]\] and &#91;&#91;Ent&#93;&#93; and &lbrack;&lbrack;Ent2&rbrack;&rbrack;"
        "\nADR-0001 decides it. Web [site](https://site.example/x) stays a link."))))
    assert extract_links(body) == [], extract_links(body)
    assert "Secret" in body and "site.example/x" in body


def test_no_header_a_stranger_wrote_puts_a_link_in_the_note(tmp_vault, tmp_path):
    """The stamp is the note too, and Silica's graph reads its frontmatter:
    a Message-ID, a thread id, a sender, a subject or an attachment name that
    spells a link made an edge from the note."""
    from silica.kernel.link.ast import extract_links
    from silica.sources.registry import stage

    path = write_raw(tmp_path / "hdr.eml", (
        b"X-GM-THRID: [[ThreadLink]]\n"
        b"From: \"[[FromLink]] ADR-0002\" <evil@evil.example>\nTo: " + ME_FULL.encode() + b"\n"
        b"Subject: ADR-0003 [[SubjectLink]]\nDate: Wed, 09 Sep 2026 09:00:00 +0000\n"
        b"Message-ID: <[[IdLink]]ADR-0004@evil.example>\nMIME-Version: 1.0\n"
        b"Content-Type: multipart/mixed; boundary=b\n\n--b\nContent-Type: text/plain\n\nBody.\n"
        b"--b\nContent-Type: application/pdf\nContent-Disposition: attachment; "
        b"filename=\"[[AttachmentLink]] ADR-0005.pdf\"\n\n%PDF\n--b--\n"))
    mail.remember_addresses([ME])
    [thread] = mail.threads_in(path)
    out = stage(mail.MAIL, f"{path}#{thread.key}")
    assert out["status"] == "ok", out
    text = (tmp_path / "vault" / out["note_path"]).read_text(encoding="utf-8")
    assert extract_links(text) == [], extract_links(text)
    assert mail.plan(path, all_=True).present == 1          # and dedupe still finds it


def test_a_link_the_rules_miss_is_caught_by_the_graphs_own_reader():
    """CommonMark lets a destination start on the next line, which the
    per-line rules do not match: the backstop reads the body as the graph
    does and takes every bracket out."""
    from silica.kernel.link.ast import extract_links

    body = mail.render(_thread(msg(mid="<a@x.example>", plain="See [x](\nSecret.md) here.")))
    assert extract_links(body) == [] and "Secret.md" in body


@pytest.mark.parametrize("element", [
    '<div style="height:0;overflow:hidden">HIDDEN-1</div>',
    '<div style="height: 0px; overflow-y: hidden">HIDDEN-1</div>',
    '<div style="width:0;overflow:clip">HIDDEN-1</div>',
    '<span style="mso-hide:all">HIDDEN-1</span>',
    '<span style="MSO-HIDE: ALL">HIDDEN-1</span>',
    '<span style="font-size:1px;color:#fefefe">HIDDEN-1</span>',
    '<span style="font-size:0.5px">HIDDEN-1</span>',
    '<span style="font-size:0.75pt">HIDDEN-1</span>',
    '<span style="font-size:0.05em">HIDDEN-1</span>',
    '<span style="font-size:5%">HIDDEN-1</span>',
    '<span style="font:1px/1 Arial">HIDDEN-1</span>',
    '<span style="font:0/0 a">HIDDEN-1</span>',
    '<span style="font: bold 1px Arial">HIDDEN-1</span>',
])
def test_three_more_hiding_styles_never_reach_the_note(element):
    body = mail.render(_thread(msg(mid="<a@x.example>",
                                   html=f"<p>VISIBLE-1</p>{element}<p>VISIBLE-2</p>")))
    assert "VISIBLE-1" in body and "VISIBLE-2" in body and "HIDDEN" not in body


@pytest.mark.parametrize("element", [
    '<div style="height:0">SHOWN-1</div>',                 # overflows its box, visibly
    '<span style="font-size:2px">SHOWN-1</span>',
    '<span style="font: 700 12px Arial">SHOWN-1</span>',   # 700 is the weight, not the size
])
def test_a_style_that_only_looks_like_hiding_keeps_its_text(element):
    assert "SHOWN-1" in mail.render(_thread(msg(mid="<a@x.example>", html=element)))


def test_the_poster_session_thread_leaks_only_the_ceiling_point_7_names():
    body = mail.render(_thread(msg(mid="<h1@x.example>", html=poster_session_html())))
    assert "VISIBLE-ONE" in body and "VISIBLE-TWO" in body
    assert set(re.findall(r"\w*LEAK[\w-]*", body)) == {"LEAK-CLASSHIDDEN", "LEAK-WHITEONWHITE"}
