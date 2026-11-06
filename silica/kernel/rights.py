# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What may leave a vault (ADR-0153 points 9, 20 and 26): one question, whose is it, for each file
a note was made from, each folder of images and each site. What nobody answered for stays out.

`read_wiki` reads the facts of one wiki, `decide` turns them and the answers into what goes out,
`sheet` into what the check before Publish shows. The build calls `decide` itself, so an answer
binds whether or not anyone opened the screen.

The fetch of a remote image is passed in: the kernel does not reach the network.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from silica.kernel.base import notekey
from silica.kernel.base.formats import IMAGE_SUFFIX, SHOWN_IMAGE_EXTS
from silica.kernel import publish
from silica.kernel.base.noteshape import SOURCES_TAIL_RE
from silica.kernel.base.syntax import EMBED_RE, LINK_TARGET_RE

logger = logging.getLogger(__name__)

ANSWERS = ("mine", "open", "else")
PUBLIC_DOMAIN = "Public domain or CC0"
NO_DERIVATIVES = "CC BY-ND"
SHARE_ALIKE = "CC BY-SA"
LICENSES = (PUBLIC_DOMAIN, "CC BY", SHARE_ALIKE, "CC BY-NC", NO_DERIVATIVES)
# The wiki's own license a share-alike source asks for, from `publish.LICENSES`.
_SHARE_ALIKE_WIKI = "CC BY-SA 4.0"
_KEY = "sources"
# ADR-0153 Open 3: the share of a note copied word for word that cannot count as the author's. My
# number; only the runs behind it are measured (point 10).
COPIED = 30
# Eight words in a row shared with the source: a run this long is copying, not a shared phrase.
_RUN = 8
_CREDIT_MAX = 200
_OUT = "*Image not published*"

_WORD = re.compile(r"\w+", re.U)
# One level of brackets inside the address: Wikipedia's file names carry them.
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(\s*<?((?:[^()\s>]|\([^()\s]*\))+)>?(?:\s+[\"'][^)]*[\"'])?\s*\)")
_HTML_IMAGE = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']?([^\"'\s>]+)[\"']?[^>]*>", re.I)
_REMOTE = re.compile(r"(?:https?:)?//", re.I)
# The mark's line, with the block around it when it is the note's only property.
_MARK = re.compile(r"\A---\r?\nrewritten:\s*true[ \t]*\r?\n---\r?\n|^rewritten:\s*true[ \t]*\r?\n", re.M)
_KEPT_NAME = re.compile(r"[0-9a-f]{24}\.[a-z]+")
_PERSONAL = (
    ("an email", re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)),
    ("a phone", re.compile(r"(?<![\d.,])(?:\+\d{1,3}[ .-]?\d{2,4}[ .-]?\d{3,4}[ .-]?\d{3,4}"
                           r"|3\d{2}[ -]?\d{6,7})(?![\d.,]*\d)")),
    ("a tax code", re.compile(r"\b[A-Z]{6}\d{2}[A-EHLMPR-T]\d{2}[A-Z]\d{3}[A-Z]\b")),
)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b")


def answers(vault: Path | None = None) -> dict:
    """{key: answer} from `public: sources:`. A key is a file's name, `folder:<path>` or
    `site:<host>`; a file holds for every wiki, so the answers live beside `vaults:`."""
    raw = publish._block(vault or publish._vault()).get(_KEY)
    return raw if isinstance(raw, dict) else {}


def save_answer(key: str, answer: str, *, license: str = "", credit: str = "",
                off: list[str] | None = None, vault: Path | None = None) -> None:
    """Record one answer; "" takes it back, so the thing is asked again."""
    from silica.kernel.base.manifest import set_block_key

    if answer and answer not in ANSWERS:
        raise ValueError(f"not an answer: {answer}")
    if license and license not in LICENSES:
        raise ValueError(f"not a license: {license}")
    vault = vault or publish._vault()
    items = answers(vault)
    if answer:
        row = {"answer": answer, "license": license, "credit": credit.strip()[:_CREDIT_MAX],
               "off": sorted(set(off or []))}
        items[key] = {k: v for k, v in row.items() if v}
    else:
        items.pop(key, None)
    set_block_key(vault, publish.PUBLIC_KEY, _KEY, items)


