# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The Page (ADR-0143, ADR-0151): the vaults a person put on it, which of
their notes go out, and where Silica keeps the static files it writes.

A vault on the Page is a folder of the account's one vault, at most ten,
each Off, Public or Private, kept in `vault.yaml` under `public: vaults:`.
A note's own `publish:` in its frontmatter beats its vault, as in Obsidian
Publish. The files themselves are written by `silica.ui.web.public_site`,
which renders with the drawer's renderer; this module only decides and
moves folders.
"""
from __future__ import annotations

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


def _vault() -> Path:
    from silica.config import CONFIG

    return Path(CONFIG.vault_path or ".")


def _block(vault: Path) -> dict:
    """The raw `public:` block of vault.yaml; {} when absent or malformed."""
    import yaml

    from silica.kernel.vault_manifest import MANIFEST_REL

    path = vault / MANIFEST_REL
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    block = raw.get(PUBLIC_KEY) if isinstance(raw, dict) else None
    return block if isinstance(block, dict) else {}


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

    env = os.environ.get("SILICA_PUBLIC_HANDLE", "").strip()
    if env or hosted():
        # The host's answer before the unit's env: a claim granted after the
        # process started is in the file, and the env catches up at the next start.
        named = _host_answer().get("handle")
        return slugify(named if isinstance(named, str) and _HANDLE.match(named) else env)
    vault = vault or _vault()
    named = _block(vault).get("handle")
    return slugify(named if isinstance(named, str) and named.strip() else vault.resolve().name)


# The names no account may take: silica-hosted bin/tenant's RESERVED (ADR-0151
# point 12). The host's list decides; this copy only spares a person the
# minute's wait for a refusal it already knows.
RESERVED_HANDLES = frozenset(
    "silica admin administrator root pages page www app api help support security abuse report "
    "privacy terms legal about official staff team mod moderator system null undefined demo static assets".split())


def handle_problem(h: str) -> str:
    """"" when `h` can be a handle, else why not, as My Wikis says it."""
    if not _HANDLE.match(h) or "--" in h:
        return "a handle is 1 to 30 of a-z, 0-9 and single dashes, not at either end"
    if h in RESERVED_HANDLES:
        return f"@{h} is reserved"
    return ""


def _host_dir() -> Path | None:
    """The folder the host writes for this account alone (root's, readable by
    the account's group), None on a host that does not take claims."""
    raw = os.environ.get("SILICA_HOST_DIR", "").strip()
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
    return _host_file("handle.json")


def host_reviews() -> dict:
    """The host's wikis.json (ADR-0153 point 7), by slug: "state" is wait, in,
    out or no, "since" its date, and a refusal adds the notice of point 13."""
    return _host_file("wikis.json")


def claim_path() -> Path:
    """Where the account asks for a handle; silica-hosted bin/pages claims reads
    it as root, never through a link, and removes it once answered."""
    return Path.home() / ".silica" / "handle-request.json"


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

    if os.environ.get("SILICA_PUBLIC_HANDLE", "").strip():
        return {"ask": False}
    if hosted():
        # A host without the claims pass, or without a public address, would
        # leave the request unanswered: the view says the operator gives handles.
        if _host_dir() is None or not os.environ.get("SILICA_PUBLIC_BASE", "").strip() or handle():
            return {"ask": False}
        out: dict = {"ask": True, "pending": _asked(), "reserved": sorted(RESERVED_HANDLES)}
        ans = _host_answer()
        if not out["pending"] and isinstance(ans.get("refused"), str):
            out["refused"] = {"handle": str(ans.get("asked") or ""), "why": ans["refused"]}
        return out
    vault = vault or _vault()
    named = _block(vault).get("handle")
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
    from silica.kernel.vault_manifest import set_block_key

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
    set_block_key(vault or _vault(), PUBLIC_KEY, "handle", h)


def page_url(vault: Path | None = None) -> str:
    """The page's address, "" when no SILICA_PUBLIC_BASE says where it is served."""
    base = os.environ.get("SILICA_PUBLIC_BASE", "").strip().rstrip("/")
    h = handle(vault)
    return f"{base}/@{h}" if base and h else ""


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
VISIBILITY = ("off", "public", "private")
# The license select's choices; "not stated" is the default (ADR-0151 answer 15a).
LICENSES = ("not stated", "CC BY 4.0", "CC BY-SA 4.0", "CC BY-NC 4.0", "CC0 1.0", "all rights reserved")
# A vault's slug is a folder of the page beside these, so none may take their names.
_RESERVED_SLUGS = {"index", "assets", "_s", "private", "paused", "vault"}
_HANDLE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,28}[a-z0-9])?$")
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
                           if isinstance(r, str) and _HANDLE.match(r.strip().lstrip("@").lower())}),
        "published": text("published", 40),
        "downgraded": raw.get("downgraded") is True,
        # the linked computer this folder came from, and what its author answered for each note
        # (ADR-0157 points 10 and 11); "" and {} for a folder of this account's own
        "from_link": link if isinstance(link, str) and LINK_ID.fullmatch(link) else "",
        # on a linked computer: the listing request its last Publish carried, so the view can say
        # that one asked since has not reached the account yet
        "sent_listed": raw.get("sent_listed") is True,
        "answers": {k: v for k, v in said.items() if isinstance(k, str) and isinstance(v, dict)},
    }


