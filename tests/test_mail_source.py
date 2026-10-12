"""ADR-0090 Part A: mail enters as one note per thread, from a file the user
hands over. The adapter's reading, the thread, the note and its path."""
from __future__ import annotations

import pytest

from silica.sources import mail
from tests.mail_fixtures import ME, ME_FULL, PROF, msg


# --- point 2: the thread key ---------------------------------------------------

def test_the_provider_thread_id_wins_over_every_header():
    m = msg(thrid="1790000000000000001", mid="<b@x.example>", irt="<a@x.example>",
            refs=("<a@x.example>",))
    assert mail.thread_key(m) == "1790000000000000001"


def test_the_references_root_is_the_key_of_a_reply():
    m = msg(mid="<c@x.example>", irt="<b@x.example>", refs=("<a@x.example>", "<b@x.example>"))
    assert mail.thread_key(m) == "<a@x.example>"


def test_in_reply_to_stands_in_when_references_is_missing():
    assert mail.thread_key(msg(mid="<c@x.example>", irt="<b@x.example>")) == "<b@x.example>"


def test_a_root_message_keys_its_thread_by_its_own_id():
    assert mail.thread_key(msg(mid="<a@x.example>")) == "<a@x.example>"


def test_a_message_with_no_id_gets_a_key_a_reimport_finds_again():
    one = mail.thread_key(msg(frm=ME_FULL, subject="Notes", date="2026-09-02 08:00"))
    again = mail.thread_key(msg(frm=ME_FULL, subject="Notes", date="2026-09-02 08:00"))
    other = mail.thread_key(msg(frm=ME_FULL, subject="Notes", date="2026-09-02 08:01"))
    assert one == again and one != other


# --- point 9: the readers, and point 1: one thread per unit --------------------

def _two_threads(tmp_path):
    from tests.mail_fixtures import write_mbox

    return write_mbox(tmp_path / "box.mbox", [
        msg(frm=ME_FULL, to=PROF, subject="Lab slot", mid="<l1@x.example>", date="2026-09-03 09:00",
            plain="Can I book Thursday?"),
        msg(subject="Re: Thesis draft", mid="<t2@x.example>", irt="<t1@x.example>",
            refs=("<t1@x.example>",), date="2026-09-02 12:00", plain="Chapter two is fine."),
        msg(frm=ME_FULL, to=PROF, subject="Thesis draft", mid="<t1@x.example>",
            date="2026-09-01 10:00", plain="Here is the draft."),
    ])


def test_an_mbox_groups_its_messages_into_threads_in_date_order(tmp_path):
    threads = mail.threads_of(mail.parse(_two_threads(tmp_path)))
    assert [t.key for t in threads] == ["<t1@x.example>", "<l1@x.example>"]
    first = threads[0]
    assert [m.msg_id for m in first.mails] == ["<t1@x.example>", "<t2@x.example>"]
    assert first.mails[0].sender == "Ada Student" and first.mails[0].address == "ada.student@uni.example"
    assert first.mails[1].subject == "Re: Thesis draft"
    assert first.mails[0].date.isoformat().startswith("2026-09-01T10:00")


def test_an_eml_is_one_message(tmp_path):
    from tests.mail_fixtures import write_eml

    path = write_eml(tmp_path / "one.eml", msg(mid="<e1@x.example>", plain="Hello."))
    [thread] = mail.threads_of(mail.parse(path))
    assert thread.key == "<e1@x.example>" and len(thread.mails) == 1


def test_reading_an_mbox_leaves_the_file_as_it_was(tmp_path):
    path = _two_threads(tmp_path)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    mail.parse(path)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert not list(tmp_path.glob("*.lock"))


# --- point 3: the path the thread key makes unique ------------------------------