def is_free(answer: dict, default_credit: str = "") -> bool:
    """Whether an answer lets a thing out: Mine, or Open with a license that allows something made
    from it and a credit, which only the public domain does without."""
    if answer.get("answer") == "mine":
        return True
    if answer.get("answer") != "open":
        return False
    license = answer.get("license", "")
    if license not in LICENSES or license == NO_DERIVATIVES:
        return False
    return license == PUBLIC_DOMAIN or bool(answer.get("credit", "").strip() or default_credit)


def _unanswered(answer: dict, default_credit: str = "") -> bool:
    if not answer.get("answer"):
        return True
    return (answer["answer"] == "open" and answer.get("license") != NO_DERIVATIVES
            and not is_free(answer, default_credit))


# --- measures ------------------------------------------------------------------

def _words(text: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(text)]


def _runs(words: list[str]) -> set[int]:
    # Hashes and not the tuples: a book's leaf is half a million runs, and a tenant's memory is
    # the host's tightest number (project_host_headroom).
    return {hash(tuple(words[i:i + _RUN])) for i in range(len(words) - _RUN + 1)}


def _share(words: list[str], runs: set[int]) -> int:
    if not words:
        return 0
    hit = [False] * len(words)
    for i in range(len(words) - _RUN + 1):
        if hash(tuple(words[i:i + _RUN])) in runs:
            hit[i:i + _RUN] = [True] * _RUN
    return round(100 * sum(hit) / len(words))


def copied_share(note: str, leaf: str) -> int | None:
    """Percent of `note` in runs of eight words shared with `leaf`; None across languages, where
    the runs see nothing: 1,199 of 1,756 pairs measured on 2026-11-06 (ADR-0153 point 10)."""
    from silica.kernel.text.language import detect

    if detect(note) != detect(leaf):
        return None
    return _share(_words(note), _runs(_words(leaf)))


def _is_iban(text: str) -> bool:
    s = text.replace(" ", "")
    return int("".join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1


def personal_data(text: str) -> list[str]:
    """What a note shows of someone: an email, a phone, a tax code, an IBAN (ADR-0153 point 9)."""
    found = [name for name, pattern in _PERSONAL if pattern.search(text)]
    if any(_is_iban(m.group(0)) for m in _IBAN.finditer(text)):
        found.append("an IBAN")
    return found


# --- what a note embeds --------------------------------------------------------

def _is_image(target: str) -> bool:
    return target.lower().endswith(SHOWN_IMAGE_EXTS)


def _embeds(body: str) -> tuple[list[str], list[str]]:
    """(local names, remote addresses) a note's body shows, in order."""
    local: list[str] = []
    remote: list[str] = []
    for raw in EMBED_RE.findall(body):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if _is_image(target):
            local.append(target)
    for target in [m.group(2) for m in _MD_IMAGE.finditer(body)] + _HTML_IMAGE.findall(body):
        if _REMOTE.match(target):
            remote.append(_address(target))
        elif not target.startswith("data:") and _is_image(unquote(target)):
            local.append(unquote(target))
    return local, remote


def _address(target: str) -> str:
    return "https:" + target if target.startswith("//") else target


def _name_key(target: str) -> str:
    return Path(target.replace("\\", "/")).name.casefold()


def place_images(body: str, plan: dict) -> str:
    """A note's body with each image as `plan` says, written once so the page and the `.md` both
    read it: a published image is a plain embed, a copied one an embed of its copy with its credit,
    a local one left out says so, and a remote one left out is a link naming its host.
    shortcut: an image inside a code fence is rewritten too, as `_md_for_reader` does for links;
    split on fences if a published note ever shows one mangled."""
    def credit(key: str, link: str = "") -> str:
        said = plan["credits"].get(key, "")
        if not said:
            return ""
        return f"\n\n*[{said}]({link})*" if link else f"\n\n*{said}*"

    def remote(target: str, alt: str) -> str:
        url = _address(target)
        kept = plan["remote"].get(url)
        if kept:
            return f"![[{kept}|{alt}]]{credit(url, url)}" if alt else f"![[{kept}]]{credit(url, url)}"
        return f"[Image from {urlsplit(url).hostname or 'another site'}]({url})"

    def wiki(m: re.Match) -> str:
        target = m.group(1).split("|", 1)[0].split("#", 1)[0].strip()
        if not _is_image(target):
            return m.group(0)
        key = _name_key(target)
        return m.group(0) + credit(key) if key in plan["local"] else _OUT

    def markdown(m: re.Match) -> str:
        alt, target = m.group(1), m.group(2)
        if _REMOTE.match(target):
            return remote(target, alt)
        name = unquote(target)
        if target.startswith("data:") or not _is_image(name):
            return m.group(0)
        key = _name_key(name)
        if key not in plan["local"]:
            return _OUT
        # as a wikilink, the one form `_md_for_reader` points at the wiki's copy
        return (f"![[{name}|{alt}]]" if alt else f"![[{name}]]") + credit(key)

    def tag(m: re.Match) -> str:
        target = m.group(1)
        if _REMOTE.match(target):
            return remote(target, "")
        name = unquote(target)
        if target.startswith("data:") or not _is_image(name):
            return m.group(0)
        key = _name_key(name)
        return f"![[{name}]]{credit(key)}" if key in plan["local"] else _OUT

    return _HTML_IMAGE.sub(tag, _MD_IMAGE.sub(markdown, EMBED_RE.sub(wiki, body)))


# --- copies of remote images ---------------------------------------------------

def _kept_dir(vault: Path) -> Path:
    from silica.kernel.base.home import index_dir_for

    return index_dir_for(str(vault)) / "public_images"


def _kept(url: str, vault: Path) -> Path | None:
    stem = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    root = _kept_dir(vault)
    return next(root.glob(stem + ".*"), None) if root.is_dir() else None


def kept_image(name: str, vault: Path | None = None) -> Path | None:
    """A kept copy by file name, for the screen's thumbnail; None for anything but such a name."""
    if not _KEPT_NAME.fullmatch(name):
        return None
    path = _kept_dir(vault or publish._vault()) / name
    return path if path.is_file() else None


def _keep(url: str, data: bytes, ctype: str, vault: Path) -> Path:
    """A copy lasts: `build` remakes `assets/` from the vault, so without one every Publish would
    fetch again, and a site gone away would turn a copy back into a link (ADR-0153 point 20)."""
    from silica.kernel.base.durable import atomic_write_bytes

    root = _kept_dir(vault)
    root.mkdir(parents=True, exist_ok=True)
    path = root / (hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] + IMAGE_SUFFIX.get(ctype, ".jpg"))
    atomic_write_bytes(path, data, mode=0o600)
    return path