def vaults(vault: Path | None = None) -> list[dict]:
    """The vaults on the Page, in the order they were made. ADR-0143's
    `folders:` list and `page:` switch are read as vaults until the first
    write replaces them (ADR-0151 point 2): each folder becomes a vault,
    Public when the page was on."""
    block = _block(vault or _vault())
    raw = block.get("vaults")
    if not isinstance(raw, list):
        old = f if isinstance(f := block.get("folders"), list) else []
        on = "public" if block.get("page") is True else "off"
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
    from silica.kernel.vault_manifest import set_block_key, unset_block_key

    # from_link above all: a row written without it would turn the computer's folder into one
    # of the person's, and its next Publish would be refused (ADR-0157 point 10)
    keep = ("folder", "slug", "title", "line", "license", "visibility", "listed", "readers", "published",
            "from_link", "answers", "sent_listed")
    rows = [{k: v[k] for k in keep if v.get(k) not in ("", [], {}, None, False)}
            | ({"downgraded": True} if v.get("downgraded") else {}) for v in items]
    set_block_key(vault, PUBLIC_KEY, "vaults", rows)
    for old in ("folders", "page"):
        unset_block_key(vault, PUBLIC_KEY, old)


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
    names = {v.strip() for v in os.environ.get("SILICA_VIEWS", "").split(",")}
    return "ok" if names & {"all", "private"} else "paid"


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
    return [line.strip() for line in os.environ.get("SILICA_PAID_PLAN", "").split("|") if line.strip()]


def enforce_tier(vault: Path | None = None) -> list[str]:
    """A Private vault of an account that lost Private turns Off (ADR-0151
    point 10); returns the slugs it turned. Readers keep what they saved,
    and the relay has already stopped delivering on the tier."""
    vault = vault or _vault()
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
    vault = vault or _vault()
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
    vault = vault or _vault()
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
        bad = [n for n in names if not _HANDLE.match(n)]
        if bad:
            raise ValueError(f"@{bad[0]} is not a handle")
        v["readers"] = sorted({n for n in names if n != me})
    _save_vaults(items, vault)
    return next(x for x in vaults(vault) if x["slug"] == slug)


def remove_vault(slug: str, vault: Path | None = None) -> None:
    """Take an Off vault off the Page, its last publication with it; the
    folder and its notes stay as they are."""
    vault = vault or _vault()
    items = vaults(vault)
    v = next((x for x in items if x["slug"] == slug), None)
    if v is None:
        raise ValueError(f"no wiki {slug!r} on this page")
    # an unlinked computer reads a Public row as Off and offers no way to turn it Off (ADR-0157
    # point 4); its served folder is left where it is, as below
    if v["visibility"] != "off" and can_publish():
        raise ValueError("turn the wiki Off before removing it")
    root = site_root(handle(vault))
    shutil.rmtree(root / "paused" / slug, ignore_errors=True)
    _save_vaults([x for x in items if x["slug"] != slug], vault)