def test_the_note_path_is_date_subject_and_six_hex_of_the_key(tmp_path, monkeypatch):
    import hashlib

    from silica.config import CONFIG

    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    [thread, _] = mail.threads_of(mail.parse(_two_threads(tmp_path)))
    key6 = hashlib.sha1(b"<t1@x.example>").hexdigest()[:6]
    assert mail.note_path(thread) == f"sources/mail/2026-09-01 Thesis draft {key6}.md"


def test_two_threads_with_one_date_and_one_subject_get_two_paths():
    a = mail.Thread("<a@x.example>", (mail._mail(msg(subject="Re: meeting", mid="<a@x.example>")),))
    b = mail.Thread("<b@x.example>", (mail._mail(msg(subject="Re: meeting", mid="<b@x.example>")),))
    assert mail.note_path(a) != mail.note_path(b)


def test_a_subject_loses_what_a_filename_or_a_wikilink_cannot_hold():
    t = mail.Thread("<a@x.example>", (mail._mail(msg(
        subject='  [Ticket #12] Re: a/b\\c *? "x" <y> |z ^w\t', mid="<a@x.example>")),))
    name = mail.note_path(t).split("/")[-1]
    assert name.startswith("2026-09-01 Ticket 12 Re abc x y z w ")
    assert not set('/\\:*?"<>|#^[]') & set(name)
    assert mail.note_path(mail.Thread("<b@x.example>", (mail._mail(
        msg(subject="", mid="<b@x.example>")),))).split("/")[-1].startswith("2026-09-01 no subject ")


# --- point 7: what reaches the note ---------------------------------------------

def _thread(*messages):
    mails = [mail._mail(m) for m in messages]
    [thread] = mail.threads_of(mails)
    return thread


def test_the_plain_part_is_preferred_over_the_html_one():
    body = mail.render(_thread(msg(mid="<a@x.example>", plain="PLAIN-OK words",
                                   html="<p>HTML-ONLY words</p>")))
    assert "PLAIN-OK" in body and "HTML-ONLY" not in body


def test_each_message_sits_under_its_sender_and_date_and_no_other_header_reaches_the_note():
    m = msg(mid="<a@x.example>", subject="Thesis draft", plain="See you Monday.",
            to="Someone Else <other@uni.example>")
    m["X-Custom"] = "HEADER-SECRET"
    body = mail.render(_thread(m))
    assert body.startswith("# Thesis draft\n")
    assert "## Dr Grey (grey@uni.example), 2026-09-01 10:00" in body
    assert "See you Monday." in body
    assert "HEADER-SECRET" not in body and "other@uni.example" not in body


@pytest.mark.parametrize("hide", [
    'style="display:none"', 'style="DISPLAY: none !important"', 'style="visibility:hidden"',
    'style="font-size:0"', 'style="font-size: 0px; color: red"', 'style="opacity:0"',
    'style="opacity: 0.0"', 'style="max-height:0; overflow:hidden"', "hidden",
])
def test_html_a_client_hides_never_reaches_the_note(hide):
    html = (f"<html><head><title>TITLE-SECRET</title></head><body><p>VISIBLE-1</p>"
            f"<div {hide}>HIDDEN-1 <b>nested HIDDEN-2</b><div>HIDDEN-3</div></div>"
            "<p>VISIBLE-2</p></body></html>")
    body = mail.render(_thread(msg(mid="<a@x.example>", html=html)))
    assert "VISIBLE-1" in body and "VISIBLE-2" in body
    assert "HIDDEN" not in body and "TITLE-SECRET" not in body


def test_a_style_that_hides_nothing_keeps_its_text():
    html = '<p style="font-size:10px; opacity:0.5; max-height:200px">SHOWN</p>'
    assert "SHOWN" in mail.render(_thread(msg(mid="<a@x.example>", html=html)))


def test_images_are_dropped_whole_alt_and_url_included():
    html = ('<p>VISIBLE</p><img src="https://track.example/pixel.gif?u=1" alt="ALT-SECRET" '
            'width="1" height="1"><img src="cid:logo" alt="LOGO-ALT"/>'
            '<a href="https://track.example/click">a link</a>')
    body = mail.render(_thread(msg(mid="<a@x.example>", html=html)))
    assert "VISIBLE" in body and "a link" in body
    assert "ALT" not in body and "track.example" not in body and "cid:" not in body