# --- the facts of a wiki -------------------------------------------------------

def _title(path: str, fm: dict) -> str:
    t = fm.get("title")
    return t.strip() if isinstance(t, str) and t.strip() else notekey.stem(path)


def _body(text: str) -> str:
    """A note as it goes out, so a remark the build strips is no finding, and text it keeps is read."""
    from silica.kernel.base.markdown import strip_ofm_meta
    from silica.kernel.base import frontmatter

    return strip_ofm_meta(frontmatter.split(text)[2], published=True)


def _folder_of(path: str, root: str) -> str:
    rest = path[len(root) + 1:].split("/", 1)
    return f"{root}/{rest[0]}" if len(rest) > 1 else root


def _images_on_disk(vault: Path) -> dict[str, Path]:
    """{name: file}, one walk, the way `_find_asset` takes a bare name.
    shortcut: an image is told apart by its name, as `![[x.png]]` names it, so two files of one
    name share one answer and one thumbnail. No name repeats among the 885 image files measured on
    2026-11-06, where pasted names carry a date; key on the path if a vault of `image.png` exports
    shows up."""
    out: dict[str, Path] = {}
    for p in sorted(vault.rglob("*")):
        if p.suffix.lower() in SHOWN_IMAGE_EXTS and p.is_file() \
                and not any(s.startswith(".") for s in p.relative_to(vault).parts):
            out.setdefault(p.name.casefold(), p)
    return out


def _hand_edits(vault: Path) -> tuple[dict[str, str | None], set[str]]:
    """({note key: what Silica last wrote, as a hash}, note keys edited since). Both empty when it
    cannot be read: every note then has no record, which the screen says and the review reads."""
    from silica.kernel.base.notekey import note_key
    from silica.kernel.write.undo_journal import edited_since_write, written_notes

    try:
        known = {note_key(p): h for p, h in written_notes(str(vault)).items()}
        return known, {note_key(p) for p in edited_since_write(str(vault))}
    except Exception as exc:
        logger.warning("rights: journal not read (%s)", exc)
        return {}, set()


