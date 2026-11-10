# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The Page (ADR-0143, ADR-0151): the vaults a person put on it, which of
their notes go out, and where Silica keeps the static files it writes.

A vault on the Page is a folder of the account's one vault, at most ten,
each Off, Public or Private, kept in `vault.yaml` under `public: vaults:`.
A note's own `publish:` in its frontmatter beats its vault, as in Obsidian
Publish. The files themselves are written by `silica.kernel.public_site`,
which renders with the drawer's renderer; this module only decides and
moves folders. What leaves a vault is decided in two steps: this module says
which notes a vault takes (`taken_by`), and `rights.goes_out` which of those
the answers let through.
"""
from __future__ import annotations

from silica.config import live_env, public_base, silica_home, view_words

import logging
import os
import re
import shutil
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)

PUBLIC_KEY = "public"

# Learn's plan writer takes its folder as a default argument, not a constant
# (learn/plans.py), so the name is repeated here; a plan is a study schedule,
# never something a page should show.
PLANS_DIR = "Plans"


def vault_root() -> Path:
    from silica.config import CONFIG

    return Path(CONFIG.vault_path or ".")


def block(vault: Path) -> dict:
    """The raw `public:` block of vault.yaml; {} when absent or malformed."""
    import yaml

    from silica.kernel.base.manifest import MANIFEST_REL

    path = vault / MANIFEST_REL
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    block = raw.get(PUBLIC_KEY) if isinstance(raw, dict) else None
    return _paired(block) if isinstance(block, dict) else {}


_HALF = re.compile("[\ud800-\udfff]")


def _paired(value):
    """`value` with each surrogate pair read as its character. Before 2026-11-08 the writer put an
    emoji as `\\ud83c\\udfa8`, which PyYAML reads as two lone halves that no response or page can
    encode; a half with no partner has no character and reads as U+FFFD."""
    if isinstance(value, str):
        return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace") if _HALF.search(value) else value
    if isinstance(value, list):
        return [_paired(v) for v in value]
    if isinstance(value, dict):
        return {_paired(k): _paired(v) for k, v in value.items()}
    return value


def slugify(text: str) -> str:
    """`Sleep and memory consolidation` -> `sleep-and-memory-consolidation`;
    accents folded, so an Italian title keeps a readable address."""
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")


def handle(vault: Path | None = None) -> str:
    """The page's @name. On the server the host writes it and the account
    cannot take another's (ADR-0143 point 7, amended 2026-11-02: the account
    asks for one from My Wikis, the host decides), so an account without one
    has no page rather than one named after its vault folder."""
    from silica.config import hosted

    env = live_env("SILICA_PUBLIC_HANDLE")
    if env or hosted():
        # The host's answer before the unit's env: a claim granted after the
        # process started is in the file, and the env catches up at the next start.
        named = _host_answer().get("handle")
        return slugify(named if isinstance(named, str) and HANDLE_RE.match(named) else env)
    vault = vault or vault_root()
    named = block(vault).get("handle")
    return slugify(named if isinstance(named, str) and named.strip() else vault.resolve().name)


# The names no account may take: silica-hosted bin/tenant's RESERVED (ADR-0151
# point 12). The host's list decides; this copy only spares a person the
# minute's wait for a refusal it already knows.
RESERVED_HANDLES = frozenset(
    "silica admin administrator root pages page www app api help support security abuse report "
    "privacy terms legal about official staff team mod moderator system null undefined demo static assets".split())


def handle_problem(h: str) -> str:
    """"" when `h` can be a handle, else why not, as My Wikis says it."""
    if not HANDLE_RE.match(h) or "--" in h:
        return "a handle is 1 to 30 of a-z, 0-9 and single dashes, not at either end"
    if h in RESERVED_HANDLES:
        return f"@{h} is reserved"
    return ""


# --- the files this process and silica-hosted pass each other ------------------
# The other end is another repository (its bin/pages), so nothing here can import it and this
# is where the protocol is written down. A rename on either side fails no test of the other.
#   <SILICA_HOST_DIR>/handle.json  root's. {"handle": h} once the account has one,
#                                  {"asked": h, "refused": "taken" | "invalid"} after a refusal.
#   <SILICA_HOST_DIR>/wikis.json   root's. {slug: {"state": "wait" | "in" | "out" | "no",
#                                  "since": date, ...}}, a refusal adding its notice.
#   <silica home>/handle-request.json  the account's. {"handle": h}, read as root and never
#                                  through a link, removed once answered.
#   <SILICA_PUBLIC_DIR>/<handle>/live/<slug>/vault.json  the account's. Read as data, nothing
#                                  trusted beyond its shape (`pages_catalog`, with the host's
#                                  handles.tsv and wikis.tsv beside it).
HANDLE_FILE = "handle.json"
WIKIS_FILE = "wikis.json"
CLAIM_FILE = "handle-request.json"


def _host_dir() -> Path | None:
    """The folder the host writes for this account alone (root's, readable by
    the account's group), None on a host that does not take claims."""
    raw = live_env("SILICA_HOST_DIR")
    return Path(raw) if raw else None


def _host_file(name: str) -> dict:
    import json

    d = _host_dir()
    if d is None:
        return {}
    try:
        raw = json.loads((d / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # No answer yet, or one being replaced: the account reads as having
        # none, which is what it has until the host's rename lands.
        return {}
    return raw if isinstance(raw, dict) else {}


def _host_answer() -> dict:
    """The host's handle.json: {"handle": h} once the account has one,
    {"asked": h, "refused": "taken" | "invalid"} after a claim it refused."""
    return _host_file(HANDLE_FILE)


def host_reviews() -> dict:
    """The host's wikis.json (ADR-0153 point 7), by slug: "state" is wait, in,
    out or no, "since" its date, and a refusal adds the notice of point 13."""
    return _host_file(WIKIS_FILE)


def claim_path() -> Path:
    """Where the account asks for a handle; silica-hosted bin/pages claims reads
    it as root, never through a link, and removes it once answered."""
    return silica_home() / CLAIM_FILE


def _asked() -> str:
    import json

    try:
        h = json.loads(claim_path().read_text(encoding="utf-8")).get("handle")
    except (OSError, ValueError, AttributeError):
        # No request, or one written by hand that the host will refuse too.
        return ""
    return h if isinstance(h, str) else ""


def handle_state(vault: Path | None = None) -> dict:
    """What My Wikis' overlay needs: whether to ask for a handle, the claim
    waiting for the host, and the last one it refused."""
    from silica.config import hosted

    if live_env("SILICA_PUBLIC_HANDLE"):
        return {"ask": False}
    if hosted():
        # A host without the claims pass, or without a public address, would
        # leave the request unanswered: the view says the operator gives handles.
        if _host_dir() is None or not live_env("SILICA_PUBLIC_BASE") or handle():
            return {"ask": False}
        out: dict = {"ask": True, "pending": _asked(), "reserved": sorted(RESERVED_HANDLES)}
        ans = _host_answer()
        if not out["pending"] and isinstance(ans.get("refused"), str):
            out["refused"] = {"handle": str(ans.get("asked") or ""), "why": ans["refused"]}
        return out
    vault = vault or vault_root()
    named = block(vault).get("handle")
    # A laptop that already published under its folder's name keeps it: a new
    # handle would leave those files in a folder nothing points at.
    if (isinstance(named, str) and named.strip()) or site_root(handle(vault)).exists():
        return {"ask": False}
    return {"ask": True, "suggest": handle(vault), "reserved": sorted(RESERVED_HANDLES)}


def claim(h: str, vault: Path | None = None) -> None:
    """Take `h` as the Page's handle. On a laptop it is the vault's own key; on
    the server the host decides, so the request waits where its claims pass
    reads it, once a minute."""
    import json

    from silica.config import hosted
    from silica.kernel.base.manifest import set_block_key

    h = h.strip().lstrip("@").lower()
    if why := handle_problem(h):
        raise ValueError(why)
    if not handle_state(vault).get("ask"):
        raise ValueError("this account already has its handle")
    if hosted():
        p = claim_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"handle": h}), encoding="utf-8")
        os.replace(tmp, p)
        return
    set_block_key(vault or vault_root(), PUBLIC_KEY, "handle", h)


