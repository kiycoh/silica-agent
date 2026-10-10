# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ZoteroAdapter, ADR-0055: Zotero is an adapter on the running app's local API.

Reads go to `127.0.0.1:23119/api/users/0`, the API Zotero 7+ serves when
"Allow other applications on this computer to communicate with Zotero" is on.
No dependency beyond httpx: the API is plain REST v3 and the adapter needs
about six calls of it.

A paper with a stored PDF does not go through `stage()`: the planner queues the
PDF for the converter with the paper's leaf (ADR-0055 decision 4), because the
converter is what writes a PDF's text into the vault. `read`/`to_stub` serve
the paper with no PDF, whose leaf is its title, authors and abstract.

Zero-trust (ADR-0009): titles and abstracts come from publishers and arXiv, so
everything that reaches a note passes `strip_degenerate_runs`, and the
identity fields reach the leaf only through yaml.safe_dump.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from silica.sources.base import GroundedStub, RawItem

logger = logging.getLogger(__name__)

ZOTERO_URL = "http://127.0.0.1:23119"
# Connect in 0.5 s: Zotero is on loopback, which answers in milliseconds, and a
# hung Zotero must not stall a session open (ADR-0055 decision 1). The read
# budget is long because `file/view/url` and big pages wait on Zotero's DB.
_TIMEOUT = httpx.Timeout(30.0, connect=0.5)
_PAGE = 100  # the API's own maximum page size

_TARGET_RE = re.compile(r"zotero://select/library/(items|collections)/([A-Z0-9]{8})$")
_CITEKEY_RE = re.compile(r"@([A-Za-z0-9_:.\-]+)$")
_ARXIV_RE = re.compile(r"(?:arxiv\.org/abs/|arXiv[:.])\s*(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})",
                       re.IGNORECASE)
# Where a venue lives depends on the item type; the first one present wins.
_VENUE_FIELDS = ("publicationTitle", "proceedingsTitle", "conferenceName", "bookTitle",
                 "repository", "university", "publisher")


class ZoteroError(ValueError):
    """Zotero cannot serve the request. A ValueError, so `stage()` and the
    planner report it as a skipped target instead of a crash."""


class OtherLibrary(ZoteroError):
    """A Zotero answers, but with a library this vault is not linked to."""

    def __init__(self, answered: str, linked: str):
        super().__init__(f"another Zotero library answers ({answered}); this vault is linked "
                         f"to {linked}")
        self.answered = answered


