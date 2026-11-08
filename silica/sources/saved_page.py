# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Save to Silica (ADR-0143 point 8, ADR-0151 points 8 and 9): a published
note, or the areas a reader picks of a published vault, copied into the
reader's vault under `Saved/@handle/<Vault>/`, never read from another vault.

A vault publishes a `.md` beside each note's `.html` and all of them in
`notes.json`, whose links already point at the author's pages and whose
private links are already plain words, so the copy can only rewrite the
links the reader can serve from their own vault: a note of the same author
they saved before, or one saved in the same write. A Private vault reaches
the reader as the same files, put outside their vault by the host's relay.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

SAVED_DIR = "Saved"
# a note, /@handle/<vault>/<note>, and a vault, /@handle/<vault>/ (ADR-0151 point 4)
_PAGE_PATH = re.compile(r"^/@([a-z0-9-]+)/([a-z0-9-]+)/([a-z0-9-]+)$")
_VAULT_PATH = re.compile(r"^/@([a-z0-9-]+)/([a-z0-9-]+)/$")
_FOLDER_UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]+')
_MD_LINK = re.compile(r"(?<!!)\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_MD_IMAGE = re.compile(r"!\[[^\]\n]*\]\((https?://[^)\s]+)\)")
# ADR-0157 Open 4: a wiki file may not pass 100 MB unpacked. Decimal megabytes, as Caddy reads the
# edge's `max_size 100MB`, so the limit the file is built under is the one its upload meets.
WIKI_FILE_MAX = 100_000_000


def allowed_hosts() -> set[str]:
    # the hosted pages' host (ADR-0143 point 6): the apex is the landing, on GitHub Pages
    raw = os.environ.get("SILICA_PUBLIC_HOSTS", "").strip() or "wikis.silica-agent.dev"
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _on_host(url: str):
    parts = urlsplit((url or "").strip())
    if parts.scheme != "https" or (parts.hostname or "").lower() not in allowed_hosts() \
            or parts.query or parts.fragment or parts.port is not None:
        return None, parts
    return f"https://{(parts.hostname or '').lower()}{parts.path}", parts


def page_of(url: str) -> tuple[str, str, str]:
    """(url, handle, slug) of a published note's address, or ValueError. Only
    an https page on a host the server lists: the confirm card must never be
    a way to make the app fetch an arbitrary address."""
    clean, parts = _on_host(url)
    m = _PAGE_PATH.match(parts.path or "")
    if not clean or not m:
        raise ValueError("that is not the address of a note on a Silica page")
    return clean, m.group(1), m.group(3)


def vault_of(url: str) -> tuple[str, str, str]:
    """(url, handle, vault slug) of a published vault's address, or ValueError."""
    clean, parts = _on_host(url)
    m = _VAULT_PATH.match(parts.path or "")
    if not clean or not m:
        raise ValueError("that is not the address of a wiki on a Silica page")
    return clean, m.group(1), m.group(2)


def is_vault(url: str) -> bool:
    return bool(_VAULT_PATH.match(urlsplit((url or "").strip()).path or ""))


def _wikilink(m: re.Match, path: str, words: str) -> str:
    """A link in place of the match `m`, its bar escaped in a table row, where Obsidian reads a bare one as
    the cell's edge and splits the row. Silica's renderer escapes it itself, so only Obsidian shows that."""
    row = m.string[m.string.rfind("\n", 0, m.start()) + 1:].lstrip()
    bar = "\\|" if row.startswith("|") else "|"
    return f"[[{path}{bar}{words}]]"


def _folder_name(title: str) -> str:
    return _FOLDER_UNSAFE.sub(" ", title or "").strip().strip(".")[:80].strip() or "Saved vault"


def _fetch_markdown(url: str) -> str:
    """The page's `.md`, through the web reader's guard, which revalidates
    every redirect hop (a public page must not be a door into the server)."""
    from silica.sources.web_fetch import _fetch

    resp, final = _fetch(url + ".md", types=("text/",))
    if (urlsplit(final).hostname or "").lower() not in allowed_hosts():
        raise ValueError("that page moved to a host Silica does not save from")
    return resp.text


def saved_copies(handle: str, vault: Path | None = None) -> dict[str, str]:
    """{source url: vault path} of the notes already saved from @handle.
    shortcut: reads Saved/@handle only, so a copy the reader moved elsewhere
    is not found and its link stays a web link; widen to every note's
    `source:` if moved copies turn out common."""
    from silica.kernel.publish import frontmatters
    from silica.kernel.vault_manifest import in_write_dir

    folder = in_write_dir(f"{SAVED_DIR}/@{handle}") + "/"
    out: dict[str, str] = {}
    for path, fm in frontmatters(vault).items():
        src = fm.get("source")
        if path.startswith(folder) and isinstance(src, str):
            out.setdefault(src.strip(), path)
    return out


def _credit(props: dict, handle: str, url: str) -> tuple[str, str]:
    """(author, source) the copy carries. A page that is itself a saved copy
    names its original, and the new copy credits that one: provenance does
    not chain through readers (ADR-0143 point 10). Both come from a file
    someone else wrote, so the credit is taken only when `source` is itself
    a public page address and its handle is the declared author: anything
    else (a path in `author`, a source elsewhere) credits the page fetched."""
    author, source = props.get("author"), props.get("source")
    if isinstance(author, str) and isinstance(source, str):
        try:
            orig, who, _slug = page_of(source)
        except ValueError:
            return f"@{handle}", url
        if who == author.strip().lstrip("@"):
            return f"@{who}", orig
    return f"@{handle}", url


def prepare(url: str, vault: Path | None = None) -> dict:
    """What the confirm card shows, and the note it would write."""
    from silica.kernel.vault_manifest import in_write_dir
    from silica.kernel.write import frontmatter as fm

    url, handle, slug = page_of(url)
    text = _fetch_markdown(url)
    props, _raw, body = fm.split(text)
    props = props or {}
    author, source = _credit(props, handle, url)
    who = author.lstrip("@")
    title = props.get("title") if isinstance(props.get("title"), str) else slug.replace("-", " ")
    mine = saved_copies(who, vault)
    links, seen = [], set()

    def relink(m: re.Match) -> str:
        words, target = m.group(1), m.group(2)
        own = mine.get(target)
        if target not in seen:
            seen.add(target)
            links.append({"text": words, "to": "copy" if own else "web"})
        return _wikilink(m, own.removesuffix(".md"), words) if own else m.group(0)

    new_body = _MD_LINK.sub(relink, body)
    tags = props.get("tags") if isinstance(props.get("tags"), list) else []
    note = {"title": title, **({"tags": tags} if tags else {}), "author": author,
            "source": source, "saved": datetime.date.today().isoformat(), "form": "clip"}
    # a note of a vault lands beside the other notes of that vault the reader saves (ADR-0151 point 8)
    shelf = props.get("vault") if isinstance(props.get("vault"), str) and who == handle else ""
    folder = in_write_dir(f"{SAVED_DIR}/@{who}" + (f"/{_folder_name(shelf)}" if shelf else ""))
    return {
        "url": url, "title": title, "author": author, "source": source, "folder": folder,
        "links": links, "images": len(_MD_IMAGE.findall(body)),
        "existing": mine.get(source, ""),
        "content": fm.dump(note, new_body),
    }


def save(url: str, vault: Path | None = None) -> str:
    """Write the copy; returns its vault path. A second save of one page is a
    second copy beside the first, never a rewrite of it: the first is a
    snapshot the reader may have annotated."""
    from silica.driver import DRIVER

    p = prepare(url, vault)
    name = re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", p["title"]).strip() or "Saved note"
    path, n = f"{p['folder']}/{name}.md", 2
    while _exists(path):
        path, n = f"{p['folder']}/{name} {n}.md", n + 1
    DRIVER.create(path, p["content"])
    return path


def _exists(rel: str) -> bool:
    from silica.config import CONFIG

    return (Path(CONFIG.vault_path or ".") / rel).exists()


# --- a whole vault, or the areas a reader picks of it (ADR-0151 point 8) -------

def _limits() -> tuple[int, int]:
    """(notes, MB) a vault may hold, 0 for no limit: the Free limits are
    still open (ADR-0151 Opens 1), so the host sets them or nothing does."""
    def num(key: str) -> int:
        raw = os.environ.get(key, "").strip()
        return int(raw) if raw.isdigit() else 0
    return num("SILICA_VAULT_MAX_NOTES"), num("SILICA_VAULT_MAX_MB")


def room(adding_notes: int, adding_bytes: int, vault: Path | None = None) -> dict:
    """What the vault holds, what the save adds, and what the limits allow.
    `short` names the shortfall, "" when it fits."""
    from silica.config import CONFIG

    root = Path(vault or CONFIG.vault_path or ".")
    notes = size = 0
    for p in root.rglob("*"):
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts):
            size += p.stat().st_size
            notes += p.suffix == ".md"
    max_notes, max_mb = _limits()
    short = []
    if max_notes and notes + adding_notes > max_notes:
        short.append(f"{notes + adding_notes - max_notes} notes over the {max_notes} this vault may hold")
    if max_mb and size + adding_bytes > max_mb * 1024 * 1024:
        over = (size + adding_bytes - max_mb * 1024 * 1024) / 1024 / 1024
        short.append(f"{over:.1f} MB over the {max_mb} MB this vault may hold")
    return {"notes": notes, "bytes": size, "max_notes": max_notes, "max_mb": max_mb,
            "short": "; ".join(short)}


def _fetch_json(url: str):
    from silica.sources.web_fetch import _fetch

    resp, final = _fetch(url, types=("application/json", "text/"), cap=64 * 1024 * 1024)
    if (urlsplit(final).hostname or "").lower() not in allowed_hosts():
        raise ValueError("that wiki moved to a host Silica does not save from")
    return resp.json()


def _chosen(manifest: dict, areas, named=None) -> list[str]:
    """The note slugs of what a reader picked, whole areas and notes by name
    (ADR-0151, amendment of 2026-11-06, point 8); every note when neither is
    given. -1 is the notes with no link, which the vault's page lists as an
    area; an area that holds nothing, the -2 a page sends beside `notes`, takes
    nothing, so a Silica that reads only `areas` offers none instead of all."""
    rows = [(r[2], r[5]) for r in manifest.get("n", []) if isinstance(r, list) and len(r) >= 6]
    rows += [(-1, r[1]) for r in manifest.get("unlinked", []) if isinstance(r, list) and len(r) >= 2]
    if areas is None and named is None:
        return [slug for _a, slug in rows]
    want = {int(a) for a in areas or () if str(a).lstrip("-").isdigit()}
    names = set(named or ())
    return [slug for a, slug in rows if a in want or slug in names]


def _vault_files(url: str) -> tuple[dict, dict]:
    clean, _h, _slug = vault_of(url)
    return _fetch_json(clean + "vault.json"), _fetch_json(clean + "notes.json")


def prepare_vault(url: str, areas=None, vault: Path | None = None, *, named=None, files=None,
                  private: bool = False) -> dict:
    """What the save sheet shows for a vault, and every note it would write.
    `files` is (manifest, notes) already read, as an invitation's are. A note
    already saved from this vault stays as it is and is counted, not written:
    a reader saves a vault in pieces, and the notes of one vault share one
    folder (ADR-0151, amendment of 2026-11-06, point 10)."""
    from silica.kernel.vault_manifest import in_write_dir
    from silica.kernel.write import frontmatter as fm

    if files is None:
        url, handle, vslug = vault_of(url)
        manifest, notes = _vault_files(url)
    else:
        manifest, notes = files
        handle, vslug = manifest.get("handle", ""), manifest.get("slug", "")
    if not isinstance(manifest, dict) or not isinstance(notes, dict):
        raise ValueError("that wiki could not be read")
    title = t if isinstance(t := manifest.get("title"), str) else vslug
    chosen = [s for s in _chosen(manifest, areas, named) if isinstance(notes.get(s), str)]
    today = datetime.date.today().isoformat()
    folder = in_write_dir(f"{SAVED_DIR}/@{handle}/{_folder_name(title)}")
    if private and _exists(folder):
        # a Private copy carries no `source` to know its notes by, so a second one stays apart
        folder = f"{folder} ({today})"
    base = url if url.endswith("/") else ""
    mine = saved_copies(handle, vault)
    names: dict[str, str] = {}
    titles: dict[str, str] = {}
    taken: set[str] = set()
    already: set[str] = set()
    for slug in chosen:
        props, _raw, _body = fm.split(notes[slug])
        props = props or {}
        _who, src = _credit(props, handle, base + slug if base else "")
        if src in mine:
            # in `names` all the same: a link from a note of this save opens the copy that stayed
            already.add(slug)
            names[slug] = mine[src].removesuffix(".md")
            continue
        t = props.get("title")
        titles[slug] = t.strip() if isinstance(t, str) and t.strip() else slug.replace("-", " ")
        name = _FOLDER_UNSAFE.sub(" ", titles[slug]).strip() or slug
        n, cand = 2, name
        # the disk too: the folder may hold another note of that title, from an earlier save
        while cand.lower() in taken or _exists(f"{folder}/{cand}.md"):
            cand, n = f"{name} {n}", n + 1
        taken.add(cand.lower())
        names[slug] = f"{folder}/{cand}"
    out = {}
    for slug in chosen:
        if slug in already:
            continue
        props, _raw, body = fm.split(notes[slug])
        props = props or {}

        def relink(m: re.Match) -> str:
            words, target = m.group(1), m.group(2)
            tail = target.rsplit("/", 1)[-1].removesuffix(".md")
            if (not base or target.startswith(base)) and tail in names:
                return _wikilink(m, names[tail], words)
            own = mine.get(target)
            if own:
                return _wikilink(m, own.removesuffix(".md"), words)
            # a Private vault's address is never served: a link to a note not saved is its words
            return words if private else m.group(0)

        new_body = _MD_LINK.sub(relink, body)
        tags = props.get("tags") if isinstance(props.get("tags"), list) else []
        # a note the author saved from someone else credits that someone (ADR-0143 point 10)
        author, src = _credit(props, handle, base + slug if base else "")
        note = {"title": titles[slug], **({"tags": tags} if tags else {}), "author": author}
        if src and not (private and author == f"@{handle}"):
            note["source"] = src
        note.update({"saved": today, "form": "clip", **({"shared": "private"} if private else {})})
        out[names[slug] + ".md"] = fm.dump(note, new_body)
    adding = sum(len(t.encode("utf-8")) for t in out.values())
    return {"url": url, "title": title, "author": f"@{handle}", "folder": folder,
            "notes": len(out), "already": len(already), "of": len(_chosen(manifest, None)),
            "areas": manifest.get("areas", []), "room": room(len(out), adding, vault),
            "license": manifest.get("license", ""), "files": out}


def save_vault(url: str, areas=None, vault: Path | None = None, *, named=None, files=None,
               private: bool = False, assets: Path | None = None) -> dict:
    """Write the chosen notes as one run of the undo journal, so `/revert`
    takes the whole save back (ADR-0151 point 8). Refused past the room
    left, naming the shortfall, before anything is written."""
    import hashlib
    import shutil

    from silica.config import CONFIG
    from silica.driver import DRIVER
    from silica.kernel.write.ops import InverseOp, InverseOpKind
    from silica.kernel.write.undo_journal import get_undo_journal

    p = prepare_vault(url, areas, vault, named=named, files=files, private=private)
    if not p["files"]:
        raise ValueError("those notes are saved already" if p["already"] else "no note of that wiki was chosen")
    if p["room"]["short"]:
        raise ValueError(f"this vault has no room for that save: {p['room']['short']}")
    journal = get_undo_journal()
    run_id = journal.start_run(source="vault:save", vault=str(vault or CONFIG.vault_path or ""))
    for path, text in p["files"].items():
        DRIVER.create(path, text)
        post = DRIVER.read_note(path).content or ""
        journal.record(run_id, InverseOp(kind=InverseOpKind.delete_created, path=path),
                       hashlib.sha256(post.encode("utf-8")).hexdigest())
    if assets is not None and assets.is_dir():
        # a Private vault's images travel with it, beside the notes that cite them as assets/<name>
        dest = Path(vault or CONFIG.vault_path or ".") / p["folder"] / "assets"
        dest.mkdir(parents=True, exist_ok=True)
        for f in assets.iterdir():
            if f.is_file() and not f.is_symlink():
                shutil.copyfile(f, dest / f.name)
    return {"folder": p["folder"], "notes": len(p["files"]), "run": run_id,
            "first": next(iter(p["files"]))}


# --- Private vaults shared with this account (ADR-0151 point 9) ----------------
# The host's relay puts each one in <dir>/<author>/<vault>/ (vault.json, notes.json, assets/),
# owned by root and readable by this account alone, outside the vault and its quota. A saved or
# declined one is marked in the account's own `done` folder with the hash the relay wrote, so the
# relay stops offering it until the author publishes it again.
_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,58}[a-z0-9])?$")


