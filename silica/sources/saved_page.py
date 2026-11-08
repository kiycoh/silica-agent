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
                  private: bool = False, assets: Path | None = None) -> dict:
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
    # an empty title is none: a manifest without one saves under its slug, not "Saved vault"
    title = t if isinstance(t := manifest.get("title"), str) and t.strip() else vslug
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
    # the images save_vault copies count against the room too, or a wiki of photos passes it
    adding = sum(len(t.encode("utf-8")) for t in out.values()) + _image_bytes(assets)
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

    p = prepare_vault(url, areas, vault, named=named, files=files, private=private, assets=assets)
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
        for f in _images(assets):
            shutil.copyfile(f, dest / f.name)
    return {"folder": p["folder"], "notes": len(p["files"]), "run": run_id,
            "first": next(iter(p["files"]))}


def _images(assets: Path | None) -> list[Path]:
    if assets is None or not assets.is_dir():
        return []
    return [f for f in assets.iterdir() if f.is_file() and not f.is_symlink()]


def _image_bytes(assets: Path | None) -> int:
    return sum(f.stat().st_size for f in _images(assets))


# --- Private vaults shared with this account (ADR-0151 point 9) ----------------
# The host's relay puts each one in <dir>/<author>/<vault>/ (vault.json, notes.json, assets/),
# owned by root and readable by this account alone, outside the vault and its quota. A saved or
# declined one is marked in the account's own `done` folder with the hash the relay wrote, so the
# relay stops offering it until the author publishes it again.
# Always fullmatch: `$` also matches before a final newline, so "eve\n" passed .match and was
# listed, and "marta\n" got past the relay's folder while it showed as @marta.
_NAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,58}[a-z0-9])?")


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
        if a.is_dir() and _NAME.fullmatch(a.name):
            yield from (d for d in sorted(a.iterdir()) if d.is_dir() and _NAME.fullmatch(d.name))


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


def _text(v, n: int) -> str:
    # A lone surrogate is valid JSON and no UTF-8: the response carrying it could not be encoded,
    # so one such title made every GET of the list a 500.
    return v[:n].encode("utf-8", "replace").decode("utf-8") if isinstance(v, str) else ""


def _int(v) -> int:
    return v if type(v) is int else 0  # a bool is an int to isinstance


def _num(v) -> float:
    # compared, never converted: math.isfinite(10**400) raises OverflowError, and NaN or an
    # infinity fails both comparisons
    return v if type(v) in (int, float) and -1e308 < v < 1e308 else 0


def _manifest(m) -> dict:
    """The fields of a wiki's vault.json that a reader's Silica reads, each of its own type or
    left out. A manifest is the author's, a file's anyone's: one field of the wrong type raised in
    the list every later GET drew, so a single file took Shared with you down for good."""
    m = m if isinstance(m, dict) else {}
    count = m.get("count") if isinstance(m.get("count"), dict) else {}

    def rows(key: str) -> list:
        v = m.get(key)
        return v if isinstance(v, list) else []

    return {
        "handle": _text(m.get("handle"), 60), "slug": _text(m.get("slug"), 60),
        "title": _text(m.get("title"), 80), "line": _text(m.get("line"), 160),
        "license": _text(m.get("license"), 80), "published": _text(m.get("published"), 10),
        "url": _text(m.get("url"), 300), "count": {"notes": _int(count.get("notes"))},
        # an area keeps its place even when its row is not one: a note names its area by index
        "areas": [[_text(a[0], 120), _int(a[1])] if isinstance(a, list) and len(a) >= 2 else ["", 0]
                  for a in rows("areas")],
        "n": [[_num(r[0]), _num(r[1]), r[2], _num(r[3]), _text(r[4], 200), r[5]] for r in rows("n")
              if isinstance(r, list) and len(r) >= 6 and type(r[2]) is int and isinstance(r[5], str)],
        "unlinked": [[_text(r[0], 200), r[1]] for r in rows("unlinked")
                     if isinstance(r, list) and len(r) >= 2 and isinstance(r[1], str)],
    }


