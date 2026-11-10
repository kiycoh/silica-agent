# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A Publish from a linked computer (ADR-0157 points 10 and 11), both of its ends: `send`, on the
computer, packs the notes its check before Publish lets out, their images and the author's answers,
and posts them to its account; `receive`, on the hosted service, makes them the folder of its vault
they name. One module because the package is one format, `wiki.json`, `notes/` and `assets/`, which
the two ends must read alike.

In sources and not beside the link (kernel.account_link) or the rows (kernel.publish): both ends
read a wiki file's cap and its zip readers, which are saved_page's, while publish, which asks the
link whether this computer may publish at all, sits under every one of them."""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import unquote

from silica.kernel import account_link, publish, rights
from silica.kernel.base.syntax import EMBED_RE
from silica.kernel.image_meta import strip_location
from silica.sources import saved_page as sp

logger = logging.getLogger(__name__)

# The images the receiver takes (saved_page._ASSET): no svg, which can carry a script, no bmp.
_SENT_IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


# --- the computer's end ---------------------------------------------------------------------------

def _answer(files: list[str], said: dict) -> dict:
    """The author's answer for a note's sources, as the receiver keys it, {} for a note made from
    none. An open answer first, whose license and credit the page must print.
    shortcut: a note made from two files sends one answer, since the receiver keeps one a note
    (Task 10), so the second file's license and credit never reach the review. 325 of the 1,052
    notes made from a file in alex_second_brain came from two or more (2026-11-08); a list per note
    on the receiver first, then here, by 2027-02-01 or at the first wiki the review finds short."""
    rows: list[dict] = [r if isinstance(r := said.get(f), dict) else {} for f in sorted(files)]
    a = next((r for r in rows if r.get("answer") == "open"), None) \
        or next((r for r in rows if r.get("answer") in rights.ANSWERS), None)
    if a is None:
        return {}
    lic = a.get("license") if a.get("license") in rights.LICENSES else ""
    credit = c.strip()[:rights._CREDIT_MAX] if isinstance(c := a.get("credit"), str) else ""
    return {k: v for k, v in (("answer", a["answer"]), ("license", lic), ("credit", credit)) if v}


def _pointed(text: str, names: dict[str, str], credits: dict[str, str], folder: str) -> str:
    """The note with each image the check let out pointing at its copy under `<folder>/assets/`,
    as the account's vault will hold it, with the credit its answer asks for; every other embed as
    written. The three shapes rights.place_images reads, in its order; its shortcut on code fences
    holds here too."""
    def to(target: str, alt: str) -> str | None:
        key = rights._name_key(target)
        if key not in names:
            return None
        said = credits.get(key, "")
        return f"![[{folder}/assets/{names[key]}{'|' + alt if alt else ''}]]" + (f"\n\n*{said}*" if said else "")

    def wiki(m: re.Match) -> str:
        target, bar, alt = m.group(1).partition("|")
        target = target.split("#", 1)[0].strip()
        return (to(target, alt if bar else "") if rights._is_image(target) else None) or m.group(0)

    def markdown(m: re.Match) -> str:
        alt, target = m.group(1), m.group(2)
        if rights._REMOTE.match(target) or target.startswith("data:"):
            return m.group(0)
        return to(unquote(target), alt) or m.group(0)

    def tag(m: re.Match) -> str:
        target = m.group(1)
        if rights._REMOTE.match(target) or target.startswith("data:"):
            return m.group(0)
        return to(unquote(target), "") or m.group(0)

    return rights._HTML_IMAGE.sub(tag, rights._MD_IMAGE.sub(markdown, EMBED_RE.sub(wiki, text)))


def _too_big() -> ValueError:
    return ValueError(f"This wiki is past the {sp.WIKI_FILE_MAX / 1e6:g} MB a Publish may send.")


def _pack(conf: dict, vault: Path, out) -> None:
    """The zip a Publish sends, written into `out` (the receiver is publish.receive): `wiki.json`,
    then the notes the check before Publish lets out as written in the vault, under
    `notes/<path in the folder>`, then `assets/<name>` for each image of theirs it lets out. Never
    a page: the account builds those (ADR-0157 point 10). A remote image is sent nothing; the
    account shows it as a link (Task 10's ruling). Stops past the 100 MB cap before the image or
    note that passes it is read."""
    cap = sp.WIKI_FILE_MAX
    folder = conf["folder"]
    name = _hosted_folder(folder)
    # what the check before Publish decides, read as the build reads it (public_site._write_wiki)
    wiki = rights.read_wiki(conf["slug"], vault)
    said = rights.answers(vault)
    plan = rights.decide(wiki, said)
    held = 0
    names: dict[str, str] = {}
    answers: dict[str, dict] = {}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for key in sorted(plan["local"]):
            src = vault / wiki["local"][key]["src"]
            if src.suffix.lower() not in _SENT_IMAGES:
                continue  # its embed goes as written, and the account's page leaves it out
            try:
                size = src.stat().st_size
                if held + size > cap:
                    raise _too_big()
                data = strip_location(src.read_bytes())
            except OSError as exc:
                # gone or unreadable since the check read it: its embed goes as written, and the
                # account's page says the image is not published, as a build does
                logger.warning("account link: %s not sent (%s)", src.name, type(exc).__name__)
                continue
            held += size
            # numbered and slugged as the build names a copy: a pasted name has spaces, which the
            # receiver refuses, and two folders can hold one name
            names[key] = f"{len(names) + 1}-{publish.slugify(src.stem)[:100] or 'image'}{src.suffix.lower()}"
            z.writestr("assets/" + names[key], data)
        for p in sorted(plan["notes"]):
            try:
                text = (vault / p).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                # one note that cannot be read stays out, as in a build; the receiver would refuse
                # the whole wiki for a note that is not text
                logger.warning("account link: %s not sent (%s)", p, type(exc).__name__)
                continue
            if "\x00" in text:
                logger.warning("account link: %s not sent (it holds a NUL)", p)
                continue
            held += len(text.encode("utf-8"))
            if held > cap:
                raise _too_big()
            rel = p[len(folder) + 1:]
            z.writestr("notes/" + rel, _pointed(text, names, plan["credits"], name))
            if a := _answer(wiki["notes"][p]["files"], said):
                answers[rel] = a
        head = {"link": "", "folder": name, "slug": conf["slug"], "title": conf["title"], "line": conf["line"],
                "license": conf["license"], "visibility": conf["visibility"], "answers": answers,
                "readers": conf["readers"], "listed": conf["listed"]}
        # `link` is the format's and stays empty: the account learns which link sent a wiki from
        # the gateway, never from the package (Task 10), and this computer is never told its id
        z.writestr("wiki.json", json.dumps(head, ensure_ascii=False))


def _hosted_folder(folder: str) -> str:
    """The folder a wiki lands in on the account: the receiver takes a name at the top of the
    account's vault, never a path, so a nested wiki goes under its last segment."""
    return folder.rsplit("/", 1)[-1]


def _refusal(status: int, detail: str, at: str) -> str:
    """The sentence the view shows for an answer of the account other than 200 and 401."""
    detail = " ".join(detail.split())[:300].rstrip(".")
    if status in (502, 504, 524):
        # The edge between: it cut one 53 MB POST at 125 s on a slow uplink (measured 2026-11-02), so
        # the account may still have taken the wiki, and a second Publish would send it all again
        h = account_link._named(at)
        return (f"The connection to {h} was cut before your account answered; a large wiki may need a "
                f"faster connection. Check Wikis → My wikis on {h} before publishing again.")
    if status == 409:
        # The account's own words name the folder; a folder of the person's own is never replaced
        # (ADR-0157 point 10), so one of the two is renamed
        return f"{detail}." if detail else ("Your account already has a folder of its own with this wiki's "
                                            "name: rename one of them to publish this wiki.")
    if status == 503:
        return "Your account is busy. Try again in a minute."
    if status == 413:
        return str(_too_big())
    if status == 400 and detail:
        return f"Your account refused this wiki: {detail}."
    if status == 500 and detail:
        return f"{detail[0].upper()}{detail[1:]}."
    return f"{account_link._named(at)} answered {status}. Try again in a minute."


def send(slug: str, vault: Path | None = None, visibility: str | None = None) -> dict:
    """Publish one wiki through the linked account: the notes that go out, as written, their images
    and the author's answers (ADR-0157 points 10 and 11); the account publishes them with its
    own renderer, in the visibility the wiki has here, or `visibility`, which the row takes only
    once the account has taken the wiki, so a refusal here or there leaves the row as it was. An
    Off wiki is sent Off, which takes it off its address on the account. Returns {"vault",
    "notes", "url", "published_at", "sent", "off"}, the account's count and address. ValueError
    with a sentence for anything the person must read, the account's refusals included."""
    import httpx

    from silica.kernel.base.home import silica_tmp_dir

    got = account_link._read()
    if got is None:
        raise ValueError(account_link.NOT_LINKED)
    vault = vault or publish.vault_root()
    row = publish.find(slug, vault)
    conf = {**row, "visibility": visibility or row["visibility"]}
    name = _hosted_folder(conf["folder"])
    # The account replaces a folder any linked computer sent (Task 10), so A/Notes and B/Notes
    # would overwrite each other there, each Publish taking the other's place.
    if sum(_hosted_folder(v["folder"]) == name for v in publish.vaults(vault)) > 1:
        raise ValueError(f"Two of your wikis would publish into the folder {name} on your account: rename one.")
    if len(conf["readers"]) > READERS_MAX:
        raise ValueError(f"A wiki published from this computer names {READERS_MAX} readers at most.")
    at = got["host"]
    # On disk under ~/.silica, unnamed, so it goes when closed even after a crash, and 100 MB is
    # never held in memory
    with tempfile.TemporaryFile(dir=silica_tmp_dir()) as f:
        _pack(conf, vault, f)
        # the notes and images were counted unpacked; the zip adds its headers
        if f.tell() > sp.WIKI_FILE_MAX:
            raise _too_big()
        f.seek(0)
        # A wiki of 100 MB, then the account builds its pages before it answers
        r = account_link._post(f"{at}/link/publish", at, content=f, timeout=httpx.Timeout(600, connect=15),
                  headers={"Authorization": f"Bearer {got['token']}", "Content-Type": "application/zip"})
    if r.status_code == 401:
        # The account no longer knows this token: removed in its Settings, or signed out
        # everywhere. Kept, it would read as linked while every Publish fails.
        account_link.unlink()
        logger.info("account link: %s refused the token; this computer is unlinked", at)
        raise ValueError("Your account no longer knows this computer. Link it again to publish.")
    out = account_link._body(r, at)
    if r.status_code != 200:
        logger.info("account link: %s answered %s to a Publish of %s", at, r.status_code, slug)
        detail = out.get("detail")
        raise ValueError(_refusal(r.status_code, detail if isinstance(detail, str) else "", at))
    url = out.get("url") if isinstance(out.get("url"), str) and out["url"].startswith("https://") else ""
    notes = out.get("notes") if type(out.get("notes")) is int else 0
    off = conf["visibility"] == "off"
    if conf["visibility"] != row["visibility"]:
        publish.set_visibility(slug, conf["visibility"], vault)
    items = publish.vaults(vault)
    for v in items:
        if v["slug"] == slug:
            v["sent_listed"] = conf["listed"]
            if not off:
                v["published"], v["sent"] = datetime.date.today().isoformat(), True
    publish._save_vaults(items, vault)
    return {"vault": slug, "notes": notes, "url": url,
            "published_at": out.get("published_at") if isinstance(out.get("published_at"), str) else "",
            "sent": True, "off": off}


# --- the account's end --------------------------------------------------------------------------

class FolderTaken(ValueError):
    """The folder a computer's wiki names holds notes that computer never sent."""


_BAD_HEAD = "that wiki's wiki.json is not one a Silica sends"


def _said(v, n: int) -> str:
    """A string of wiki.json as a row keeps it. A lone surrogate is valid JSON and no UTF-8, and
    vault.yaml is UTF-8, so one would fail the row's write after the checks had passed."""
    if not isinstance(v, str):
        raise ValueError(_BAD_HEAD)
    try:
        v.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(_BAD_HEAD) from None
    return v.strip()[:n]


# Readers a computer names for a Private wiki, as many as an invitation list is worth carrying in
# one wiki.json (the controller's number for Task 11, 2026-11-08)
READERS_MAX = 100


def _head(m, notes: set[str]) -> dict:
    """wiki.json's fields, each of its own type. Its `link` is never read: the gateway names the
    link a wiki came by, and a package is the client's word. Off takes the wiki off its address
    on this account, as Off here does. `readers` may be absent, read as none; `listed` is None when
    absent, and the row keeps the request it has."""
    if not isinstance(m, dict) or m.get("visibility") not in publish.VISIBILITY \
            or not isinstance(m.get("answers"), dict):
        raise ValueError(_BAD_HEAD)
    readers, listed = m.get("readers", []), m.get("listed")
    # a handle each, within the 30 characters update_vault takes, so nothing is refused after the
    # row is written; `listed` carried is a bool, null included in what is refused
    if ("listed" in m and not isinstance(listed, bool)) or not isinstance(readers, list) or len(readers) > READERS_MAX \
            or not all(isinstance(r, str) and sp._NAME.fullmatch(r) and publish.HANDLE_RE.fullmatch(r) for r in readers):
        raise ValueError(_BAD_HEAD)
    head = {k: _said(m.get(k), n) for k, n in (("folder", 4096), ("slug", 60), ("title", 80),
                                                ("line", 160), ("license", 80))}
    if head["license"] not in publish.LICENSES:
        raise ValueError("that wiki's license is not one this account offers")
    answers = {}
    for rel, a in m["answers"].items():
        if not isinstance(a, dict) or a.get("answer") not in rights.ANSWERS:
            raise ValueError(_BAD_HEAD)
        row = {"answer": a["answer"], "license": _said(a.get("license", ""), 80),
               "credit": _said(a.get("credit", ""), rights._CREDIT_MAX)}
        if row["license"] and row["license"] not in rights.LICENSES:
            raise ValueError(_BAD_HEAD)
        # Keyed as the note's entry under notes/ is: a sender keying them any other way would lose
        # every credit in silence, and a credit is what a CC BY source asks for.
        if rel not in notes:
            raise ValueError(f"that wiki answers for {rel[:120]!r}, a note it did not send")
        answers[rel] = {k: v for k, v in row.items() if v}
    return {**head, "visibility": m["visibility"], "answers": answers, "readers": readers, "listed": listed}


def _plain(name: str) -> bool:
    """One segment of a path a computer named: no dot-name, which every walk of the vault skips, no
    control character, and no longer than a file system takes."""
    return bool(name) and not name.startswith(".") and len(name.encode("utf-8")) <= 255 \
        and not any(ord(c) < 32 for c in name)


def _held(root: Path) -> tuple[int, int]:
    """(notes, bytes) under `root`, counted as saved_page.room counts a vault."""
    notes = size = 0
    for p in root.rglob("*"):
        if p.is_file() and not any(s.startswith(".") for s in p.relative_to(root).parts):
            size += p.stat().st_size
            notes += p.suffix == ".md"
    return notes, size


def _receive_row(folder: str, link: str, meta: dict, vault: Path) -> str:
    """The row of a computer's wiki, made or rewritten: what the computer says replaces what was
    edited here, as its notes do (ADR-0157 Open 3). A slug stays once made, since readers hold it."""
    items = publish.vaults(vault)
    v = next((x for x in items if x["folder"] == folder), None)
    if v is None:
        v = publish._clean_vault({"folder": folder, "slug": publish._free_slug(meta["slug"] or folder, items)})
        assert v is not None  # receive refused an empty folder name, the one way it cleans to None
        items.append(v)
    # shortcut: the answers ride in vault.yaml, which every publish.vaults() parses whole, about 100 bytes
    # a note; give them a file beside the row past 1,000 answered notes in one account
    v.update(from_link=link, title=meta["title"] or folder, line=" ".join(meta["line"].split()),
             license=meta["license"], answers=meta["answers"])
    publish._save_vaults(items, vault)
    return v["slug"]


def receive(data: bytes, link: str, vault: Path | None = None) -> dict:
    """A wiki a linked computer sent through the gateway (ADR-0157 point 10), a zip of `wiki.json`,
    `notes/<path>.md` and `assets/<image>`: its notes become the folder of this vault it names,
    replacing that folder whole when its row came from any linked computer, and its row carries
    `link` as the last sender, the author's answers (point 11), its readers, its listing request
    and the visibility. Returns {"slug", "folder", "visibility"}; the caller builds, unless Off.

    ValueError with a sentence for a refused package, FolderTaken for a folder of the person's
    own, and then nothing is written; an OSError is the disk's. The folder is written beside the
    vault and moved in, so a failure never leaves half of one, and the old one goes into the
    journal, so /revert takes the Publish back as it does a save (saved_page.save_vault)."""
    import hashlib
    import io
    import json
    import stat
    import tempfile
    import time
    import zipfile

    from silica.driver import DRIVER, reset_driver
    from silica.kernel.write.ops import InverseOp, InverseOpKind
    from silica.kernel.write.undo_journal import get_undo_journal

    vault = vault or publish.vault_root()
    if len(data) > sp.WIKI_FILE_MAX:
        raise sp._file_too_big()
    # zipfile holds every entry's header before a check can run (saved_page.open_file)
    if data.count(b"PK\x01\x02") > sp._ENTRIES_MAX:
        raise ValueError(f"that wiki holds more than {sp._ENTRIES_MAX:,} entries, which no wiki does")
    z = sp._zip_read("that is not a zip Silica can read", zipfile.ZipFile, io.BytesIO(data))
    with z:
        head, keep = None, {}
        for info in z.infolist():
            name = info.filename
            if name.startswith("/") or "\\" in name or ".." in name.split("/") or sp._DRIVE.match(name):
                raise ValueError(f"that wiki holds {name[:120]!r}, a name that leaves its folder")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(f"that wiki holds {name[:120]!r}, a link, which Silica never unpacks")
            if info.is_dir():
                continue
            if name.lower().endswith((".html", ".htm")):
                raise ValueError(f"that wiki holds {name[:120]!r}, a page: a computer sends notes, "
                                 "and this account writes the pages")
            rel = name.removeprefix("notes/")
            # Refused here and not met as a write's OSError, which the route words as the disk's:
            # 255 is a name's limit on every file system, and 1,024 keeps the vault's path, a
            # folder of 255 and this under Linux's 4,096.
            if len(rel.encode("utf-8")) > 1024 or any(len(s.encode("utf-8")) > 255 for s in rel.split("/")):
                raise ValueError(f"that wiki holds {name[:120]!r}, a name longer than a file system takes")
            if name == "wiki.json":
                head = info
            elif (rel != name and rel.endswith(".md") and all(_plain(p) for p in rel.split("/"))) \
                    or (name.startswith("assets/") and sp._ASSET.fullmatch(name.removeprefix("assets/"))):
                keep[rel] = info  # a note by its path in the folder, an image as assets/<name>
            else:
                raise ValueError(f"that wiki holds {name[:120]!r}, which is neither a note nor an image")
        # `A.md` beside `A.md/B.md`: one name as a file and a folder, which no unpacking can write
        folders = {k[:i] for k in keep for i, c in enumerate(k) if c == "/"}
        if clash := next((k for k in keep if k in folders), None):
            raise ValueError(f"that wiki holds {clash[:120]!r} both as a file and as a folder")
        if head is None:
            raise ValueError("that is not a wiki a Silica sends: it holds no wiki.json")
        if head.file_size > sp._MANIFEST_MAX:
            raise sp._part_too_big("wiki.json", sp._MANIFEST_MAX)
        raw = sp._zip_read(sp._DAMAGED, z.read, head)
        if len(raw) > sp._MANIFEST_MAX:
            raise sp._part_too_big("wiki.json", sp._MANIFEST_MAX)
        try:
            m = json.loads(raw)
        except (ValueError, RecursionError):
            raise ValueError(_BAD_HEAD) from None
        notes = {k for k in keep if k.endswith(".md")}
        meta = _head(m, notes)
        del raw, m
        folder = meta["folder"]
        # at the top of the vault: a name and not a path, never one of Silica's own folders
        if not _plain(folder) or "/" in folder or "\\" in folder or publish.never(folder + "/x", publish.never_dirs()):
            raise ValueError(f"{folder[:80] or 'that folder'} cannot be a wiki's folder here")
        items = publish.vaults(vault)
        row = next((v for v in items if v["folder"] == folder), None)
        dest = vault / folder
        # Any link's mark and not only this one's: a link lasts 90 days and a new one gets a new
        # id, so the computer's own folder would refuse it. Point 10 refuses a folder with no mark.
        ours = row is not None and bool(row["from_link"])
        # A link in the vault's place is the person's too: replacing it would write where it leads.
        if (dest.exists() or dest.is_symlink()) and not (ours and dest.is_dir() and not dest.is_symlink()):
            raise FolderTaken(f"A folder named {folder} already holds notes of yours: "
                              "rename one of them to publish this wiki.")
        if row is None and len(items) >= (limit := publish.max_vaults()):
            raise ValueError(f"you can publish {limit} wikis at most")
        if meta["visibility"] == "private" and publish.private_allowed() != "ok":
            raise ValueError("Private needs a paid account on Silica hosted")
        if not publish.available(vault):
            raise ValueError("this account has no public address yet")
        # Beside the vault, on its disk, so the move in is a rename and no walk of the vault ever
        # sees half a folder. One a receive killed mid-write left is swept here after an hour,
        # far past any unpacking of 100 MB, as saved_page.open_file sweeps its own.
        parent = Path(os.path.abspath(vault)).parent
        for old in parent.glob(".receive-*"):
            if old.name.endswith("-old"):
                continue  # an old folder a failed swap could not put back: maybe the wiki's only copy here
            try:
                if old.stat().st_mtime < time.time() - 3600:
                    shutil.rmtree(old, ignore_errors=True)
            except OSError:
                pass  # gone between the glob and the stat: another receive swept it first
        tmp = Path(tempfile.mkdtemp(prefix=".receive-", dir=parent))
        aside = tmp.with_name(tmp.name + "-old")
        try:
            for rel in keep:
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            sp._unpacked(z, keep, tmp)
            for rel in notes:
                blob = (tmp / rel).read_bytes()
                try:
                    if b"\x00" in blob:
                        raise UnicodeError
                    blob.decode("utf-8")
                except UnicodeError:
                    raise ValueError(f"that wiki's note {rel[:120]!r} is not text") from None
            # the folder it replaces is freed by the same move, so it counts once
            new_n, new_b = _held(tmp)
            old_n, old_b = _held(dest) if ours and dest.is_dir() else (0, 0)
            short = sp.room(new_n - old_n, new_b - old_b, vault)["short"]
            if short:
                raise ValueError(f"this account has no room for that wiki: {short}")
            # The row before the folder: a row whose folder failed to land is taken by the next
            # Publish, while a folder without its row would refuse that Publish as the person's.
            slug = _receive_row(folder, link, meta, vault)
            # The author's readers and listing request, as a hosted author's own: the account's
            # handle leaves the readers, and the host's review still decides the listing. The
            # computer owns the request for its wikis; a package without it leaves the row's.
            publish.update_vault(slug, {"readers": meta["readers"]}
                         | ({} if meta["listed"] is None else {"listed": meta["listed"]}), vault)
            if dest.exists():
                dest.rename(aside)
            try:
                tmp.rename(dest)
            except BaseException:
                if aside.exists():
                    try:
                        aside.rename(dest)
                    except OSError:
                        # left where it is, never swept: it may be the only copy of the wiki here
                        logger.error("receive: the old %s could not be put back; it waits beside the vault as %s",
                                     folder, aside.name)
                raise
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    # The driver's index learns of a folder moved in only at its next look, up to 2 s on, and a
    # write of its own in that window absorbs the change (fs_backend._restamp_dirs): the build
    # below resolves each link through it, and a note it has not seen is written as words.
    reset_driver()
    try:
        # hashed as the driver reads them back, as save_vault does, which is what revert compares
        post = {f"{folder}/{rel}": hashlib.sha256(DRIVER.read_note(f"{folder}/{rel}").content.encode("utf-8"))
                .hexdigest() for rel in notes}
        journal = get_undo_journal()
        run = journal.start_run(source="vault:receive", vault=str(vault))
        # LIFO on revert: each new note goes, then each old one comes back; one at a path both
        # hold is rewritten unless edited since, which the hash says. Notes only, as save_vault:
        # the images of the old folder do not come back.
        for p in sorted(aside.rglob("*.md")) if aside.is_dir() else ():
            rel = p.relative_to(aside).as_posix()
            if p.is_file() and all(not s.startswith(".") for s in rel.split("/")):
                journal.record(run, InverseOp(kind=InverseOpKind.recreate_deleted, path=f"{folder}/{rel}",
                                              prior_content=p.read_text(encoding="utf-8", errors="replace")),
                               post.pop(f"{folder}/{rel}", None))
        for path, h in post.items():
            journal.record(run, InverseOp(kind=InverseOpKind.delete_created, path=path), h)
    except Exception as exc:
        # The folder is in and its row says so: only /revert loses this Publish, as a checkpoint
        # in write_prop. What it replaced was the computer's, which a Publish overwrites by the
        # owner's answer (ADR-0157 Open 3), so failing the Publish now would save nothing.
        logger.warning("receive: %s not journalled (%s); /revert cannot take it back", folder, exc)
    finally:
        shutil.rmtree(aside, ignore_errors=True)
    publish.set_visibility(slug, meta["visibility"], vault)
    return {"slug": slug, "folder": folder, "visibility": meta["visibility"]}