def _reply_pair(reply_plain=None, reply_html=None, irt="<a@x.example>"):
    root = msg(frm=ME_FULL, to=PROF, mid="<a@x.example>", plain="ORIGINAL question about chapter two.")
    reply = msg(mid="<b@x.example>", irt=irt, refs=("<a@x.example>",), date="2026-09-02 09:00",
                subject="Re: Thesis draft", plain=reply_plain, html=reply_html)
    return root, reply


def test_a_quoted_copy_of_a_message_in_the_note_is_cut():
    root, reply = _reply_pair(reply_plain=(
        "REPLY fine by me.\n\nOn Mon, Ada Student wrote:\n> ORIGINAL question about chapter two.\n>> older"))
    body = mail.render(_thread(root, reply))
    assert "REPLY fine by me." in body
    assert body.count("ORIGINAL question") == 1 and "older" not in body


def test_a_quote_of_a_message_missing_from_the_note_is_kept():
    reply = msg(mid="<b@x.example>", irt="<gone@x.example>", refs=("<a@x.example>",),
                plain="REPLY\n> QUOTED-ONLY-HERE")
    assert "QUOTED-ONLY-HERE" in mail.render(_thread(reply))


def test_gmail_and_outlook_quote_blocks_are_cut_in_html_and_plain():
    root, gmail = _reply_pair(reply_html=(
        '<div>REPLY-G</div><div class="gmail_quote"><div class="gmail_attr">On Mon wrote:</div>'
        '<blockquote class="gmail_quote">ORIGINAL question about chapter two.</blockquote></div>'))
    body = mail.render(_thread(root, gmail))
    assert "REPLY-G" in body and body.count("ORIGINAL question") == 1 and "On Mon wrote" not in body

    root, outlook_html = _reply_pair(reply_html=(
        '<div>REPLY-O</div><div id="appendonsend"></div><hr><div id="divRplyFwdMsg">'
        "<b>From:</b> Ada</div><div>ORIGINAL question about chapter two.</div>"))
    body = mail.render(_thread(root, outlook_html))
    assert "REPLY-O" in body and body.count("ORIGINAL question") == 1

    root, outlook_plain = _reply_pair(reply_plain=(
        "REPLY-P\n\n________________________________\nFrom: Ada Student\nSent: Monday\n"
        "Subject: Thesis draft\n\nORIGINAL question about chapter two."))
    body = mail.render(_thread(root, outlook_plain))
    assert "REPLY-P" in body and body.count("ORIGINAL question") == 1 and "Sent: Monday" not in body


def test_everything_after_the_rfc3676_separator_is_dropped():
    body = mail.render(_thread(msg(mid="<a@x.example>", plain=(
        "BODY-KEPT\n-- \nDr Grey\nSIG-PHONE +00 000"))))
    assert "BODY-KEPT" in body and "SIG-PHONE" not in body
    html = mail.render(_thread(msg(mid="<b@x.example>", html=(
        "<p>BODY-KEPT</p><div>-- </div><div>SIG-HTML</div>"))))
    assert "BODY-KEPT" in html and "SIG-HTML" not in html


def test_a_dash_line_inside_the_text_is_not_a_signature():
    body = mail.render(_thread(msg(mid="<a@x.example>", plain="a --\n--x\nKEPT")))
    assert "KEPT" in body


def test_a_long_message_and_a_long_note_are_cut_with_the_visible_marker():
    long = mail.render(_thread(msg(mid="<a@x.example>", plain="word " * 10_000)))
    assert f"[truncated at {mail.MESSAGE_CAP} characters]" in long
    replies = [msg(mid="<a@x.example>", plain="x " * 9_000)] + [
        msg(mid=f"<r{i}@x.example>", irt="<a@x.example>", refs=("<a@x.example>",),
            date=f"2026-09-{i + 2:02d} 10:00", plain="y " * 9_000) for i in range(12)]
    note = mail.render(_thread(*replies))
    assert note.rstrip().endswith(f"[truncated at {mail.NOTE_CAP} characters]")
    assert len(note) < mail.NOTE_CAP + 100