def invitations_dir() -> Path:
    raw = os.environ.get("SILICA_INVITATIONS_DIR", "").strip()
    return Path(raw) if raw else Path.home() / ".silica" / "invitations"


def _done_dir() -> Path:
    return Path.home() / ".silica" / "invitations-done"


def _stamp(d: Path) -> str:
    try:
        return (d / ".hash").read_text(encoding="utf-8").strip()[:128]
    except OSError:
        return ""


def _is_done(author: str, slug: str, stamp: str) -> bool:
    try:
        return (_done_dir() / f"{author}.{slug}").read_text(encoding="utf-8").strip() == stamp
    except OSError:
        return False


def file_invitations_dir() -> Path:
    """Where Open a file unpacks a wiki file (ADR-0157 point 7). Not the relay's folder: on the
    hosted service that one is root's, and the account cannot write in it."""
    raw = os.environ.get("SILICA_FILE_INVITATIONS_DIR", "").strip()
    return Path(raw) if raw else Path.home() / ".silica" / "file-invitations"


def _folders(root: Path):
    if not root.is_dir():
        return
    for a in sorted(root.iterdir()):
        if a.is_dir() and _NAME.match(a.name):
            yield from (d for d in sorted(a.iterdir()) if d.is_dir() and _NAME.match(d.name))