def page_url(vault: Path | None = None) -> str:
    """The page's address, "" when no SILICA_PUBLIC_BASE says where it is served."""
    base = public_base()
    h = handle(vault)
    return f"{base}/@{h}" if base and h else ""


def wiki_url(page: str, slug: str) -> str:
    """A wiki's address under its page's, "" when the page has none."""
    return f"{page}/{slug}/" if page else ""


def available(vault: Path | None = None) -> bool:
    """Whether this account can have a page. On the server it needs the
    address too: without SILICA_PUBLIC_BASE the files link to each other as
    plain files, which the host does not serve under /@handle."""
    from silica.config import hosted

    return bool(handle(vault)) and (bool(page_url(vault)) or not hosted())


# ADR-0151 point 1: a vault on the Page is a folder of the account's one vault. MAX_VAULTS is
# what any account can hold (amendment 2026-11-06) and what vaults() reads up to, never a
# tier's number: _save_vaults writes back what vaults() read, so a read cut at Free's three
# would erase the rest of a downgraded account's rows at its next write.
MAX_VAULTS = 20
FREE_VAULTS = 3
# ADR-0157 Open 4: a wiki file may not pass 100 MB unpacked. Decimal megabytes, as Caddy reads the
# edge's `max_size 100MB`, so the limit the file is built under is the one its upload meets. Here
# and not beside the opener (saved_page), since the build reading it sits under that layer.
WIKI_FILE_MAX = 100_000_000
VISIBILITY = ("off", "public", "private")
# What a wiki built on a share-alike source must itself say (`rights.blocks`).
SHARE_ALIKE_LICENSE = "CC BY-SA 4.0"
# The license select's choices; "not stated" is the default (ADR-0151 answer 15a). The terms a
# wiki grants its readers, each with its version. `rights.LICENSES` is the other list and stays
# another: what a source was published under, by family, with terms no wiki here offers.
LICENSES = ("not stated", "CC BY 4.0", SHARE_ALIKE_LICENSE, "CC BY-NC 4.0", "CC0 1.0", "all rights reserved")
# A vault's slug is a folder of the page beside these, so none may take their names.
_RESERVED_SLUGS = {"index", "assets", "_s", "private", "paused", "vault"}
# A handle, the host's rule too: the catalog reads its map with it, and app-public.js checks a
# field against a copy (`tests/test_public_copies.py`) so a refusal needs no round trip.
HANDLE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,28}[a-z0-9])?$")
# The gateway's link id, as Caddy's matcher lets it reach a tenant (silica-hosted deploy/Caddyfile,
# `handle /link/publish*`). Always fullmatch: `$` also passes a trailing newline.
LINK_ID = re.compile(r"[0-9a-f]{16}")