def test_attachments_are_listed_by_name_type_and_size_never_read():
    pdf = b"%PDF-1.4 ATTACHED-CONTENT " + b"0" * 3000
    body = mail.render(_thread(msg(mid="<a@x.example>", plain="See attached.",
                                   attachments=(("chapter 2.pdf", "application/pdf", pdf),))))
    assert "Attachments: chapter 2.pdf (application/pdf, 3 KB)" in body
    assert "ATTACHED-CONTENT" not in body


# --- point 1 and point 3: the adapter -------------------------------------------

def test_the_adapter_claims_eml_and_mbox_files_and_their_thread_targets():
    assert mail.MAIL.name == "mail"
    for target in ("box.mbox", "Takeout/All mail.MBOX", "one.eml", "box.mbox#<a@x.example>",
                   "box.mbox#1790000000000000001"):
        assert mail.MAIL.matches(target), target
    for target in ("note.md", "box.mbox.md", "paper.pdf", "#<a@x.example>"):
        assert not mail.MAIL.matches(target), target


def test_read_refuses_a_file_that_names_more_than_one_thread(tmp_path):
    path = _two_threads(tmp_path)
    with pytest.raises(ValueError, match="2 threads"):
        mail.MAIL.read(str(path))
    with pytest.raises(ValueError, match="no thread"):
        mail.MAIL.read(f"{path}#<nope@x.example>")


def test_a_thread_target_becomes_a_terminal_note_stamped_as_mail(tmp_vault, tmp_path):
    from silica.kernel.write import frontmatter

    path = _two_threads(tmp_path)
    stub = mail.MAIL.to_stub(mail.MAIL.read(f"{path}#<t1@x.example>"))
    assert stub.lane == "terminal"
    assert stub.note_path.startswith("sources/mail/2026-09-01 Thesis draft ")
    data, _, body = frontmatter.split(stub.body)
    assert data == {
        "source": "mail", "form": "clip", "mail_thread": "<t1@x.example>",
        "mail_ids": ["<t1@x.example>", "<t2@x.example>"],
        "mail_from": "Ada Student <ada.student@uni.example>", "mail_date": "2026-09-01",
        "mail_origin": "box.mbox", "source_id": stub.note_path.rsplit("/", 1)[-1],
    }
    assert body.startswith("# Thesis draft\n") and "Chapter two is fine." in body


# --- point 4: only threads the user took part in, at most 200 per run ------------

def _box(tmp_path, messages, name="box.mbox"):
    from tests.mail_fixtures import write_mbox

    return write_mbox(tmp_path / name, messages)


def _mixed(tmp_path, takeout=False):
    """Two threads Ada wrote in, one newsletter, one stranger spoofing her From."""
    lab = (lambda x: x) if takeout else (lambda x: None)
    thr = (lambda x: x) if takeout else (lambda x: None)
    return _box(tmp_path, [
        msg(frm=ME_FULL, to=PROF, mid="<a1@x.example>", plain="Mine.", labels=lab("Sent"),
            thrid=thr("11"), delivered_to=None),
        msg(mid="<a2@x.example>", irt="<a1@x.example>", refs=("<a1@x.example>",), plain="Reply.",
            labels=lab("Inbox,Supervisor"), thrid=thr("11"), delivered_to=ME),
        msg(frm=PROF, mid="<b1@x.example>", plain="Question.", date="2026-09-03 10:00",
            labels=lab("Inbox"), thrid=thr("22"), delivered_to=ME),
        msg(frm=ME_FULL, to=PROF, mid="<b2@x.example>", irt="<b1@x.example>",
            refs=("<b1@x.example>",), plain="Answer.", date="2026-09-03 11:00",
            labels=lab("Sent,Supervisor"), thrid=thr("22"), delivered_to=None),
        msg(frm="Weekly <news@list.example>", mid="<n1@x.example>", plain="Offers.",
            date="2026-09-04 10:00", labels=lab("Inbox,Category Promotions"), thrid=thr("33"),
            delivered_to=ME),
        msg(frm=ME_FULL, to=ME_FULL, mid="<s1@x.example>", plain="Spoofed.", date="2026-09-05 10:00",
            labels=lab("Inbox"), thrid=thr("44"), delivered_to=ME),
    ])


