"""Synthetic mail for the ADR-0090 tests.

Every message here is written by this module: no real mail, no real person,
no entity from any benchmark set. Addresses sit under `.example`, the domain
RFC 2606 reserves for documentation.
"""
from __future__ import annotations

import datetime as dt
import mailbox
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

ME = "ada.student@uni.example"
ME_FULL = f"Ada Student <{ME}>"
PROF = "Dr Grey <grey@uni.example>"


def msg(*, frm: str = PROF, to: str = ME_FULL, subject: str = "Thesis draft",
        date: str = "2026-09-01 10:00", mid: str | None = None, irt: str | None = None,
        refs: tuple[str, ...] = (), plain: str | None = None, html: str | None = None,
        labels: str | None = None, thrid: str | None = None,
        attachments: tuple[tuple[str, str, bytes], ...] = (),
        delivered_to: str | None = None) -> EmailMessage:
    m = EmailMessage()
    # Takeout writes its two headers first, before the message's own.
    if thrid is not None:
        m["X-GM-THRID"] = thrid
    if labels is not None:
        m["X-Gmail-Labels"] = labels
    if delivered_to is not None:
        m["Delivered-To"] = delivered_to
    m["From"] = frm
    m["To"] = to
    m["Subject"] = subject
    when = dt.datetime.strptime(date, "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
    m["Date"] = format_datetime(when)
    if mid is not None:
        m["Message-ID"] = mid
    if irt is not None:
        m["In-Reply-To"] = irt
    if refs:
        m["References"] = " ".join(refs)
    if plain is not None:
        m.set_content(plain)
        if html is not None:
            m.add_alternative(html, subtype="html")
    elif html is not None:
        m.set_content(html, subtype="html")
    else:
        m.set_content("")
    for name, ctype, data in attachments:
        maintype, subtype = ctype.split("/", 1)
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return m


def write_mbox(path: Path, messages: list[EmailMessage]) -> Path:
    box = mailbox.mbox(str(path))
    try:
        for m in messages:
            box.add(m)
        box.flush()
    finally:
        box.close()
    return path


def write_eml(path: Path, m: EmailMessage) -> Path:
    path.write_bytes(bytes(m))
    return path


def write_raw(path: Path, data: bytes) -> Path:
    """Bytes as a stranger's client wrote them: raw 8-bit headers, control
    bytes, headers the modern policy refuses to build."""
    path.write_bytes(data)
    return path


# What a tracking pixel would call home to. `.example` never resolves, and gate 1
# fails any socket an import opens, so a URL here proves only what reached the note.
TRACK = "http://track.example"
STRANGER = "Ivo Stranger <ivo@elsewhere.example>"


def poster_session_html() -> str:
    """The end-to-end tester's "Poster session" HTML part, 2026-10-12: one
    visible line per way a mail client hides text. Every LEAK- string is text
    a client does not show; LEAK-CLASSHIDDEN (a class in a <style> block) and
    LEAK-WHITEONWHITE (a colour equal to the background) are the ceiling
    ADR-0090 point 7 names, the rest must never reach a note."""
    # One element per line, as the tester wrote it: inline elements with no
    # space between them would run their strings together into one word.
    return "\n".join([
        "<html><head><title>HEADTITLE-LEAK</title><style>.x{display:none}</style></head><body>",
        '<div style="display:none;max-height:0;overflow:hidden">LEAK-DISPLAYNONE</div>',
        "<p>VISIBLE-ONE</p>",
        '<span style="font-size:0px">LEAK-FONTSIZE0</span>',
        '<span style="visibility:hidden">LEAK-VISIBILITY</span>',
        '<span style="opacity:0">LEAK-OPACITY</span>',
        '<div style="max-height:0px">LEAK-MAXHEIGHT</div>',
        "<span hidden>LEAK-HIDDENATTR</span>",
        '<span style="DISPLAY : NONE !important">LEAK-DISPLAYCAPS</span>',
        '<span style="color:#ffffff;background:#ffffff">LEAK-WHITEONWHITE</span>',
        '<span class="x">LEAK-CLASSHIDDEN</span>',
        '<span style="display:none" style="color:red">LEAK-DUPATTR</span>',
        '<div style="height:0;overflow:hidden">LEAK-HEIGHT0</div>',
        '<span style="font-size:1px;color:#fefefe">LEAK-TINYFONT</span>',
        '<span style="mso-hide:all">LEAK-MSOHIDE</span>',
        "<p>VISIBLE-TWO</p></body></html>",
    ])


def field_photos() -> list[EmailMessage]:
    """The end-to-end tester's "Field photos" thread, 2026-10-12: a stranger's
    plain part carrying a markdown image and a raw `<img>`, Ada's reply, then an
    HTML-only reply whose visible text spells an image in markdown and in
    escaped HTML. Opening the note in the GUI drawer requested every URL."""
    root = "<k1@elsewhere.example>"
    reply = {"irt": root, "refs": (root,), "subject": "Re: Field photos"}
    return [
        msg(frm=STRANGER, subject="Field photos", mid=root, delivered_to=ME,
            plain=(f"Photos attached.\n\n![pixel]({TRACK}/md-plain.gif)\n\n"
                   f'<img src="{TRACK}/raw-plain.gif">\n')),
        msg(frm=ME_FULL, to=STRANGER, mid="<k2@uni.example>", date="2026-09-01 11:00",
            plain="Thanks.", **reply),
        msg(frm=STRANGER, mid="<k3@elsewhere.example>", date="2026-09-01 12:00", **reply,
            html=(f"<p>Markdown in html text: ![md]({TRACK}/md-in-html.gif)</p>"
                  f'<p>Escaped markup in visible text: &lt;img src="{TRACK}/escaped.gif"&gt;</p>'
                  "<p>FIELD-VISIBLE</p>")),
    ]