def _only_marked(path: Path, written: str | None) -> bool:
    """Whether a note differs from what Silica wrote by its `rewritten:` line alone. The journal
    hashes the whole file, so the mark typed by hand reads as the edit it claims (ADR-0153 point 26).
    shortcut: a mark set through a tool that reformats the properties reads as an edit; compare
    bodies if the review ever meets one."""
    from silica.kernel.write.undo_journal import content_hash

    return content_hash(_MARK.sub("", path.read_text(encoding="utf-8"), count=1)) == written


def _read_sources(vault: Path, notes: dict, wanted: dict[str, list[str]], names: dict[str, str]) -> dict:
    """One pass over the leaves in `sources/`, one held at a time: what each names as its file, the
    images it embeds, and the share of each wanted note. `wanted` is {leaf stem: [note paths]}.
    Returns {image key: file} and fills `names`, {leaf stem: file}, and each note's share."""
    from silica.kernel.base.layout import SOURCES_DIR
    from silica.kernel.text.language import detect
    from silica.kernel.base.manifest import in_write_dir
    from silica.kernel.base import frontmatter

    traced: dict[str, str] = {}
    credits: dict[str, str] = {}
    root = vault / in_write_dir(SOURCES_DIR)
    for leaf in sorted(root.rglob("*.md")) if root.is_dir() else ():
        data, _raw, body = frontmatter.split(leaf.read_text(encoding="utf-8", errors="ignore"))
        data = data or {}
        stem = leaf.stem.casefold()
        raw = str(data.get("source_file") or "")
        file = Path(raw.replace("\\", "/")).name or leaf.name
        names[stem] = file
        said = ", ".join(str(data[k]) for k in ("authors", "source_title") if data.get(k))
        if said:
            credits[file] = said
        for target in _embeds(body)[0]:
            traced.setdefault(_name_key(target), file)
        paths = wanted.get(stem)
        if not paths:
            continue
        runs, language = _runs(_words(body)), detect(body[:20000])
        for p in paths:
            n = notes[p]
            n["measured"] = True
            if detect(n["text"]) != language:
                continue
            n["share"] = max(n["share"] or 0, _share(_words(n["text"]), runs))
    return {"traced": traced, "credits": credits}


def read_wiki(slug: str, vault: Path | None = None, fetch: Callable[[str], dict] | None = None) -> dict:
    """The facts of one wiki. `fetch` brings a remote image with no kept copy: the screen passes one,
    the build does not, since a Publish must not wait on a stranger's site."""
    from silica.kernel.write.provenance import read_records
    from silica.kernel.base.notekey import note_key

    vault = vault or publish._vault()
    conf = publish.find(slug, vault)
    sel = publish.selection(vault)
    fms = sel["frontmatter"]
    paths = sorted(p for p, s in sel["notes"].items() if s == "on" and sel["vault_of"].get(p) == slug)
    notes: dict[str, dict] = {}
    for p in paths:
        text = _body((vault / p).read_text(encoding="utf-8", errors="ignore")) if (vault / p).is_file() else ""
        notes[p] = {"title": _title(p, fms.get(p) or {}), "text": SOURCES_TAIL_RE.sub("", text), "body": text,
                    "files": [], "rewritten": (fms.get(p) or {}).get("rewritten") is True,
                    "edited": None, "share": None, "measured": False}

    by_key = {note_key(p): p for p in paths}
    wanted: dict[str, list[str]] = {}
    fallback: dict[str, str] = {}
    for r in read_records(vault_path=str(vault)):
        source = str(r.get("source") or "") if isinstance(r, dict) else ""
        if not source:
            continue
        stem = Path(source).stem.casefold()
        fallback.setdefault(stem, source)
        for n in r.get("notes") or []:
            p = by_key.get(note_key(n))
            if p and p not in wanted.setdefault(stem, []):
                wanted[stem].append(p)
    names: dict[str, str] = {}
    leaves = _read_sources(vault, notes, wanted, names)

    files: dict[str, dict] = {}
    for stem, mine in wanted.items():
        file = names.get(stem) or fallback[stem]
        for p in mine:
            if file not in notes[p]["files"]:
                notes[p]["files"].append(file)
            row = files.setdefault(file, {"notes": [], "images": [], "credit": leaves["credits"].get(file, "")})
            if p not in row["notes"]:
                row["notes"].append(p)

    known, edits = _hand_edits(vault)
    on_disk = _images_on_disk(vault)
    local: dict[str, dict] = {}
    remote: dict[str, dict] = {}
    titles = {Path(p).stem.casefold(): p for p in paths}
    links: list[tuple[str, str]] = []
    nowhere: dict[str, int] = {}
    for p, n in notes.items():
        key = note_key(p)
        if n["files"] and key in known:
            n["edited"] = key in edits
            if n["edited"] and n["rewritten"] and _only_marked(vault / p, known[key]):
                n["edited"] = n["rewritten"] = False
        n["personal"] = personal_data(n["body"])
        for target in LINK_TARGET_RE.findall(n["body"]):
            hit = titles.get(Path(target.strip()).name.casefold())
            if hit is None:
                nowhere[p] = nowhere.get(p, 0) + 1
            elif hit != p:
                links.append((p, hit))
        shown_local, shown_remote = _embeds(n["body"])
        for target in shown_local:
            name = _name_key(target)
            found = on_disk.get(name)
            if found is None:
                continue
            row = local.setdefault(name, {
                "name": found.name, "src": found.relative_to(vault).as_posix(), "notes": [],
                "file": leaves["traced"].get(name), "group": "folder:" + _folder_of(p, conf["folder"]),
                "location": _located(found)})
            row["notes"].append(p)
        for url in shown_remote:
            row = remote.setdefault(url, _remote_row(url, vault, fetch))
            row["notes"].append(p)
    for row in local.values():
        if row["file"] and row["file"] not in files:
            files[row["file"]] = {"notes": [], "images": [], "credit": leaves["credits"].get(row["file"], "")}
        if row["file"]:
            files[row["file"]]["images"].append(row["name"].casefold())
    # The facts stay cached while the screen is open, and a tenant's memory is the host's tightest
    # number: no note's text outlives this read.
    for n in notes.values():
        del n["text"], n["body"]
    return {"slug": slug, "folder": conf["folder"], "license": conf["license"], "visibility": conf["visibility"],
            "notes": notes, "files": files, "local": local, "remote": remote, "links": links,
            "nowhere": nowhere, "asked": set(answers(vault))}