def _clean_vault(raw) -> dict | None:
    """One `vaults:` entry as the code reads it, None when it names no folder."""
    if not isinstance(raw, dict):
        return None
    folder = raw.get("folder")
    folder = folder.strip().strip("/") if isinstance(folder, str) else ""
    if not folder:
        return None
    text = lambda k, n: raw[k].strip()[:n] if isinstance(raw.get(k), str) else ""  # noqa: E731
    vis = raw.get("visibility")
    lic = raw.get("license")
    readers = r if isinstance(r := raw.get("readers"), list) else []
    link = raw.get("from_link")
    said = a if isinstance(a := raw.get("answers"), dict) else {}
    return {
        "folder": folder,
        "slug": slugify(text("slug", 60)) or slugify(folder.rsplit("/", 1)[-1]) or "vault",
        "title": text("title", 80) or folder.rsplit("/", 1)[-1],
        "line": text("line", 160),
        "license": lic if lic in LICENSES else LICENSES[0],
        "visibility": vis if vis in VISIBILITY else "off",
        "listed": raw.get("listed") is True,
        "readers": sorted({r.strip().lstrip("@").lower() for r in readers
                           if isinstance(r, str) and HANDLE_RE.match(r.strip().lstrip("@").lower())}),
        "published": text("published", 40),
        "downgraded": raw.get("downgraded") is True,
        # the linked computer this folder came from, and what its author answered for each note
        # (ADR-0157 points 10 and 11); "" and {} for a folder of this account's own
        "from_link": link if isinstance(link, str) and LINK_ID.fullmatch(link) else "",
        # on a linked computer: the listing request its last Publish carried, so the view can say
        # that one asked since has not reached the account yet
        "sent_listed": raw.get("sent_listed") is True,
        # the published date is the linked account's, set by a Publish sent there: it goes on unlink
        "sent": raw.get("sent") is True,
        "answers": {k: v for k, v in said.items() if isinstance(k, str) and isinstance(v, dict)},
    }


