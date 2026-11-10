# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A Silica on a computer linked to a hosted account (ADR-0157 points 4, 9, 10 and 11): the code
the person confirms on the gateway, the token that comes back, and the one thing it does, send a
wiki's notes to the account, which builds the pages and publishes them. Unlinked, a computer only
saves wikis as files.

Named account_link and not link: silica.kernel.link is the wikilink package.

The token publishes in the person's name for 90 days, so it lives in one file, 0600, and goes to
the host that issued it alone: it is in no log line, no exception, no answer to the page and not
in `state()`. Every call to the gateway is the app server's, never the browser's: the gateway
refuses a fetch from a page on localhost (silica-hosted bin/gateway, `same_origin`)."""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

logger = logging.getLogger(__name__)

DEFAULT_HOST = "https://app.silica-agent.dev"
# the link's life, the owner's (ADR-0157 Open 1), as the gateway's LINK_LIFE
LIFE = datetime.timedelta(days=90)
NOT_LINKED = "Link a Silica account to publish."
# The gateway hands out token_urlsafe(32), 43 characters. The bound keeps a hand-written file from
# putting into a header what no proxy carries; the alphabet keeps a newline out of it.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,200}")
_CODE = re.compile(r"[A-Z0-9]{3}-[A-Z0-9]{3}")
# The images the receiver takes (saved_page._ASSET): no svg, which can carry a script, no bmp.
_SENT_IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _origin(raw: str) -> str:
    """`raw` as scheme://host[:port], "" for an address a token must not travel to: https alone,
    since the token rides in a header, but for this machine's own, where a test's gateway listens."""
    try:
        u = urlsplit(raw.strip().rstrip("/"))
        _ = u.port  # raises on a port out of range
    except ValueError:
        return ""
    here = u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")
    if not (u.scheme == "https" or here) or not u.hostname or u.username is not None \
            or u.path or u.query or u.fragment:
        return ""
    return f"{u.scheme}://{u.netloc.lower()}"


def host() -> str:
    """The gateway a new link goes to; "" when SILICA_LINK_HOST names one it must not."""
    return _origin(os.environ.get("SILICA_LINK_HOST", "").strip() or DEFAULT_HOST)


def _path() -> Path:
    # read per call: a tenant's and the tests' _SILICA_HOME move, and a path bound at import would not
    from silica.kernel.recall import paths

    return paths._SILICA_HOME / "link.json"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _keep(token: str, handle: str, at: str) -> None:
    """Write the link as the gateway gave it. Through a 0600 temporary file and a rename, so the
    token is never readable by another account, nor half written."""
    from silica.kernel.recall.paths import atomic_write_bytes

    now = _now()
    row = {"token": token, "handle": handle, "host": at, "linked_at": now.isoformat(),
           "expires": (now + LIFE).isoformat()}
    atomic_write_bytes(_path(), json.dumps(row).encode("utf-8"), mode=0o600)


def _read() -> dict | None:
    """The link the file holds, None when there is none to use: no file, one that does not parse
    or has another shape, one for a host a token must not go to, or one past its 90 days."""
    from silica.kernel.publish import _HANDLE

    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # no file is no link, and a file that does not parse is one no Publish can trust
        return None
    if not isinstance(raw, dict):
        return None
    token, at, handle = raw.get("token"), raw.get("host"), raw.get("handle")
    if not (isinstance(token, str) and _TOKEN.fullmatch(token) and isinstance(at, str) and at
            and _origin(at) == at):
        return None
    try:
        since = datetime.datetime.fromisoformat(raw["linked_at"])
        expires = datetime.datetime.fromisoformat(raw["expires"])
        if expires <= _now():
            return None
    except (KeyError, TypeError, ValueError):
        # a date missing, not one, or without its zone, which cannot be compared with now
        return None
    return {"token": token, "host": at, "handle": handle if isinstance(handle, str) and _HANDLE.fullmatch(handle)
            else "", "since": since.isoformat(), "expires": expires.isoformat()}


def state() -> dict:
    """{"linked", "host", "handle", "since", "expires"}, what the view shows; never the token."""
    got = _read()
    if got is None:
        return {"linked": False, "host": host(), "handle": "", "since": "", "expires": ""}
    return {"linked": True, **{k: got[k] for k in ("host", "handle", "since", "expires")}}


def unlink() -> None:
    """Forget the link on this computer. The account's Settings remove it on the host's side."""
    _path().unlink(missing_ok=True)
    _forget_sent()


def _forget_sent() -> None:
    """Forget what an account was told: the listing request each row's last Publish carried and a
    published date only a Publish set, so a link to another account reads as never sent."""
    from silica.config import CONFIG
    from silica.kernel import publish

    if not CONFIG.vault_path:
        return
    vault = Path(CONFIG.vault_path)
    try:
        items = publish.vaults(vault)
        if any(v["sent"] or v["sent_listed"] for v in items):
            for v in items:
                if v["sent"]:
                    v["published"] = ""
                v["sent"] = v["sent_listed"] = False
            publish._save_vaults(items, vault)
    except (OSError, ValueError) as exc:
        # The link is gone or new, which is what was asked for; only the view's "Asked" and
        # "Published" lines read one Publish late, until the next one (ValueError: vault.yaml not
        # UTF-8, or one that does not parse, which vault_manifest refuses to write over)
        logger.warning("account link: rows not cleared (%s)", type(exc).__name__)


def _linked_to() -> tuple[str, str] | None:
    """(host, handle) the link file names, past its 90 days too: a link renewed to the same account
    keeps what that account was told. None with no file, or one that cannot say."""
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # no file, or one no Publish could use: nothing says which account the rows were sent to
        return None
    return (raw.get("host"), raw.get("handle")) if isinstance(raw, dict) else None


def _named(at: str) -> str:
    return urlsplit(at).netloc or "Silica hosted"


def _post(url: str, at: str, **kw):
    import httpx

    try:
        # no redirect: one would take the Authorization header somewhere this code never chose
        return httpx.post(url, follow_redirects=False, **kw)
    except httpx.HTTPError as exc:
        # Its words name the address at most, and still never reach the page or the log: the class
        # is enough to tell a refused connection from a timeout.
        logger.warning("account link: %s not reached (%s)", url.split("?", 1)[0], type(exc).__name__)
    # Raised out here and not inside the except: there it would keep the httpx error as its
    # __context__, and that error holds the request, Authorization header included.
    raise ValueError(f"{_named(at)} could not be reached. Check the connection and try again.")


# The gateway's and the tenant's answers are a few hundred bytes; JSON grows as it parses (9 MB of
# [] held 192 MiB, 06c69d5), so a longer one is refused before the parse.
_ANSWER_MAX = 64 * 1024
# a code's life on the gateway (LINK_TTL): the page polls no longer than that, whatever is said
_CODE_LIFE = 600


def _body(r, at: str) -> dict:
    # shortcut: httpx has read the body whole before this check; stream the response if a gateway
    # ever answers megabytes, which no answer of bin/gateway or a tenant does (2026-11-08)
    if len(r.content) > _ANSWER_MAX:
        raise ValueError(f"{_named(at)} answered with more than Silica reads. Try again in a minute.")
    try:
        out = r.json()
    except ValueError:
        # An edge's HTML error page, or an empty body: no field is read from it, and each caller's
        # check of the status and the fields it needs says what went wrong in a sentence of its own
        return {}
    return out if isinstance(out, dict) else {}


def _gateway() -> str:
    at = host()
    if not at:
        raise ValueError("SILICA_LINK_HOST is not an https address, so this Silica cannot link to it.")
    return at


def start() -> dict:
    """A code for the person to confirm: {"code", "poll", "expires_in", "url"}, `url` being the
    page of the gateway that shows it (ADR-0157 point 9, at /link/confirm since Task 9's review).
    Whoever holds `poll` collects the token once the code is confirmed: the caller keeps it."""
    at = _gateway()
    r = _post(f"{at}/link/start", at, json={}, timeout=15)
    if r.status_code == 429:
        wait = r.headers.get("retry-after", "")
        when = f"in {max(1, -(-int(wait) // 60))} minutes" if wait.isdigit() else "later"
        raise ValueError(f"Too many link codes from this network. Try again {when}.")
    got = _body(r, at)
    code, poll, ttl = got.get("code"), got.get("poll"), got.get("expires_in")
    if r.status_code != 200 or not (isinstance(code, str) and _CODE.fullmatch(code) and isinstance(poll, str)
                                    and poll and type(ttl) is int and ttl > 0):
        raise ValueError(f"{_named(at)} gave no link code (it answered {r.status_code}). Try again in a minute.")
    return {"code": code, "poll": poll, "expires_in": min(ttl, _CODE_LIFE),
            "url": f"{at}/link/confirm?code={quote(code)}"}


def poll(poll: str) -> dict:
    """{"state": "pending"} or {"state": "gone"}; once the code is confirmed, the link kept and
    {"state": "linked", **state()}. The gateway hands the token out once, here, and it goes
    straight to the file: what this returns never holds it."""
    from silica.kernel.publish import _HANDLE

    at = _gateway()
    r = _post(f"{at}/link/poll", at, json={"poll": poll}, timeout=15)
    got = _body(r, at)
    said = got.get("state")
    if r.status_code != 200 or said not in ("pending", "linked", "gone"):
        raise ValueError(f"{_named(at)} did not say whether the code was confirmed "
                         f"(it answered {r.status_code}). Try again in a minute.")
    if said != "linked":
        return {"state": said}
    token, handle = got.get("token"), got.get("handle")
    if not (isinstance(token, str) and _TOKEN.fullmatch(token)):
        raise ValueError(f"{_named(at)} answered with a link this Silica cannot keep. Link again.")
    handle = handle if isinstance(handle, str) and _HANDLE.fullmatch(handle) else ""
    before = _linked_to()
    _keep(token, handle, at)
    if before != (at, handle):
        # another account, or one this computer cannot tell is the same: it holds none of the copies
        # the rows say were sent, and its "Turn off on your account" would upload them
        _forget_sent()
    logger.info("account link: this computer is linked to @%s on %s", handle or "?", at)
    if handle:
        _take_handle(handle)
    return {"state": "linked", **state()}


def _take_handle(handle: str) -> None:
    """The account's handle becomes this vault's, so My wikis never asks a linked computer for one
    and a wiki file saved here says the @handle its Publish goes out under."""
    from silica.config import CONFIG
    from silica.kernel.publish import PUBLIC_KEY
    from silica.kernel.vault_manifest import set_block_key

    if not CONFIG.vault_path:
        return  # no vault open: nothing to name, and "." would put a vault.yaml in the working folder
    try:
        set_block_key(CONFIG.vault_path, PUBLIC_KEY, "handle", handle)
    except (OSError, ValueError) as exc:
        # A vault.yaml not writable, or not UTF-8 (UnicodeDecodeError is a ValueError): the link is
        # kept and works, and only the handle overlay asks again until vault.yaml is fixed
        logger.warning("account link: @%s not written into vault.yaml (%s)", handle, type(exc).__name__)


# --- a Publish from the computer (ADR-0157 points 10 and 11) -------------------------------------

def _answer(files: list[str], said: dict) -> dict:
    """The author's answer for a note's sources, as the receiver keys it, {} for a note made from
    none. An open answer first, whose license and credit the page must print.
    shortcut: a note made from two files sends one answer, since the receiver keeps one a note
    (Task 10), so the second file's license and credit never reach the review. 325 of the 1,052
    notes made from a file in alex_second_brain came from two or more (2026-11-08); a list per note
    on the receiver first, then here, by 2027-02-01 or at the first wiki the review finds short."""
    from silica.kernel import rights

    rows = [said.get(f) if isinstance(said.get(f), dict) else {} for f in sorted(files)]
    a = next((r for r in rows if r.get("answer") == "open"), None) \
        or next((r for r in rows if r.get("answer") in rights.ANSWERS), None)
    if a is None:
        return {}
    lic = a.get("license") if a.get("license") in rights.LICENSES else ""
    credit = a.get("credit").strip()[:rights._CREDIT_MAX] if isinstance(a.get("credit"), str) else ""
    return {k: v for k, v in (("answer", a["answer"]), ("license", lic), ("credit", credit)) if v}


def _pointed(text: str, names: dict[str, str], credits: dict[str, str], folder: str) -> str:
    """The note with each image the check let out pointing at its copy under `<folder>/assets/`,
    as the account's vault will hold it, with the credit its answer asks for; every other embed as
    written. The three shapes rights.place_images reads, in its order; its shortcut on code fences
    holds here too."""
    from silica.kernel import rights

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

    return rights._HTML_IMAGE.sub(tag, rights._MD_IMAGE.sub(markdown, rights._WIKI_EMBED.sub(wiki, text)))


def _too_big() -> ValueError:
    from silica.sources import saved_page

    return ValueError(f"This wiki is past the {saved_page.WIKI_FILE_MAX / 1e6:g} MB a Publish may send.")


def _pack(conf: dict, vault: Path, out) -> None:
    """The zip a Publish sends, written into `out` (the receiver is publish.receive): `wiki.json`,
    then the notes the check before Publish lets out as written in the vault, under
    `notes/<path in the folder>`, then `assets/<name>` for each image of theirs it lets out. Never
    a page: the account builds those (ADR-0157 point 10). A remote image is sent nothing; the
    account shows it as a link (Task 10's ruling). Stops past the 100 MB cap before the image or
    note that passes it is read."""
    from silica.kernel import publish, rights
    from silica.kernel.image_meta import strip_location
    from silica.sources import saved_page

    cap = saved_page.WIKI_FILE_MAX
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
        h = _named(at)
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
    return f"{_named(at)} answered {status}. Try again in a minute."


def send(slug: str, vault: Path | None = None, visibility: str | None = None) -> dict:
    """Publish one wiki through the linked account: the notes that go out, as written, their images
    and the author's answers (ADR-0157 points 10 and 11); the account publishes them with its
    own renderer, in the visibility the wiki has here, or `visibility`, which the row takes only
    once the account has taken the wiki, so a refusal here or there leaves the row as it was. An
    Off wiki is sent Off, which takes it off its address on the account. Returns {"vault",
    "notes", "url", "published_at", "sent", "off"}, the account's count and address. ValueError
    with a sentence for anything the person must read, the account's refusals included."""
    import httpx

    from silica.kernel import publish
    from silica.kernel.recall.paths import silica_tmp_dir
    from silica.sources import saved_page

    got = _read()
    if got is None:
        raise ValueError(NOT_LINKED)
    vault = vault or publish._vault()
    row = publish.find(slug, vault)
    conf = {**row, "visibility": visibility or row["visibility"]}
    name = _hosted_folder(conf["folder"])
    # The account replaces a folder any linked computer sent (Task 10), so A/Notes and B/Notes
    # would overwrite each other there, each Publish taking the other's place.
    if sum(_hosted_folder(v["folder"]) == name for v in publish.vaults(vault)) > 1:
        raise ValueError(f"Two of your wikis would publish into the folder {name} on your account: rename one.")
    if len(conf["readers"]) > publish.READERS_MAX:
        raise ValueError(f"A wiki published from this computer names {publish.READERS_MAX} readers at most.")
    at = got["host"]
    # On disk under ~/.silica, unnamed, so it goes when closed even after a crash, and 100 MB is
    # never held in memory
    with tempfile.TemporaryFile(dir=silica_tmp_dir()) as f:
        _pack(conf, vault, f)
        # the notes and images were counted unpacked; the zip adds its headers
        if f.tell() > saved_page.WIKI_FILE_MAX:
            raise _too_big()
        f.seek(0)
        # A wiki of 100 MB, then the account builds its pages before it answers
        r = _post(f"{at}/link/publish", at, content=f, timeout=httpx.Timeout(600, connect=15),
                  headers={"Authorization": f"Bearer {got['token']}", "Content-Type": "application/zip"})
    if r.status_code == 401:
        # The account no longer knows this token: removed in its Settings, or signed out
        # everywhere. Kept, it would read as linked while every Publish fails.
        unlink()
        logger.info("account link: %s refused the token; this computer is unlinked", at)
        raise ValueError("Your account no longer knows this computer. Link it again to publish.")
    out = _body(r, at)
    if r.status_code != 200:
        logger.info("account link: %s answered %s to a Publish of %s", at, r.status_code, slug)
        raise ValueError(_refusal(r.status_code, out.get("detail") if isinstance(out.get("detail"), str) else "", at))
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