def _located(path: Path) -> bool:
    from silica.kernel.image_meta import has_location

    try:
        return has_location(path.read_bytes())
    except OSError as exc:
        # Unreadable here is unreadable at Publish too, which then leaves the image out.
        logger.debug("rights: %s not read (%s)", path, exc)
        return False


def _remote_row(url: str, vault: Path, fetch: Callable[[str], dict] | None) -> dict:
    site = urlsplit(url).hostname or "another site"
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or site
    kept = _kept(url, vault)
    why = ""
    if kept is None and fetch is not None:
        got = fetch(url)
        why = got["why"]
        if not why:
            kept = _keep(url, got["data"], got["type"], vault)
    elif kept is None:
        why = "Could not be read"
    return {"url": url, "site": site, "name": name, "kept": kept, "why": why,
            "size": kept.stat().st_size if kept else 0, "group": "site:" + site, "notes": []}


# --- what goes out -------------------------------------------------------------

def _can_mark(n: dict) -> bool:
    if n["share"] is not None and n["share"] >= COPIED:
        return False
    return n["rewritten"] or n["edited"] is not False


def decide(wiki: dict, ans: dict) -> dict:
    """What goes out: `notes`, `local` image keys, `remote` {address: copy's name}, `files` {copy's
    name: path}, `credits` {image key or address: words}, `note_credits` {note: words}."""
    free = {f: is_free(ans.get(f, {})) for f in wiki["files"]}
    notes = set()
    for p, n in wiki["notes"].items():
        if all(free[f] for f in n["files"]):
            notes.add(p)
        elif n["rewritten"] and (n["share"] is None or n["share"] < COPIED):
            notes.add(p)

    local: set[str] = set()
    credits: dict[str, str] = {}
    for key, img in wiki["local"].items():
        if not notes.intersection(img["notes"]):
            continue
        a = ans.get(img["file"] or img["group"], {})
        if is_free(a) and img["name"] not in a.get("off", []):
            local.add(key)
            if a.get("answer") == "open" and a.get("credit"):
                credits[key] = a["credit"]

    remote: dict[str, str] = {}
    files: dict[str, Path] = {}
    for url, img in wiki["remote"].items():
        a = ans.get(img["group"], {})
        default = f"from {img['site']}"
        if img["kept"] and notes.intersection(img["notes"]) and is_free(a, default) \
                and img["name"] not in a.get("off", []):
            remote[url] = img["kept"].name
            files[img["kept"].name] = img["kept"]
            credits[url] = a.get("credit") or default

    note_credits: dict[str, str] = {}
    for p in notes:
        said = [f"{a['credit']}, {a['license']}" if a.get("credit") else a["license"]
                for f in wiki["notes"][p]["files"] if (a := ans.get(f, {})).get("answer") == "open"]
        if said:
            note_credits[p] = "; ".join(said)
    return {"notes": notes, "local": local, "remote": remote, "files": files, "credits": credits,
            "note_credits": note_credits}