def test_only_threads_a_message_of_the_user_is_in_are_kept_by_default(tmp_vault, tmp_path):
    p = mail.plan(_mixed(tmp_path), me=(ME,))
    assert not p.takeout and p.in_file == 4
    assert [t.key for t in p.write] == ["<a1@x.example>", "<b1@x.example>", "<s1@x.example>"]
    assert p.added_by_all == 1


def test_all_drops_the_filter(tmp_vault, tmp_path):
    p = mail.plan(_mixed(tmp_path), me=(ME,), all_=True)
    assert len(p.write) == 4


def test_in_a_takeout_file_the_sent_label_decides_and_from_is_ignored(tmp_vault, tmp_path):
    p = mail.plan(_mixed(tmp_path, takeout=True), me=())
    assert p.takeout
    assert [t.key for t in p.write] == ["11", "22"]


def test_a_takeout_label_keeps_the_threads_one_of_whose_messages_carries_it(tmp_vault, tmp_path):
    p = mail.plan(_mixed(tmp_path, takeout=True), me=(), label="Supervisor")
    assert p.in_file == 2 and [t.key for t in p.write] == ["11", "22"]


def test_one_message_carrying_labels_does_not_make_a_file_takeout(tmp_vault, tmp_path):
    path = _box(tmp_path, [
        msg(frm=PROF, mid="<x1@x.example>", plain="Forged.", labels="Sent"),
        msg(frm=ME_FULL, mid="<x2@x.example>", plain="Mine.", date="2026-09-02 10:00"),
    ])
    p = mail.plan(path, me=(ME,))
    assert not p.takeout and [t.key for t in p.write] == ["<x2@x.example>"]


def test_the_address_is_guessed_from_delivered_to_then_to(tmp_vault, tmp_path):
    p = mail.plan(_mixed(tmp_path))
    assert p.guessed and p.me == (ME,)
    no_delivered = _box(tmp_path, [
        msg(frm=PROF, to=ME_FULL, mid="<y1@x.example>", plain="a"),
        msg(frm=PROF, to="Other <o@uni.example>", mid="<y2@x.example>", plain="b"),
        msg(frm=PROF, to=ME_FULL, mid="<y3@x.example>", plain="c"),
    ], name="other.mbox")
    assert mail.plan(no_delivered).me == (ME,)


def test_the_addresses_the_user_gave_are_kept_outside_the_vault(tmp_vault, tmp_path):
    from silica.config import CONFIG
    from silica.kernel.recall.paths import index_dir_for

    mail.remember_addresses(["Ada.Student@uni.example", " second@uni.example "])
    p = mail.plan(_mixed(tmp_path))
    assert not p.guessed and p.me == (ME, "second@uni.example")
    assert (index_dir_for(CONFIG.vault_path) / "mail_import.json").exists()
    assert not list((tmp_path / "vault").rglob("mail_import.json"))


def test_a_run_writes_at_most_limit_threads_oldest_first(tmp_vault, tmp_path):
    msgs = [msg(frm=ME_FULL, mid=f"<m{i}@x.example>", subject=f"Note {i}",
                date=f"2026-09-{i + 1:02d} 10:00", plain="x") for i in range(5)]
    p = mail.plan(_box(tmp_path, list(reversed(msgs))), me=(ME,), limit=3)
    assert [t.key for t in p.write] == ["<m0@x.example>", "<m1@x.example>", "<m2@x.example>"]
    assert p.over_limit == 2
    assert mail.MAX_THREADS == 200