def _read_manifest(d: Path) -> dict | None:
    try:
        return _manifest(json.loads((d / "vault.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, RecursionError):
        # half written, too deep or not JSON: nothing to offer, and the folder is not this list's to fix
        return None


def _listed(d: Path, *, file: bool) -> list[dict]:
    m = _read_manifest(d)
    if m is None:
        return []
    return [{"author": d.parent.name, "vault": d.name, "title": m["title"] or d.name, "line": m["line"],
             "notes": m["count"]["notes"], "areas": m["areas"], "unlinked": len(m["unlinked"]),
             "published": m["published"], "license": m["license"], "file": file}]


def _invitation(author: str, slug: str) -> Path:
    if not (_NAME.fullmatch(author or "") and _NAME.fullmatch(slug or "")):
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
    m = _read_manifest(d)
    try:
        n = json.loads((d / "notes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        n = None
    if m is None or not isinstance(n, dict):
        raise ValueError("that invitation could not be read")
    # the author is the folder the host wrote, never the manifest's claim
    m["handle"], m["slug"] = d.parent.name, d.name
    return m, n


def prepare_invitation(author: str, slug: str, areas=None, vault: Path | None = None) -> dict:
    d = _invitation(author, slug)
    m, n = _files(d)
    p = prepare_vault(m["url"], areas, vault, files=(m, n), private=True, assets=d / "assets")
    p.pop("files")
    return p


def save_invitation(author: str, slug: str, areas=None, vault: Path | None = None) -> dict:
    d = _invitation(author, slug)
    m, n = _files(d)
    out = save_vault(m["url"], areas, vault, files=(m, n), private=True, assets=d / "assets")
    _done_with(d)
    return out


def decline_invitation(author: str, slug: str) -> None:
    d = _invitation(author, slug)
    if _from_file(d):
        try:
            shutil.rmtree(d)
        except OSError as exc:
            raise ValueError(f"that file could not be removed ({exc.strerror or exc})") from None
    else:
        _done_with(d)


def _done_with(d: Path) -> None:
    """A saved invitation, or a declined relay one. A file's folder goes rather than being marked
    done: the mark is keyed author.slug with the file's hash, so the same file opened again would
    be hidden from the person who chose to open it. A relay one is marked, and a file of the same
    author and wiki goes with it: hidden behind the relay's folder for good, it would only hold
    one of the ten places. A folder that will not go stays where it was, and the save or the
    decline it follows stands."""
    if _from_file(d):
        shutil.rmtree(d, ignore_errors=True)
        return
    _mark_done(d.parent.name, d.name, _stamp(d))
    copy = file_invitations_dir() / d.parent.name / d.name
    if _from_file(copy):
        shutil.rmtree(copy, ignore_errors=True)


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
_ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(png|jpe?g|gif|webp)", re.IGNORECASE)
_DRIVE = re.compile(r"^[A-Za-z]:")
# A wiki file holds two entries a note (its page and its .md) and one an image: 100,000 is some
# 45,000 notes, far past the largest vault seen (1,780 notes, 2026-10-29), and about 42 MB of
# headers held while zipfile reads them.
_ENTRIES_MAX = 100_000
# Read whole before they are parsed, and JSON grows as it parses: 9 MB of `[]` held 192 MiB. A
# wiki's vault.json runs about 100 bytes a note and its notes.json 3.5 KB (Culture, 26 notes,
# 2026-11-08), so 4 MB is some 40,000 notes and 32 MB some 9,000 notes of that length.
_MANIFEST_MAX = 4_000_000
_NOTES_JSON_MAX = 32_000_000
# What waits in Shared with you from files at once; past it a file is refused until one goes.
_FILES_WAITING_MAX = 10


def _file_too_big() -> ValueError:
    return ValueError(f"that file is past the {WIKI_FILE_MAX / 1e6:g} MB a wiki file may hold")


def _part_too_big(name: str, cap: int) -> ValueError:
    return ValueError(f"that wiki file's {name} is past the {cap / 1e6:g} MB a wiki's may hold")


_DAMAGED = "that wiki file could not be read: the zip is damaged"


def _zip_read(said: str, fn, *args):
    """fn(*args), a read of the zip and nothing else: the zip is anyone's. zipfile, zlib, bz2,
    lzma and struct each raise their own on a crafted archive (a bare OSError from bz2, an
    LZMAError), and the list grows with Python: every one of them is a file that cannot be read,
    a 400 with `said` and not a 500. A refusal of ours, a ValueError, passes as it was. Never
    around a write: a full or unwritable disk is the reader's, and would read as a broken file."""
    try:
        return fn(*args)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"{said} ({type(exc).__name__})") from None


def _unpacked(z, keep: dict, into: Path) -> None:
    """Each kept entry into `into`, counting what comes out: a zip's sizes are its own claim. The
    reads go through _zip_read and the writes do not, so an OSError of the disk reaches the
    route as the disk's."""
    held = 0
    for name, info in keep.items():
        src = _zip_read(_DAMAGED, z.open, info)
        with src, open(into / name, "wb") as out:
            while chunk := _zip_read(_DAMAGED, src.read, 1 << 20):
                held += len(chunk)
                if held > WIKI_FILE_MAX:
                    raise _file_too_big()
                out.write(chunk)


def _flat_notes(text: str) -> dict[str, str]:
    """notes.json as a Silica writes it, an object of strings, read without building anything
    else: json.loads builds every list and object a hostile file nests in it before a check can
    refuse them. ValueError for anything that is not that object, or that holds more notes than
    a wiki file can carry."""
    from json.decoder import scanstring

    ws = re.compile(r"[ \t\n\r]*")
    out: dict[str, str] = {}
    i = ws.match(text).end()
    if text[i:i + 1] != "{":
        raise ValueError("not an object")
    i = ws.match(text, i + 1).end()
    while text[i:i + 1] != "}":
        if out and text[i:i + 1] != ",":
            raise ValueError("not an object of strings")
        if out:
            i = ws.match(text, i + 1).end()
        if text[i:i + 1] != '"':
            raise ValueError("not an object of strings")
        key, i = scanstring(text, i + 1)
        i = ws.match(text, i).end()
        if text[i:i + 1] != ":":
            raise ValueError("not an object of strings")
        i = ws.match(text, i + 1).end()
        if text[i:i + 1] != '"':
            raise ValueError("not an object of strings")
        out[key], i = scanstring(text, i + 1)
        i = ws.match(text, i).end()
        if len(out) > _ENTRIES_MAX // 2:
            raise ValueError("more notes than a wiki file carries")
    if ws.match(text, i + 1).end() != len(text):
        raise ValueError("more after the object")
    return out


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

    if len(data) > WIKI_FILE_MAX:
        raise _file_too_big()
    # zipfile holds every entry's header before any check below can run, about 420 bytes each
    # (measured 2026-11-08), and 100 MB of empty entries is 1.1 M of them. Each one zipfile reads
    # starts with this signature, so its count bounds them, before a byte is parsed.
    if data.count(b"PK\x01\x02") > _ENTRIES_MAX:
        raise ValueError(f"that file holds more than {_ENTRIES_MAX:,} entries, which no wiki file does")
    z = _zip_read("that file is not a zip Silica can read", zipfile.ZipFile, io.BytesIO(data))
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
                    rest.startswith("assets/") and _ASSET.fullmatch(rest.removeprefix("assets/"))):
                keep[rest] = i
        if "notes.json" not in keep:
            raise ValueError("that zip is not a wiki file: it holds no notes.json beside its vault.json")
        if sum(i.file_size for i in keep.values()) > WIKI_FILE_MAX:
            raise _file_too_big()
        if keep["vault.json"].file_size > _MANIFEST_MAX:
            raise _part_too_big("vault.json", _MANIFEST_MAX)
        if keep["notes.json"].file_size > _NOTES_JSON_MAX:
            raise _part_too_big("notes.json", _NOTES_JSON_MAX)
        raw = _zip_read(_DAMAGED, z.read, keep["vault.json"])
        if len(raw) > _MANIFEST_MAX:
            raise _part_too_big("vault.json", _MANIFEST_MAX)
        try:
            m = json.loads(raw)
        except (ValueError, RecursionError):
            raise ValueError("that wiki file could not be read: its vault.json is not a wiki's") from None
        if not isinstance(m, dict):
            raise ValueError("that wiki file could not be read: its vault.json is not a wiki's")
        handle, slug = m.get("handle"), m.get("slug")
        if not (isinstance(handle, str) and isinstance(slug, str)
                and _NAME.fullmatch(handle) and _NAME.fullmatch(slug)):
            raise ValueError("that file does not name its author and its wiki as Silica names them")
        manifest = _manifest(m)
        del raw, m  # the parse of a 4 MB manifest can hold some 90 MB, and nothing below needs it
        title = manifest["title"] or slug
        if (invitations_dir() / handle / slug / "vault.json").is_file():
            raise ValueError(f"@{handle}'s {title} already came to you as an invitation, "
                             "and a file does not replace it")
        root = file_invitations_dir()
        waiting = [d for d in _folders(root) if (d / "vault.json").is_file()]
        if len(waiting) >= _FILES_WAITING_MAX and root / handle / slug not in waiting:
            raise ValueError(f"{_FILES_WAITING_MAX} wiki files are waiting in Shared with you. "
                             "Decline one to open another.")
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
            # the manifest written is the one read, each field of its type, so every later list
            # reads a small file that cannot raise
            parts = {k: v for k, v in keep.items() if k != "vault.json"}
            _unpacked(z, parts, tmp)
            if (tmp / "notes.json").stat().st_size > _NOTES_JSON_MAX:
                raise _part_too_big("notes.json", _NOTES_JSON_MAX)
            try:
                notes = _flat_notes((tmp / "notes.json").read_text(encoding="utf-8"))
            except ValueError:
                raise ValueError("that wiki file could not be read: its notes.json is not a wiki's") from None
            manifest["handle"], manifest["slug"] = handle, slug
            manifest["count"] = {"notes": len(notes)}
            (tmp / "vault.json").write_text(json.dumps(manifest), encoding="utf-8")
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