class Zotero:
    """A client of one running Zotero. `server_id` is the library this vault
    is linked to; a response from any other library is refused, because its
    papers would land in a vault linked elsewhere (ADR-0055 decision 10)."""

    def __init__(self, server_id: str = "", api_key: str = "", *,
                 base_url: str | None = None, transport: httpx.BaseTransport | None = None):
        self.server_id = server_id
        self.api_key = api_key
        # Read at call time, not bound as a default: the suite points it at a
        # closed port so a Zotero running on the developer's machine is never
        # what a test talks to.
        self._http = httpx.Client(base_url=base_url or ZOTERO_URL, timeout=_TIMEOUT,
                                  transport=transport)

    def send(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = self._http.request(method, path, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ZoteroError(
                "Zotero is not running, or its API is off (Zotero › Settings › Advanced "
                "› Allow other applications on this computer to communicate with Zotero)"
            ) from exc
        answered = r.headers.get("Zotero-Server-ID", "")
        if self.server_id and answered and answered != self.server_id:
            raise OtherLibrary(answered, self.server_id)
        return r

    def get(self, path: str, **params) -> httpx.Response:
        r = self.send("GET", f"/api/users/0{path}", params=params)
        if r.status_code == 404:
            raise ZoteroError(f"Zotero has no {path}")
        r.raise_for_status()
        return r

    def item(self, key: str) -> dict:
        return self.get(f"/items/{key}").json()

    def paged(self, path: str, **params) -> list[dict]:
        out: list[dict] = []
        while True:
            r = self.get(path, start=len(out), limit=_PAGE, **params)
            batch = r.json()
            out += batch
            total = int(r.headers.get("Total-Results", len(out)))
            if not batch or len(out) >= total:
                return out

    def library_id(self) -> str:
        return self.send("GET", "/api/").headers.get("Zotero-Server-ID", "")

    def file_path(self, attachment_key: str) -> Path | None:
        """The stored file, read in place from Zotero's storage: no copy and
        no data-folder setting, because Zotero names the path itself."""
        try:
            url = self.get(f"/items/{attachment_key}/file/view/url").text.strip()
        except ZoteroError:
            return None
        parsed = urlparse(url)
        return Path(unquote(parsed.path)) if parsed.scheme == "file" else None

    def bbt_citekey(self, key: str) -> str:
        """Better BibTeX's key for an item whose native `citationKey` is
        empty. Absent BBT, or any failure, is no citekey: one is never made up."""
        try:
            r = self.send("POST", "/better-bibtex/json-rpc", json={
                "jsonrpc": "2.0", "method": "item.citationkey", "params": [[key]]})
            return str((r.json().get("result") or {}).get(key) or "")
        except (ZoteroError, httpx.HTTPError, ValueError):
            return ""


def client() -> Zotero:
    """The client for the active vault's linked library."""
    from silica.kernel.vault_manifest import get_active_manifest

    link = get_active_manifest().zotero
    return Zotero(server_id=link.library if link else "",
                  api_key=os.getenv("SILICA_ZOTERO_API_KEY", ""))


def importing_client() -> Zotero:
    """`client()` for a read that lands a paper in the vault (ADR-0066
    decision 1). Refused in a vault that holds no link while another known
    vault holds the one for the library that answers: its leaves live there,
    and a second copy here is the duplicate ADR-0055 decision 10 prevents. The
    refusal sits here and not in `client()` because every vault may read the
    library; only a leaf has an owner. With no vault linked, imports run as
    they always did."""
    from silica.config import CONFIG

    zot = client()
    if not zot.server_id:
        library = zot.library_id()
        other = linked_elsewhere(library, CONFIG.vault_path) if library else None
        if other is not None:
            raise ZoteroError(f"library {library} is linked to {other}; bring the paper in "
                              "there, this vault reaches it by peek")
    return zot


def parse_target(target: str) -> tuple[str, str] | None:
    t = (target or "").strip()
    m = _TARGET_RE.match(t)
    if m:
        return ("item" if m.group(1) == "items" else "collection", m.group(2))
    m = _CITEKEY_RE.match(t)
    return ("citekey", m.group(1)) if m else None


@dataclass(frozen=True)
class Paper:
    key: str
    fields: dict
    pdf: Path | None
    title: str
    abstract: str

    @property
    def leaf_name(self) -> str:
        ck = self.fields.get("citekey")
        return f"@{ck}.md" if ck else f"@zotero-{self.key}.md"


def _arxiv_of(data: dict) -> str:
    for text in (data.get("archiveID", ""), data.get("DOI", ""), data.get("url", ""),
                 data.get("extra", "")):
        m = _ARXIV_RE.search(text or "")
        if m:
            return m.group(1)
    return ""


def _authors_of(data: dict) -> list[str]:
    people = [c for c in data.get("creators", []) if c.get("creatorType") == "author"]
    out = []
    for c in people or data.get("creators", []):
        if c.get("name"):
            out.append(c["name"])
        elif c.get("lastName"):
            out.append(f"{c['lastName']}, {c['firstName']}" if c.get("firstName") else c["lastName"])
    return out


def paper_of(zot: Zotero, item: dict) -> Paper:
    from silica.kernel.text.sanitize import strip_degenerate_runs

    data = item.get("data", {})
    key = item.get("key") or data.get("key", "")
    fields: dict = {"zotero_key": key}
    citekey = (data.get("citationKey") or "").strip() or zot.bbt_citekey(key)
    if citekey:
        fields["citekey"] = citekey
    title = strip_degenerate_runs(" ".join((data.get("title") or "").split()))
    if title:
        fields["title"] = title
    authors = _authors_of(data)
    if authors:
        fields["authors"] = authors
    year = (item.get("meta", {}).get("parsedDate") or "")[:4]
    if year.isdigit():
        fields["year"] = int(year)
    venue = next((data[f] for f in _VENUE_FIELDS if data.get(f)), "")
    if venue:
        fields["venue"] = venue
    if data.get("DOI"):
        fields["doi"] = data["DOI"]
    arxiv = _arxiv_of(data)
    if arxiv:
        fields["arxiv"] = arxiv

    pdf = None
    att = item.get("links", {}).get("attachment") or {}
    if att.get("attachmentType") == "application/pdf" and att.get("href"):
        att_key = att["href"].rstrip("/").rsplit("/", 1)[-1]
        pdf = zot.file_path(att_key)
        if pdf is not None:
            # The attachment's key is what `zotero://open-pdf` takes, so the
            # leaf can open the PDF in Zotero's reader without asking Zotero.
            fields["pdf_key"] = att_key
            md5 = zot.item(att_key).get("data", {}).get("md5")
            if md5:
                fields["pdf_md5"] = md5
    abstract = strip_degenerate_runs((data.get("abstractNote") or "").strip())
    return Paper(key=key, fields=fields, pdf=pdf, title=title, abstract=abstract)


def _is_paper(item: dict) -> bool:
    return item.get("data", {}).get("itemType") not in ("note", "attachment", "annotation")


def resolve(zot: Zotero, target: str) -> list[Paper]:
    """A target's papers. A collection is its own items only, as Zotero shows
    it by default, not its subcollections'."""
    parsed = parse_target(target)
    if parsed is None:
        raise ZoteroError(f"not a Zotero target: {target}")
    kind, value = parsed
    if kind == "item":
        return [paper_of(zot, zot.item(value))]
    if kind == "collection":
        return [paper_of(zot, i) for i in zot.paged(f"/collections/{value}/items/top") if _is_paper(i)]
    hits = [i for i in zot.paged("/items/top")
            if (i.get("data", {}).get("citationKey") or "") == value]
    if not hits:
        raise ZoteroError(f"no Zotero item has the citekey {value}")
    return [paper_of(zot, hits[0])]


_LEAF_KEY_RE = re.compile(r"^zotero_key:\s*['\"]?([A-Z0-9]{8})", re.MULTILINE)
_LEAF_MD5_RE = re.compile(r"^pdf_md5:\s*['\"]?([0-9a-f]{32})", re.MULTILINE)


def _leaf_index(vault: str | Path | None = None) -> dict[str, tuple[str, str]]:
    """{zotero_key: (leaf basename, pdf_md5)} for the papers in a vault."""
    from silica.config import CONFIG
    from silica.kernel.recall.paths import SOURCES_DIR
    from silica.kernel.vault_manifest import load_manifest

    vault = Path(vault or CONFIG.vault_path or ".")
    write_dir = load_manifest(vault).write_dir or ""
    root = vault / write_dir / SOURCES_DIR
    out: dict[str, tuple[str, str]] = {}
    for p in sorted(root.glob("@*.md")):
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:1500]
        except OSError:
            continue  # a leaf deleted mid-scan is simply not in the vault
        m = _LEAF_KEY_RE.search(head)
        if m and m.group(1) not in out:
            md5 = _LEAF_MD5_RE.search(head)
            out[m.group(1)] = (p.name, md5.group(1) if md5 else "")
    return out


def vault_leaves() -> dict[str, str]:
    """{zotero_key: leaf basename} for the papers already in the vault.

    By key, not by name: a citekey changed in Zotero must find the leaf the
    paper already has, because the provenance ledger keys sources by name.
    """
    return {k: name for k, (name, _) in _leaf_index().items()}


def memberships(zot: Zotero) -> dict[str, list[str]]:
    """{item key: collection names}, read live: collection membership is never
    copied into the vault, because Silica's own filing would make the copy
    stale (ADR-0055 decision 10). Three requests for the founder's 276 items."""
    names = {c["key"]: c["data"]["name"] for c in zot.paged("/collections")}
    return {i["key"]: [names.get(c, c) for c in i["data"].get("collections", [])]
            for i in zot.paged("/items/top") if _is_paper(i)}


def linked_elsewhere(library: str, vault: str | Path) -> Path | None:
    """Another known vault already linked to this library (ADR-0055 decision
    10: one library, one vault; the others reach its papers by peek)."""
    from silica.kernel.recall.vault_registry import known_vaults
    from silica.kernel.vault_manifest import load_manifest

    here = Path(vault).resolve()
    for other in known_vaults():
        other = Path(other)
        if other.resolve() == here:
            continue
        link = load_manifest(other).zotero
        if link is not None and link.library == library:
            return other
    return None


# --- detection at session open (ADR-0055 decision 9, ADR-0038) ------------------

def _client_for(vault: str | Path, link) -> Zotero:
    return Zotero(server_id=link.library, api_key=os.getenv("SILICA_ZOTERO_API_KEY", ""))


def _state_path(vault: str | Path) -> Path:
    from silica.kernel.recall.paths import index_dir_for

    return index_dir_for(str(vault)) / "zotero_state.json"


def last_detection(vault: str | Path) -> dict:
    """What the last session open found, for surfaces that must not move the
    mark themselves (the Library tab)."""
    import json

    try:
        state = json.loads(_state_path(vault).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    return {"new": state.get("new", []), "changed": state.get("changed", []), "other": ""}


def detect(vault: str | Path) -> dict | None:
    """Papers new in Zotero since the last session, and vault papers whose PDF
    changed. Moves the session mark in the index namespace; writes nothing to
    the vault. None when the vault is unlinked or Zotero is closed.

    "New" is by `dateAdded` after the last mark, not by version: Silica's own
    filing and groups bump versions of old items, and those are not new.
    """
    import json
    from datetime import datetime, timezone

    from silica.kernel.vault_manifest import load_manifest

    link = load_manifest(vault).zotero
    if link is None:
        return None
    zot = _client_for(vault, link)
    try:
        r = zot.get("/items/top", limit=1, format="versions")
        version = int(r.headers.get("Last-Modified-Version") or 0)
        path = _state_path(vault)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        new: list[str] = []
        changed: list[str] = []
        if "version" in state and version != state["version"]:
            idx = _leaf_index(vault)
            since = zot.get("/items", since=state["version"], format="versions").json()
            for key in sorted(since):
                item = zot.item(key)
                data = item.get("data", {})
                parent = data.get("parentItem")
                if data.get("itemType") == "attachment":
                    held = idx.get(parent or "", ("", ""))[1]
                    if held and data.get("md5") and data["md5"] != held and parent not in changed:
                        changed.append(parent)
                elif not parent and _is_paper(item) and key not in idx \
                        and data.get("dateAdded", "") >= state.get("at", ""):
                    new.append(key)
    except OtherLibrary as exc:
        return {"new": [], "changed": [], "other": exc.answered}
    except (ZoteroError, httpx.HTTPError):
        return None  # Zotero closed or broken: detection stays silent, commands say why
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": version, "at": now, "new": new, "changed": changed}),
                    encoding="utf-8")
    return {"new": new, "changed": changed, "other": ""}


class ZoteroAdapter:
    name = "zotero"

    def matches(self, target: str) -> bool:
        return parse_target(target) is not None

    def read(self, target: str) -> RawItem:
        papers = resolve(importing_client(), target)
        if len(papers) != 1:
            raise ZoteroError(f"{target} names {len(papers)} papers; the planner expands it")
        paper = papers[0]
        return RawItem(target=target, text=paper.abstract, meta={"paper": paper})

    def to_stub(self, item: RawItem) -> GroundedStub:
        from silica.kernel.recall.paths import SOURCES_DIR
        from silica.kernel.vault_manifest import in_write_dir
        from silica.kernel.write import frontmatter

        paper: Paper = item.meta["paper"]
        head = f"# {paper.title}\n\n" if paper.title else ""
        authors = ", ".join(paper.fields.get("authors", []))
        byline = f"{authors}\n\n" if authors else ""
        body = frontmatter.dump({**paper.fields, "source_id": paper.leaf_name},
                                f"{head}{byline}{item.text}\n")
        return GroundedStub(lane="terminal",
                            note_path=f"{in_write_dir(SOURCES_DIR)}/{paper.leaf_name}",
                            body=body)


ZOTERO = ZoteroAdapter()