def invitations() -> list[dict]:
    """The Private vaults offered to this account and not yet saved or declined, then the wiki
    files opened here. A relay's folder owns its author and wiki: a file of the same two is never
    offered beside it, saved or not, so `_invitation` and this list always mean the same folder."""
    relay = invitations_dir()
    out: list[dict] = []
    for d in _folders(relay):
        if not _is_done(d.parent.name, d.name, _stamp(d)):
            out += _listed(d, file=False)
    for d in _folders(file_invitations_dir()):
        if not (relay / d.parent.name / d.name / "vault.json").is_file():
            out += _listed(d, file=True)
    return out


def _listed(d: Path, *, file: bool) -> list[dict]:
    try:
        m = json.loads((d / "vault.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # half written, or not a manifest: nothing to offer, and the folder is not this list's to fix
        return []
    if not isinstance(m, dict):
        return []
    count = m.get("count") if isinstance(m.get("count"), dict) else {}
    return [{"author": d.parent.name, "vault": d.name,
             "title": str(m.get("title") or d.name)[:80], "line": str(m.get("line") or "")[:160],
             "notes": int(count.get("notes") or 0), "areas": m.get("areas") or [],
             "unlinked": len(m.get("unlinked") or []),
             "published": str(m.get("published") or "")[:10], "license": str(m.get("license") or ""),
             "file": file}]


def _invitation(author: str, slug: str) -> Path:
    if not (_NAME.match(author or "") and _NAME.match(slug or "")):
        raise ValueError("no such invitation")
    # the relay's first, as invitations() lists them
    for root in (invitations_dir(), file_invitations_dir()):
        d = root / author / slug
        if (d / "vault.json").is_file():
            return d
    raise ValueError("that invitation is gone: its author may have withdrawn it")


def _from_file(d: Path) -> bool:
    return d.parent.parent == file_invitations_dir() and (d / ".from-file").is_file()


def _files(d: Path) -> tuple[dict, dict]:
    m = json.loads((d / "vault.json").read_text(encoding="utf-8"))
    n = json.loads((d / "notes.json").read_text(encoding="utf-8"))
    # the author is the folder the host wrote, never the manifest's claim
    m["handle"], m["slug"] = d.parent.name, d.name
    return m, n


def prepare_invitation(author: str, slug: str, areas=None, vault: Path | None = None) -> dict:
    d = _invitation(author, slug)
    m, n = _files(d)
    p = prepare_vault(str(m.get("url") or ""), areas, vault, files=(m, n), private=True)
    p.pop("files")
    return p


def save_invitation(author: str, slug: str, areas=None, vault: Path | None = None) -> dict:
    d = _invitation(author, slug)
    m, n = _files(d)
    out = save_vault(str(m.get("url") or ""), areas, vault, files=(m, n), private=True, assets=d / "assets")
    if _from_file(d):
        # Gone rather than marked done: the mark is keyed author.slug with the file's hash, so the
        # same file opened again would be hidden from the person who chose to open it. A folder that
        # will not go stays offered; the notes are saved all the same, so the save is not undone.
        shutil.rmtree(d, ignore_errors=True)
    else:
        _mark_done(author, slug, _stamp(d))
    return out


def decline_invitation(author: str, slug: str) -> None:
    d = _invitation(author, slug)
    if _from_file(d):
        shutil.rmtree(d)
    else:
        _mark_done(author, slug, _stamp(d))


def _mark_done(author: str, slug: str, stamp: str) -> None:
    done = _done_dir()
    done.mkdir(parents=True, exist_ok=True)
    (done / f"{author}.{slug}").write_text(stamp or "-", encoding="utf-8")


# --- a wiki file, opened (ADR-0157 point 7) ---------------------------------------
# The zip comes from anyone. Its data is read and its pages never: a page is the author's HTML and
# a script, and Silica builds its own from the notes. What is written is only vault.json,
# notes.json and the images under assets/, by names this code makes, so no entry's name decides
# where a byte lands; the refusals below are for a zip no Silica built, which is not opened at all.
# No svg: it can carry a script, and the reader's vault would serve it back.
_ASSET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(png|jpe?g|gif|webp)$", re.IGNORECASE)
_DRIVE = re.compile(r"^[A-Za-z]:")
# A wiki file holds two entries a note (its page and its .md) and one an image: 100,000 is some
# 45,000 notes, far past the largest vault seen (1,780 notes, 2026-10-29), and about 42 MB of
# headers held while zipfile reads them.
_ENTRIES_MAX = 100_000


def _file_too_big() -> ValueError:
    return ValueError(f"that file is past the {WIKI_FILE_MAX / 1e6:g} MB a wiki file may hold")


def _unpacked(z, keep: dict, into: Path) -> None:
    """Each kept entry into `into`, counting what comes out: a zip's sizes are its own claim."""
    held = 0
    for name, info in keep.items():
        with z.open(info) as src, open(into / name, "wb") as out:
            while chunk := src.read(1 << 20):
                held += len(chunk)
                if held > WIKI_FILE_MAX:
                    raise _file_too_big()
                out.write(chunk)


def open_file(data: bytes) -> dict:
    """A wiki file someone sent, as an invitation from a file in
    file_invitations_dir()/<handle>/<slug>/: {"author", "vault", "title", "notes"}. The handle is
    the file's word, which nothing here can check, and the screen says so. Opening the same
    author's wiki again replaces the one opened before; a relay invitation of the two is never
    replaced. ValueError with a sentence on a refusal, and then nothing is written."""
    import hashlib
    import io
    import stat
    import tempfile
    import time
    import zipfile
    import zlib

    if len(data) > WIKI_FILE_MAX:
        raise _file_too_big()
    # zipfile holds every entry's header before any check below can run, about 420 bytes each
    # (measured 2026-11-08), and 100 MB of empty entries is 1.1 M of them. Each one zipfile reads
    # starts with this signature, so its count bounds them, before a byte is parsed.
    if data.count(b"PK\x01\x02") > _ENTRIES_MAX:
        raise ValueError(f"that file holds more than {_ENTRIES_MAX:,} entries, which no wiki file does")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("that file is not a zip Silica can read") from None
    unreadable = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError)
    with z:
        for info in z.infolist():
            name = info.filename
            if name.startswith("/") or "\\" in name or ".." in name.split("/") or _DRIVE.match(name):
                raise ValueError(f"that file holds {name[:120]!r}, a name that leaves its folder")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(f"that file holds {name[:120]!r}, a link, which Silica never unpacks")
        entries = [i for i in z.infolist() if not i.is_dir()]
        heads = [i for i in entries if i.filename.count("/") == 1 and i.filename.endswith("/vault.json")]
        if len(heads) != 1:
            raise ValueError("that zip is not a wiki file: it holds no one <wiki>/vault.json")
        top = heads[0].filename.removesuffix("vault.json")
        keep = {}
        for i in entries:
            rest = i.filename.removeprefix(top) if i.filename.startswith(top) else ""
            if rest in ("vault.json", "notes.json") or (
                    rest.startswith("assets/") and _ASSET.match(rest.removeprefix("assets/"))):
                keep[rest] = i
        if "notes.json" not in keep:
            raise ValueError("that zip is not a wiki file: it holds no notes.json beside its vault.json")
        if sum(i.file_size for i in keep.values()) > WIKI_FILE_MAX:
            raise _file_too_big()
        try:
            m = json.loads(z.read(keep["vault.json"]))
        except (ValueError, *unreadable):
            raise ValueError("that wiki file could not be read: its vault.json is not a wiki's") from None
        if not isinstance(m, dict):
            raise ValueError("that wiki file could not be read: its vault.json is not a wiki's")
        handle, slug = m.get("handle"), m.get("slug")
        if not (isinstance(handle, str) and isinstance(slug, str) and _NAME.match(handle) and _NAME.match(slug)):
            raise ValueError("that file does not name its author and its wiki as Silica names them")
        title = str(m.get("title") or slug)[:80]
        if (invitations_dir() / handle / slug / "vault.json").is_file():
            raise ValueError(f"@{handle}'s {title} already came to you as an invitation, "
                             "and a file does not replace it")
        root = file_invitations_dir()
        root.mkdir(parents=True, exist_ok=True)
        # An Open killed mid-write never reaches its cleanup. An hour is far past any unpacking of
        # 100 MB, so no live one is taken.
        for old in root.glob(".open-*"):
            try:
                if old.stat().st_mtime < time.time() - 3600:
                    shutil.rmtree(old, ignore_errors=True)
            except OSError:
                pass  # gone between the glob and the stat: another Open swept it first
        # Beside the folder it becomes, under a name _NAME refuses, so invitations() never lists a
        # half-written one, a refused one leaves the earlier one as it was, and the move is a rename.
        tmp = Path(tempfile.mkdtemp(prefix=".open-", dir=root))
        try:
            (tmp / "assets").mkdir()
            try:
                _unpacked(z, keep, tmp)
            except unreadable:
                raise ValueError("that wiki file could not be read: the zip is damaged") from None
            try:
                notes = json.loads((tmp / "notes.json").read_text(encoding="utf-8"))
            except ValueError:
                notes = None
            if not isinstance(notes, dict):
                raise ValueError("that wiki file could not be read: its notes.json is not a wiki's")
            (tmp / ".hash").write_text(hashlib.sha256((tmp / "notes.json").read_bytes()).hexdigest(),
                                       encoding="utf-8")
            (tmp / ".from-file").write_bytes(b"")
            dest = root / handle / slug
            dest.parent.mkdir(exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            tmp.rename(dest)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    return {"author": handle, "vault": slug, "title": title, "notes": len(notes)}