def vaults(vault: Path | None = None) -> list[dict]:
    """The vaults on the Page, in the order they were made. ADR-0143's
    `folders:` list and `page:` switch are read as vaults until the first
    write replaces them (ADR-0151 point 2): each folder becomes a vault,
    Public when the page was on."""
    public = block(vault or vault_root())
    raw = public.get("vaults")
    if not isinstance(raw, list):
        old = f if isinstance(f := public.get("folders"), list) else []
        on = "public" if public.get("page") is True else "off"
        raw = [{"folder": f, "visibility": on} for f in old if isinstance(f, str)]
    out: list[dict] = []
    slugs: set[str] = set()
    folders: set[str] = set()
    for item in raw:
        v = _clean_vault(item)
        if v is None or v["folder"] in folders or len(out) >= MAX_VAULTS:
            continue
        base, n = v["slug"], 2
        while v["slug"] in slugs or v["slug"] in _RESERVED_SLUGS:
            v["slug"], n = f"{base}-{n}", n + 1
        slugs.add(v["slug"])
        folders.add(v["folder"])
        out.append(v)
    return out


def _save_vaults(items: list[dict], vault: Path) -> None:
    """Write `vaults:` and drop ADR-0143's keys, which it replaces."""
    from silica.kernel.base.manifest import set_block_key, unset_block_key

    # from_link above all: a row written without it would turn the computer's folder into one
    # of the person's, and its next Publish would be refused (ADR-0157 point 10)
    keep = ("folder", "slug", "title", "line", "license", "visibility", "listed", "readers", "published",
            "from_link", "answers", "sent_listed", "sent")
    rows = [{k: v[k] for k in keep if v.get(k) not in ("", [], {}, None, False)}
            | ({"downgraded": True} if v.get("downgraded") else {}) for v in items]
    set_block_key(vault, PUBLIC_KEY, "vaults", rows)
    for old in ("folders", "page"):
        unset_block_key(vault, PUBLIC_KEY, old)


def mark_published(slug: str, day: str, vault: Path) -> None:
    items = vaults(vault)
    for v in items:
        if v["slug"] == slug:
            v["published"] = day
    _save_vaults(items, vault)


def vault_of(path: str, items: list[dict]) -> dict | None:
    """The vault a note is in, None outside every vault."""
    return next((v for v in items if _within(path, v["folder"])), None)


def find(slug: str, vault: Path | None = None) -> dict:
    v = next((v for v in vaults(vault) if v["slug"] == slug), None)
    if v is None:
        raise ValueError(f"no wiki {slug!r} on this page")
    return v


def private_allowed() -> str:
    """"ok", "paid" (the account's tier does not list Private) or "hosted"
    (a laptop has no relay to carry an invitation). The tier says it in its
    views line, as for every paid view (ADR-0140, ADR-0151 point 9): a word
    beside the tab ids, `private`, with no tab of its own; a seat's `all`
    takes it."""
    from silica.config import hosted

    if not hosted():
        # A linked computer's account decides, and its receive refuses a Private that its tier
        # does not list: the gateway never tells a computer the tier (ADR-0157 point 9)
        return "ok" if can_publish() else "hosted"
    return "ok" if view_words() & {"all", "private"} else "paid"


def max_vaults() -> int:
    """How many vaults this account may make (ADR-0151, amendment 2026-11-06).
    The word that allows Private already says who pays (point 9), so a second
    word could only disagree with it; a laptop has no tier and takes the most.
    Only a new vault asks: an account that fell to Free keeps its Public
    vaults, whose addresses readers hold, and makes no more until it is under."""
    return FREE_VAULTS if private_allowed() == "paid" else MAX_VAULTS