def blocks(wiki: dict, ans: dict, plan: dict) -> list[str]:
    """Why this wiki cannot ask to enter Wikis (ADR-0153 points 9 and 16), in the screen's words."""
    if wiki["visibility"] == "private":
        return []
    out = []
    if wiki["license"] == "not stated":
        out.append('It cannot ask to enter Wikis while its license is "not stated".')
    personal = sum(1 for p in plan["notes"] if wiki["notes"][p]["personal"])
    if personal:
        what = "the note in Other checks is" if personal == 1 else f"the {personal} notes in Other checks are"
        out.append(f"It can ask to enter Wikis once {what} fixed.")
    share_alike = any(ans.get(f, {}).get("license") == SHARE_ALIKE and is_free(ans.get(f, {}))
                      for f in wiki["files"])
    if share_alike and wiki["license"] != _SHARE_ALIKE_WIKI:
        out.append(f"It can ask to enter Wikis once its license is {_SHARE_ALIKE_WIKI}.")
    return out


def statement(wiki: dict, ans: dict, plan: dict, slugs: dict[str, str]) -> dict:
    """What the review reads in `vault.json`: per source its answer, license, credit and notes, and
    the notes marked as the author's. Never a file's name: it can say where it was downloaded."""
    sources = []
    for f in sorted(wiki["files"]):
        a = ans.get(f, {})
        count = sum(1 for p in plan["notes"] if f in wiki["notes"][p]["files"])
        if count and is_free(a):
            row = {"answer": a["answer"], "license": a.get("license", ""), "credit": a.get("credit", ""),
                   "notes": count}
            sources.append({k: v for k, v in row.items() if v})
    marked = [slugs[p] for p in sorted(plan["notes"]) if p in slugs and _marked(wiki, ans, p)]
    return {"sources": sources, "rewritten": marked}


def _marked(wiki: dict, ans: dict, p: str) -> bool:
    n = wiki["notes"][p]
    return n["rewritten"] and not all(is_free(ans.get(f, {})) for f in n["files"])


def check_mark(wiki: dict, path: str, on: bool = True) -> None:
    """Refuse a mark Silica can see is false (ADR-0153 point 26); taking one back is always free."""
    n = wiki["notes"].get(path)
    if n is None or not n["files"]:
        raise ValueError(f"{path} is not a note made from a file of this wiki")
    if on and not _can_mark(n):
        raise ValueError(f"{n['title']} cannot be marked as yours: {_status(n)}")


def set_rewritten(path: str, on: bool) -> None:
    publish.write_prop(path, "rewritten", True if on else None)


# --- what the screen shows -----------------------------------------------------

def _status(n: dict) -> str:
    if n["share"] is not None and n["share"] >= COPIED:
        return f"{n['share']}% copied word for word"
    if n["edited"] is False and not n["rewritten"]:
        return "Not edited since Silica wrote it"
    was = "No record of what Silica wrote" if n["edited"] is None else "Edited"
    if n["share"] is not None:
        return f"{was} · {n['share']}% copied"
    return f"{was} · {'other language, not measured' if n['measured'] else 'not measured'}"


def _answer_fields(a: dict, default_credit: str = "") -> dict:
    return {"answer": a.get("answer", ""), "license": a.get("license", ""),
            "credit": a.get("credit") or default_credit}


def _file_row(wiki: dict, ans: dict, plan: dict, name: str) -> dict:
    a = ans.get(name, {})
    row = wiki["files"][name]
    notes = []
    for p in sorted(row["notes"], key=lambda q: (not _can_mark(wiki["notes"][q]), wiki["notes"][q]["title"].casefold())):
        n = wiki["notes"][p]
        held = next((f for f in n["files"] if f != name and not is_free(ans.get(f, {}))), "")
        notes.append({"path": p, "title": n["title"], "status": _status(n), "can": _can_mark(n),
                      "mine": n["rewritten"], "goes": p in plan["notes"], "held": held})
    return {"key": name, "name": name, "old": name in wiki["asked"], "free": is_free(a), "notes": notes,
            "images": [_thumb(wiki["local"][k]) for k in sorted(row["images"])],
            **_answer_fields(a, row["credit"])}