def never_dirs() -> tuple[str, ...]:
    """The folders Silica writes for its own work, never published and given
    no globe (ADR-0143 point 3): the text of every nucleated document sits in
    sources/, the inbox and its archive hold converted files."""
    from silica.kernel.recall.paths import SOURCES_DIR
    from silica.kernel.vault_manifest import (
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
    """{path: frontmatter} of every note, the walk Workspaces already memoizes."""
    from silica.kernel.workspaces import _frontmatters

    return _frontmatters(vault or _vault())


def selection(vault: Path | None = None) -> dict:
    """Every note's state, its vault, and the vaults it was computed from."""
    vault = vault or _vault()
    items = vaults(vault)
    dirs = never_dirs()
    fms = frontmatters(vault)
    notes = {p: state(p, fm, items, dirs) for p, fm in fms.items()}
    of = {p: v["slug"] for p in notes if (v := vault_of(p, items)) is not None}
    return {"vaults": items, "never": list(dirs), "notes": notes, "vault_of": of, "frontmatter": fms}


def set_note(path: str, on: bool, vault: Path | None = None) -> str:
    """Put a note's globe on or off inside its vault, writing the least that
    says it: no `publish:` when its vault already takes it, `false` to leave
    it out, `true` for a clip, which only its own globe takes. Returns the
    note's new state."""
    vault = vault or _vault()
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
    _write_publish(path, value)
    return state(path, {**fm, "publish": value} if value is not None
                 else {k: v for k, v in fm.items() if k != "publish"}, items, dirs)


def _write_publish(path: str, value: bool | None) -> None:
    write_prop(path, "publish", value)


def write_prop(path: str, key: str, value: bool | None) -> None:
    """Set or remove a note's property through the driver, so the change is a
    session change like any edit, and leave /undo a restore point: it reads the
    checkpoints and not the session's ledger. Not `overwrite`, which stamps a
    `type:` the person never asked for. Lowercase YAML booleans, from the dumper."""
    from silica.driver import DRIVER
    from silica.kernel.write import frontmatter as fm
    from silica.kernel.write.checkpoints import push_landed

    prior = DRIVER.read_note(path).content
    data, raw, _body = fm.split(prior)
    if data is None and raw is not None:
        raise ValueError(f"the properties of {path} do not parse; fix them before publishing it")
    if value is not None:
        DRIVER.set_prop(path, key, value)
    else:
        unset = getattr(DRIVER, "unset_prop", None)
        if unset is None:
            # The Obsidian bridge has no removal; Obsidian itself does.
            raise ValueError(f"remove `{key}:` from {path} in Obsidian")
        unset(path, key)
    try:
        # `hand`: the person's click and no body of Silica's, or a note they edited would read
        # as untouched to `edited_since_write` and could not be marked as theirs (ADR-0153 point 26).
        push_landed(path, prior, prior, kind="hand")
    except Exception as exc:
        # The property is on disk: only /undo loses this step, as for a patch in tools/notes.py.
        logger.warning("checkpoint push failed for '%s': %s; /undo has no restore point for it", path, exc)


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
    base = os.environ.get("SILICA_PUBLIC_DIR", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".silica" / "public"
    return root / (h if h is not None else handle())


def set_visibility(slug: str, vis: str, vault: Path | None = None) -> dict:
    """Off, Public or Private for one vault (ADR-0151 point 3). Off moves its
    last publication aside, so its address answers that there is nothing
    there; Public puts it back; Private takes it off the public host, and
    Publish builds it where only the relay reads (point 9)."""
    vault = vault or _vault()
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
    h = handle(vault)
    if h:
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


# --- a wiki a linked computer sends (ADR-0157 points 10 and 11) -----------------

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
    from silica.kernel import rights
    from silica.sources.saved_page import _NAME

    if not isinstance(m, dict) or m.get("visibility") not in VISIBILITY \
            or not isinstance(m.get("answers"), dict):
        raise ValueError(_BAD_HEAD)
    readers, listed = m.get("readers", []), m.get("listed")
    # a handle each, within the 30 characters update_vault takes, so nothing is refused after the
    # row is written; `listed` carried is a bool, null included in what is refused
    if ("listed" in m and not isinstance(listed, bool)) or not isinstance(readers, list) or len(readers) > READERS_MAX \
            or not all(isinstance(r, str) and _NAME.fullmatch(r) and _HANDLE.fullmatch(r) for r in readers):
        raise ValueError(_BAD_HEAD)
    head = {k: _said(m.get(k), n) for k, n in (("folder", 4096), ("slug", 60), ("title", 80),
                                                ("line", 160), ("license", 80))}
    if head["license"] not in LICENSES:
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
    items = vaults(vault)
    v = next((x for x in items if x["folder"] == folder), None)
    if v is None:
        v = _clean_vault({"folder": folder, "slug": _free_slug(meta["slug"] or folder, items)})
        assert v is not None  # receive refused an empty folder name, the one way it cleans to None
        items.append(v)
    # shortcut: the answers ride in vault.yaml, which every vaults() parses whole, about 100 bytes
    # a note; give them a file beside the row past 1,000 answered notes in one account
    v.update(from_link=link, title=meta["title"] or folder, line=" ".join(meta["line"].split()),
             license=meta["license"], answers=meta["answers"])
    _save_vaults(items, vault)
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
    from silica.sources import saved_page as sp

    vault = vault or _vault()
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
        if not _plain(folder) or "/" in folder or "\\" in folder or never(folder + "/x", never_dirs()):
            raise ValueError(f"{folder[:80] or 'that folder'} cannot be a wiki's folder here")
        items = vaults(vault)
        row = next((v for v in items if v["folder"] == folder), None)
        dest = vault / folder
        # Any link's mark and not only this one's: a link lasts 90 days and a new one gets a new
        # id, so the computer's own folder would refuse it. Point 10 refuses a folder with no mark.
        ours = row is not None and bool(row["from_link"])
        # A link in the vault's place is the person's too: replacing it would write where it leads.
        if (dest.exists() or dest.is_symlink()) and not (ours and dest.is_dir() and not dest.is_symlink()):
            raise FolderTaken(f"A folder named {folder} already holds notes of yours: "
                              "rename one of them to publish this wiki.")
        if row is None and len(items) >= (limit := max_vaults()):
            raise ValueError(f"you can publish {limit} wikis at most")
        if meta["visibility"] == "private" and private_allowed() != "ok":
            raise ValueError("Private needs a paid account on Silica hosted")
        if not available(vault):
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
            update_vault(slug, {"readers": meta["readers"]}
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
    set_visibility(slug, meta["visibility"], vault)
    return {"slug": slug, "folder": folder, "visibility": meta["visibility"]}