def paid_plan() -> list[str]:
    """What the upgrade window lists, in the host's words and beside its numbers:
    silica-agent names no plan (ADR-0140 point 3). Empty for an account with
    nothing left to buy and on a host that sets none, and then no window opens."""
    if private_allowed() != "paid":
        return []
    return [line.strip() for line in live_env("SILICA_PAID_PLAN").split("|") if line.strip()]


def enforce_tier(vault: Path | None = None) -> list[str]:
    """A Private vault of an account that lost Private turns Off (ADR-0151
    point 10); returns the slugs it turned. Readers keep what they saved,
    and the relay has already stopped delivering on the tier."""
    vault = vault or vault_root()
    # only a hosted account loses Private; a laptop never had a relay, and a vault.yaml brought
    # down from the server keeps its choice there rather than reading as a downgrade
    if private_allowed() != "paid":
        return []
    items = vaults(vault)
    turned = [v["slug"] for v in items if v["visibility"] == "private"]
    if not turned:
        return []
    for v in items:
        if v["visibility"] == "private":
            v.update(visibility="off", downgraded=True)
            _drop_private(v["slug"], vault)
    _save_vaults(items, vault)
    return turned


def add_vault(folder: str, vault: Path | None = None) -> dict:
    """Make a folder a vault on the Page; it starts Off (ADR-0151 point 1)."""
    vault = vault or vault_root()
    folder = folder.strip().strip("/")
    items = vaults(vault)
    if not folder or never(folder + "/x", never_dirs()):
        raise ValueError(f"{folder or 'that folder'} cannot be published")
    if not (vault / folder).is_dir():
        raise ValueError(f"{folder} is not a folder of this vault")
    limit = max_vaults()
    if len(items) >= limit:
        raise ValueError(f"you can publish {limit} wikis at most")
    for v in items:
        if _within(folder, v["folder"]) or _within(v["folder"], folder):
            raise ValueError(f"{folder} overlaps the wiki {v['title']} ({v['folder']})")
    new = _clean_vault({"folder": folder, "slug": _free_slug(folder.rsplit("/", 1)[-1], items)})
    assert new is not None  # the folder was refused above when empty, the one way it cleans to None
    items.append(new)
    _save_vaults(items, vault)
    return next(v for v in vaults(vault) if v["folder"] == folder)


def _free_slug(name: str, items: list[dict]) -> str:
    base = slugify(name) or "vault"
    slug, n, taken = base, 2, {v["slug"] for v in items} | _RESERVED_SLUGS
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def update_vault(slug: str, changes: dict, vault: Path | None = None) -> dict:
    """Title, line, license, listing or readers of one vault. Visibility has
    its own call, which moves files."""
    vault = vault or vault_root()
    items = vaults(vault)
    v = next((x for x in items if x["slug"] == slug), None)
    if v is None:
        raise ValueError(f"no wiki {slug!r} on this page")
    if "title" in changes:
        v["title"] = str(changes["title"] or "").strip()[:80] or v["folder"].rsplit("/", 1)[-1]
    if "line" in changes:
        v["line"] = " ".join(str(changes["line"] or "").split())[:160]
    if "license" in changes:
        if changes["license"] not in LICENSES:
            raise ValueError("that is not a license this page offers")
        v["license"] = changes["license"]
    if "listed" in changes:
        v["listed"] = bool(changes["listed"])
    if "readers" in changes:
        me = handle(vault)
        names = [str(r).strip().lstrip("@").lower() for r in changes["readers"] or []]
        bad = [n for n in names if not HANDLE_RE.match(n)]
        if bad:
            raise ValueError(f"@{bad[0]} is not a handle")
        v["readers"] = sorted({n for n in names if n != me})
    _save_vaults(items, vault)
    return next(x for x in vaults(vault) if x["slug"] == slug)


def remove_vault(slug: str, vault: Path | None = None) -> None:
    """Take an Off vault off the Page, its last publication with it; the
    folder and its notes stay as they are."""
    vault = vault or vault_root()
    items = vaults(vault)
    v = next((x for x in items if x["slug"] == slug), None)
    if v is None:
        raise ValueError(f"no wiki {slug!r} on this page")
    # an unlinked computer reads a Public row as Off and offers no way to turn it Off (ADR-0157
    # point 4); its served folder is left where it is, as below
    if v["visibility"] != "off" and can_publish():
        raise ValueError("turn the wiki Off before removing it")
    from silica.config import hosted

    if hosted():  # a computer's paused/ is the person's, as set_visibility leaves it
        shutil.rmtree(site_root(handle(vault)) / "paused" / slug, ignore_errors=True)
    _save_vaults([x for x in items if x["slug"] != slug], vault)