# --- point 6: dedupe by Message-ID ------------------------------------------------

def test_a_thread_whose_messages_are_all_in_its_note_is_skipped_one_that_grew_is_restaged(
        tmp_vault, tmp_path):
    from silica.sources.registry import stage

    first = _mixed(tmp_path)
    for t in mail.plan(first, me=(ME,)).write:
        assert stage(mail.MAIL, f"{first}#{t.key}")["status"] == "ok"
    again = mail.plan(first, me=(ME,))
    assert again.write == [] and again.present == 3

    grown = _box(tmp_path, [
        msg(frm=ME_FULL, to=PROF, mid="<a1@x.example>", plain="Mine."),
        msg(mid="<a2@x.example>", irt="<a1@x.example>", refs=("<a1@x.example>",), plain="Reply."),
        msg(frm=ME_FULL, to=PROF, mid="<a3@x.example>", irt="<a2@x.example>",
            refs=("<a1@x.example>", "<a2@x.example>"), date="2026-09-06 10:00", plain="NEW-ONE"),
    ], name="later.mbox")
    p = mail.plan(grown, me=(ME,))
    assert [t.key for t in p.write] == ["<a1@x.example>"] and p.grown == 1


# --- point 8: every path the adapter writes is recorded outside the vault ---------

def test_every_written_path_is_recorded_with_its_pdf_attachments(tmp_vault, tmp_path):
    from silica.sources.registry import stage

    path = _box(tmp_path, [msg(frm=ME_FULL, mid="<p1@x.example>", plain="Draft attached.",
                               attachments=(("draft.pdf", "application/pdf", b"%PDF-1.4"),
                                            ("photo.jpg", "image/jpeg", b"\xff\xd8")))])
    out = stage(mail.MAIL, f"{path}#<p1@x.example>")
    assert mail.recorded_paths() == {out["note_path"]}
    assert mail.load_state()["threads"] == [{"path": out["note_path"], "pdfs": ["draft.pdf"]}]
    stage(mail.MAIL, f"{path}#<p1@x.example>")  # a restage records once
    assert len(mail.load_state()["threads"]) == 1


# --- "GUI and TUI": /nucleate thread.eml takes the same defaults -------------------

def test_a_bare_eml_is_written_only_under_the_import_defaults(tmp_vault, tmp_path):
    from silica.sources.registry import stage
    from tests.mail_fixtures import write_eml

    news = write_eml(tmp_path / "news.eml", msg(frm="Weekly <news@list.example>",
                                                 mid="<n@x.example>", plain="Offers."))
    mine = write_eml(tmp_path / "mine.eml", msg(frm=ME_FULL, to=PROF, mid="<m@x.example>",
                                                 plain="My question."))
    out = stage(mail.MAIL, str(mine))
    assert out["status"] == "error" and "/mail import" in out["message"]  # no address yet

    mail.remember_addresses([ME])
    out = stage(mail.MAIL, str(news))
    assert out["status"] == "error" and "took no part" in out["message"]
    assert stage(mail.MAIL, str(mine))["status"] == "ok"
    out = stage(mail.MAIL, str(mine))
    assert out["status"] == "error" and "already in the vault" in out["message"]


def test_nucleate_reaches_the_adapter_in_a_vault_with_default_sources(tmp_vault):
    from silica.config import CONFIG
    from silica.kernel.vault_manifest import load_manifest
    from silica.sources.registry import ALL_ADAPTERS, adapter_for

    assert ALL_ADAPTERS[-1] is mail.MAIL
    enabled = load_manifest(CONFIG.vault_path).sources
    assert "mail" in enabled
    assert adapter_for("thread.eml", enabled) is mail.MAIL
    assert adapter_for("All mail.mbox", enabled) is mail.MAIL
    assert adapter_for("note.md", enabled).name == "prose"