def _thumb(img: dict) -> dict:
    return {"name": img["name"], "src": "/asset?path=" + quote(img["src"])}


def _group_rows(wiki: dict, ans: dict, plan: dict) -> list[dict]:
    rows: dict[str, dict] = {}

    def row(key: str, kind: str, name: str, default: str = "") -> dict:
        a = ans.get(key, {})
        return rows.setdefault(key, {
            "key": key, "kind": kind, "name": name, "old": key in wiki["asked"], "images": [], "bad": [],
            "free": is_free(a, default), "off": a.get("off", []), **_answer_fields(a, default)})

    for img in sorted(wiki["local"].values(), key=lambda i: i["name"].casefold()):
        if img["file"] or not plan["notes"].intersection(img["notes"]):
            continue
        r = row(img["group"], "folder", img["group"].removeprefix("folder:") + "/")
        r["images"].append({**_thumb(img), "note": _first_title(wiki, img), "on": img["name"] not in r["off"]})
    for img in sorted(wiki["remote"].values(), key=lambda i: i["url"]):
        if not plan["notes"].intersection(img["notes"]):
            continue
        r = row(img["group"], "site", img["site"], f"from {img['site']}")
        if img["why"]:
            r["bad"].append({"name": img["name"], "why": img["why"]})
        else:
            r["images"].append({"name": img["name"], "src": "/public/check/image?name=" + img["kept"].name,
                                "note": _first_title(wiki, img), "on": img["name"] not in r["off"]})
    return sorted(rows.values(), key=lambda r: (r["kind"] != "folder", r["name"].casefold()))


def _natural(name: str) -> list:
    """Lezione 2 before Lezione 10: 33 of the measured vault's files are numbered lectures."""
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", name)]


def _first_title(wiki: dict, img: dict) -> str:
    return wiki["notes"][img["notes"][0]]["title"]


def sheet(wiki: dict, ans: dict) -> dict:
    """The check before Publish as the screen draws it."""
    plan = decide(wiki, ans)
    files = [_file_row(wiki, ans, plan, f) for f in sorted(wiki["files"], key=_natural)]
    groups = _group_rows(wiki, ans, plan)
    personal = [[wiki["notes"][p]["title"], what] for p in sorted(plan["notes"])
                for what in wiki["notes"][p]["personal"]]
    going_local = len(plan["local"])
    going_remote = len(plan["remote"])
    links_left = sum(1 for img in wiki["remote"].values()
                     if plan["notes"].intersection(img["notes"]) and img["url"] not in plan["remote"]
                     and img["why"] != "Not an image")
    marked = sum(1 for p in plan["notes"] if _marked(wiki, ans, p))
    said = blocks(wiki, ans, plan)
    wikis = "" if wiki["visibility"] == "private" else (said[0] if said else "It can ask to enter Wikis.")
    if wikis and marked:
        wikis += f" The review reads the {'note' if marked == 1 else f'{marked} notes'} you marked as yours."
    open_file = any(_unanswered(ans.get(f["key"], {})) for f in files)
    open_group = any(g["images"] and _unanswered(ans.get(g["key"], {}), g["credit"]) for g in groups)
    return {
        "ask": open_file or open_group or bool(personal),
        "licenses": list(LICENSES),
        "files": files,
        "groups": groups,
        "plain": sum(1 for n in wiki["notes"].values() if not n["files"]),
        "from_files": sum(len(row["images"]) for row in wiki["files"].values()),
        "checks": {
            "personal": personal,
            "personal_notes": sum(1 for p in plan["notes"] if wiki["notes"][p]["personal"]),
            "location": sum(1 for img in wiki["local"].values()
                            if img["location"] and plan["notes"].intersection(img["notes"])),
            "lost": sum(1 for a, b in wiki["links"] if a in plan["notes"] and b not in plan["notes"]),
            "nowhere": sum(c for p, c in wiki["nowhere"].items() if p in plan["notes"]),
        },
        "count": {"notes": len(plan["notes"]), "stay": len(wiki["notes"]) - len(plan["notes"]),
                  "images": going_local + going_remote, "kept": len(wiki["local"]) - going_local,
                  "links": links_left},
        "wikis": wikis,
    }