def never_dirs() -> tuple[str, ...]:
    """The folders Silica writes for its own work, never published and given
    no globe (ADR-0143 point 3): the text of every nucleated document sits in
    sources/, the inbox and its archive hold converted files."""
    from silica.kernel.base.layout import SOURCES_DIR
    from silica.kernel.base.manifest import (
        active_done_dir,
        active_inbox_dir,
        get_active_manifest,
        in_write_dir,
    )
    from silica.kernel.workspaces import DEFAULT_FOLDER

    dirs = {active_inbox_dir(), active_done_dir(), in_write_dir(SOURCES_DIR),
            in_write_dir(DEFAULT_FOLDER), in_write_dir(PLANS_DIR),
            get_active_manifest().conventions.templates_dir}
    return tuple(sorted({d.strip("/") for d in dirs if d and d.strip("/")}))


def _within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + "/")


def never(path: str, dirs: tuple[str, ...] | None = None) -> bool:
    """True for a path no globe can publish: under one of Silica's folders,
    or under any dot-folder (.silica, .silica-chat, .obsidian, .trash)."""
    if any(part.startswith(".") for part in path.split("/")[:-1]):
        return True
    return any(_within(path, d) for d in (never_dirs() if dirs is None else dirs))


def _flag(value) -> bool | None:
    """A note's `publish:` as a choice: True, False, or None for no choice.
    A quoted "true" is still a choice; anything else is none."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def state(path: str, fm: dict, items: list[dict], dirs: tuple[str, ...]) -> str:
    """"on" (goes out with its vault), "off", "struck" (left out of its vault
    by its own globe) or "never". A note outside every vault is off whatever
    its `publish:` says (ADR-0151 point 2). A clip is someone else's text, so
    its vault does not take it: only its own globe does (ADR-0143 point 3).
    A copy of someone's Private vault never goes out (ADR-0151 point 9)."""
    if never(path, dirs) or fm.get("shared") == "private":
        return "never"
    if vault_of(path, items) is None:
        return "off"
    own = _flag(fm.get("publish"))
    if own is True:
        return "on"
    if own is False:
        return "struck"
    return "off" if fm.get("form") == "clip" else "on"


def frontmatters(vault: Path | None = None) -> dict[str, dict]:
    """{path: frontmatter} of every note, from the note index."""
    from silica.kernel.base import note_index

    return note_index.frontmatters(vault or vault_root())


def selection(vault: Path | None = None) -> dict:
    """Every note's state, its vault, and the vaults it was computed from."""
    vault = vault or vault_root()
    items = vaults(vault)
    dirs = never_dirs()
    fms = frontmatters(vault)
    notes = {p: state(p, fm, items, dirs) for p, fm in fms.items()}
    of = {p: v["slug"] for p in notes if (v := vault_of(p, items)) is not None}
    return {"vaults": items, "never": list(dirs), "notes": notes, "vault_of": of, "frontmatter": fms}


def taken_by(slug: str, sel: dict) -> list[str]:
    """The notes a vault takes, sorted: on, and in it. What the answers then leave of them is
    `rights.goes_out`, and nothing but it says what a Publish writes."""
    return sorted(p for p, s in sel["notes"].items() if s == "on" and sel["vault_of"].get(p) == slug)


def set_note(path: str, on: bool, vault: Path | None = None) -> str:
    """Put a note's globe on or off inside its vault, writing the least that
    says it: no `publish:` when its vault already takes it, `false` to leave
    it out, `true` for a clip, which only its own globe takes. Returns the
    note's new state."""
    from silica.kernel.write.notes import write_prop

    vault = vault or vault_root()
    dirs = never_dirs()
    if never(path, dirs):
        raise ValueError(f"{path} cannot be published")
    fm = frontmatters(vault).get(path)
    if fm is None:
        raise ValueError(f"{path} is not a note of this vault")
    if fm.get("shared") == "private":
        raise ValueError(f"{path} is a copy of a Private wiki and stays yours alone")
    items = vaults(vault)
    if vault_of(path, items) is None:
        raise ValueError(f"{path} is in no wiki: make its folder a wiki first")
    clip = fm.get("form") == "clip"
    value = (True if clip else None) if on else (None if clip else False)
    write_prop(path, "publish", value)
    return state(path, {**fm, "publish": value} if value is not None
                 else {k: v for k, v in fm.items() if k != "publish"}, items, dirs)


def can_publish() -> bool:
    """Whether this Silica can put a wiki out (ADR-0157 point 4): the hosted service always, a
    computer only through the hosted account it is linked to. Alone, it saves wikis as files."""
    from silica.config import hosted
    from silica.kernel import account_link

    return hosted() or account_link.state()["linked"]


# --- where the files go -------------------------------------------------------

def site_root(h: str | None = None) -> Path:
    """`~/.silica/public/<handle>`: one folder per page, so a laptop serving
    two vaults keeps two pages. `live/` is what the host serves, the Page and
    one folder per Public vault; `paused/<vault>` a vault's last publication
    while it is off; `private/<vault>` a Private vault, for the relay only."""
    base = live_env("SILICA_PUBLIC_DIR")
    root = Path(base).expanduser() if base else silica_home() / "public"
    return root / (h if h is not None else handle())


def set_visibility(slug: str, vis: str, vault: Path | None = None) -> dict:
    """Off, Public or Private for one vault (ADR-0151 point 3). Off moves its
    last publication aside, so its address answers that there is nothing
    there; Public puts it back; Private takes it off the public host, and
    Publish builds it where only the relay reads (point 9)."""
    vault = vault or vault_root()
    if vis not in VISIBILITY:
        raise ValueError("a wiki is off, public or private")
    # before Private's own refusal, which would send an unlinked computer to the hosted service
    if vis != "off" and not can_publish():
        from silica.kernel.account_link import NOT_LINKED

        raise ValueError(NOT_LINKED)
    if vis == "private" and private_allowed() != "ok":
        raise ValueError("Private needs a paid account on Silica hosted"
                         if private_allowed() == "paid" else "Private works between accounts on Silica hosted")
    items = vaults(vault)
    v = next((x for x in items if x["slug"] == slug), None)
    if v is None:
        raise ValueError(f"no wiki {slug!r} on this page")
    v["visibility"], v["downgraded"] = vis, False
    _save_vaults(items, vault)
    from silica.config import hosted

    h = handle(vault)
    # On a computer its live/ and paused/ are left to the person who may be serving them (ADR-0157,
    # Consequences): the account's copy is turned on or off by a Publish, never by moving these.
    if h and hosted():
        root = site_root(h)
        live, paused = root / "live" / slug, root / "paused" / slug
        if vis == "public" and not live.exists() and paused.exists():
            live.parent.mkdir(parents=True, exist_ok=True)
            paused.rename(live)
        elif vis != "public" and live.exists():
            if paused.exists():
                shutil.rmtree(paused)
            paused.parent.mkdir(parents=True, exist_ok=True)
            live.rename(paused)
        if vis != "private":
            _drop_private(slug, vault)
    return find(slug, vault)


def _drop_private(slug: str, vault: Path) -> None:
    """A Private build no longer Private leaves the folder the relay reads,
    so its readers' invitations go at the relay's next pass."""
    h = handle(vault)
    if h:
        shutil.rmtree(site_root(h) / "private" / slug, ignore_errors=True)


def swap_in(build: Path, dest: Path) -> Path:
    """Make a finished build the folder a vault is served or relayed from.
    Two renames, so a visitor can miss the vault for the instant between
    them, never read half of one.
    shortcut: two renames leave a sub-millisecond 404; a symlink swapped with
    os.replace closes it, if a host ever reports a reader hitting that gap."""
    old = dest.with_name(f".old-{dest.name}")
    if old.exists():
        shutil.rmtree(old)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.rename(old)
    build.rename(dest)
    if old.exists():
        shutil.rmtree(old)
    return dest

