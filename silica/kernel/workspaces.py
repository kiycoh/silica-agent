# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia
"""Workspaces (ADR-0112): a `type: workspace` note that gathers notes and
folders from anywhere in the vault without moving them.

A workspace is read, never stored elsewhere: its three frontmatter lines
(`notes:` wikilinks, `folders:` live paths, `where:` "key: value" selectors)
are resolved by a walk of the vault each time, plus one hop over `sources:`
so the notes nucleated from a member come with it. The note's body is the
user's own words, read as standing instructions by a chat opened in it.
"""
from __future__ import annotations

import re
import threading
from contextvars import ContextVar
from pathlib import Path

from silica.kernel.base import note_index
from silica.kernel.base.layout import vault_root
from silica.kernel.base.notekey import store_key
from silica.kernel.base.noteshape import NOT_MEMBERS, WORKSPACE, type_word
from silica.kernel.base.syntax import NAME_UNSAFE_RE, WIKILINK_NAME_RE

LINES = ("notes", "folders", "where")
DEFAULT_FOLDER = "Workspaces"

_H1_RE = re.compile(r"^#\s+(.*?)\s*$")


def is_workspace(data: dict | None) -> bool:
    return isinstance(data, dict) and type_word(data) == WORKSPACE


def _as_list(value) -> list[str]:
    if value is None:
        return []
    # Hand-written YAML: `- [[Note]]` unquoted is a nested list and
    # `- course: stats` unquoted a mapping; str() of either matched nothing and
    # the next edit wrote it back as `[['Note']]`.
    if isinstance(value, dict):
        return [f"{k}: {v}" for k, v in value.items()]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _as_list(v)]
    text = str(value).strip()
    return [text] if text else []


def title_of(entry: str) -> str:
    """`[[Bayes theorem|Bayes]]` -> `Bayes theorem`; a bare title stays."""
    m = WIKILINK_NAME_RE.search(entry)
    return (m.group(1) if m else entry).strip()


def _mirror_copies(paths) -> set[str]:
    """Safe mode stages a patched note at `silica/<same path>`
    (vault_manifest.seed_mirror_copy): that copy counts as its original and is
    never a member of its own, as ADR-0109 point 1 counts it."""
    from silica.kernel.base.manifest import mirrors, original_of

    if not mirrors():
        return set()
    have = set(paths)
    return {p for p in have if original_of(p, exact=True) in have}


def read_workspace(path: str, text: str | None = None) -> dict:
    from silica.kernel.base import frontmatter

    if text is None:
        text = (vault_root() / path).read_text(encoding="utf-8")
    data, _raw, body = frontmatter.split(text)
    data = data or {}
    lines = body.lstrip("\n").split("\n")
    name = Path(path).stem
    # The H1 is the note's title as Obsidian shows it, not an instruction.
    if lines and (m := _H1_RE.match(lines[0])):
        name = m.group(1) or name
        lines = lines[1:]
    return {
        "path": path,
        "name": name,
        # every link of a line: a text property `"[[A]], [[B]]"` lost B on the next edit
        "notes": [t.strip() for x in _as_list(data.get("notes")) for t in (WIKILINK_NAME_RE.findall(x) or [x])],
        "folders": [f.replace("\\", "/").strip("/") for f in _as_list(data.get("folders"))],
        "where": _as_list(data.get("where")),
        "instructions": "\n".join(lines).strip(),
    }


def list_workspaces() -> list[dict]:
    vault = vault_root()
    out = []
    for path, data in note_index.frontmatters(vault).items():
        if not is_workspace(data):
            continue
        try:
            out.append(read_workspace(path))
        except (OSError, UnicodeDecodeError):
            continue  # deleted between the walk and the read: it is gone
    return sorted(out, key=lambda w: w["name"].casefold())


def find_workspace(ref: str) -> dict | None:
    """By vault path, by `[[wikilink]]`, by stem or by its H1, casefolded."""
    if not ref:
        return None
    want = title_of(str(ref)).removesuffix(".md").casefold()
    for w in list_workspaces():
        if w["path"].removesuffix(".md").casefold() == want or Path(w["path"]).stem.casefold() == want \
                or w["name"].casefold() == want:
            return w
    return None


def _where_match(data: dict, line: str) -> bool:
    key, sep, value = line.partition(":")
    if not sep:
        return False
    key, value = key.strip().casefold(), value.strip().casefold()
    hit = next((v for k, v in data.items() if str(k).casefold() == key), None)
    if hit is None:
        return False
    return any(str(v).strip().casefold() == value for v in _as_list(hit))


def members(ws: dict) -> dict[str, list[dict]]:
    """{path: [reason, ...]} in walk order. A reason is {"kind": listed |
    folder | where | derived, "via": the line or source that brought it}."""
    from silica.kernel.base.layout import in_folder

    fms = note_index.frontmatters(vault_root())
    mirrors = _mirror_copies(fms)
    material = {p: d for p, d in fms.items()
                if p not in mirrors and type_word(d) not in NOT_MEMBERS}
    by_stem: dict[str, str] = {}
    for p in material:
        by_stem.setdefault(Path(p).stem.casefold(), p)

    out: dict[str, list[dict]] = {}

    def add(path: str, kind: str, via: str) -> None:
        reasons = out.setdefault(path, [])
        if not any(r["kind"] == kind for r in reasons):
            reasons.append({"kind": kind, "via": via})

    for title in ws.get("notes", []):
        cand = title if title.endswith(".md") else title + ".md"
        path = cand if cand in material else by_stem.get(Path(title).stem.casefold())
        if path:
            add(path, "listed", title)
    for folder in ws.get("folders", []):
        for p in material:
            if in_folder(p, folder):
                add(p, "folder", folder)
    for line in ws.get("where", []):
        for p, d in material.items():
            if _where_match(d, line):
                add(p, "where", line)
    # One hop over `sources:` (point 2): a concept nucleated from a member is
    # a member. Sources are recorded by basename, so compare stems.
    stems = {Path(p).stem.casefold(): p for p in out}
    for p, d in material.items():
        for src in _as_list(d.get("sources")):
            if Path(src).stem.casefold() in stems:
                add(p, "derived", Path(src).stem)
                break
    return out


def dead_lines(ws: dict) -> dict[str, list[str]]:
    """Lines that point at nothing: a listed title no note carries, a folder
    no longer on disk (renamed outside Silica, ADR-0112 point 1). Named on
    the workspace's row rather than shown as fewer notes."""
    vault = vault_root()
    fms = note_index.frontmatters(vault)
    stems = {Path(p).stem.casefold() for p in fms}
    notes = [t for t in ws.get("notes", [])
             if (t if t.endswith(".md") else t + ".md") not in fms and Path(t).stem.casefold() not in stems]
    # A line that leaves the vault is dead whatever is there: is_dir() on it
    # would tell a hosted tenant which directories the server has.
    root = vault.resolve()
    folders = [f for f in ws.get("folders", [])
               if not (vault / f).resolve().is_relative_to(root) or not (vault / f).is_dir()]
    return {"notes": notes, "folders": folders}


def summary(ws: dict) -> dict:
    """What the drawer shows: the members grouped by folder, each with its
    reasons, and the dead lines."""
    m = members(ws)
    return {
        "path": ws["path"], "name": ws["name"], "count": len(m),
        "notes": ws["notes"], "folders": ws["folders"], "where": ws["where"],
        "has_instructions": bool(ws.get("instructions")),
        "members": [{"path": p, "reasons": r} for p, r in sorted(m.items())],
        "dead": dead_lines(ws),
    }


def membership_of(path: str, is_folder: bool = False) -> list[dict]:
    """For the drawer's menu: every workspace, and whether `path` is a listed
    line of it or a member for another reason (shown checked and disabled)."""
    out = []
    for w in list_workspaces():
        if is_folder:
            key = path.strip("/")
            listed = key in w["folders"]
            via = next((f for f in w["folders"] if key.startswith(f + "/")), "")
            reasons = [{"kind": "folder", "via": via}] if via else []
        else:
            m = members(w)
            reasons = [r for r in m.get(path, []) if r["kind"] != "listed"]
            # Against the lines, not the members: a plan or a workspace note
            # listed by hand is never a member, and its line must still show
            # checked so it can be taken out again.
            keys = {_note_key(t) for t in w["notes"]}
            listed = _note_key(path) in keys or _note_key(Path(path).stem) in keys
        out.append({"path": w["path"], "name": w["name"], "listed": listed, "reasons": reasons})
    return out


def _note_key(title: str) -> str:
    return title_of(title).removesuffix(".md").casefold()


def _link_name(value: str) -> str:
    """How a note is listed: its name, as Obsidian links it, unless another
    note shares that name, then its path without `.md` so the link names one
    note (Obsidian's "shortest path when possible")."""
    target = title_of(value).removesuffix(".md")
    stem = Path(target).stem
    if "/" not in target:
        return target
    clash = sum(1 for p in note_index.frontmatters(vault_root()) if Path(p).stem.casefold() == stem.casefold())
    return stem if clash <= 1 else target

# One writer at a time: add_line/remove_line read the note and write the
# whole line back, so two edits at once (the drawer, a turn's write, an
# import) kept only the last: 24 concurrent adds kept 1 (measured 2026-10-15).
_EDIT_LOCK = threading.Lock()


def _set_line(ws_path: str, key: str, values: list[str]) -> None:
    from silica.driver import DRIVER

    DRIVER.set_prop(ws_path, key, values, "list")


def add_line(ws_path: str, key: str, value: str) -> bool:
    """Add one line to `notes:`, `folders:` or `where:`. A note goes in as a
    wikilink so a rename in Silica or Obsidian rewrites it. False when the
    line was already there."""
    if key not in LINES:
        raise ValueError(f"unknown workspace line {key!r}")
    with _EDIT_LOCK:
        return _add_line(ws_path, key, value)


def _add_line(ws_path: str, key: str, value: str) -> bool:
    ws = read_workspace(ws_path)
    value = value.strip()
    if key == "notes":
        value = _link_name(value)
        if _note_key(value) in {_note_key(t) for t in ws["notes"]}:
            return False
        _set_line(ws_path, key, [f"[[{t}]]" for t in ws["notes"] + [value]])
        return True
    if key == "folders":
        value = value.replace("\\", "/").strip("/")
    current = ws[key]
    if value in current:
        return False
    _set_line(ws_path, key, current + [value])
    return True


def remove_line(ws_path: str, key: str, value: str) -> bool:
    """Remove one line; never the file it names (leaving deletes nothing)."""
    if key not in LINES:
        raise ValueError(f"unknown workspace line {key!r}")
    with _EDIT_LOCK:
        return _remove_line(ws_path, key, value)


def _remove_line(ws_path: str, key: str, value: str) -> bool:
    ws = read_workspace(ws_path)
    if key == "notes":
        want = {_note_key(title_of(value)), _note_key(_link_name(value))}
        kept = [t for t in ws["notes"] if _note_key(t) not in want]
        if len(kept) == len(ws["notes"]):
            return False
        _set_line(ws_path, key, [f"[[{t}]]" for t in kept])
        return True
    value = value.replace("\\", "/").strip("/") if key == "folders" else value.strip()
    kept = [v for v in ws[key] if v != value]
    if len(kept) == len(ws[key]):
        return False
    _set_line(ws_path, key, kept)
    return True




def create_workspace(name: str, notes=(), folders=()) -> str:
    """A new workspace note under `Workspaces/` inside the write boundary, as
    plans are written under `Plans/`. Returns its vault path."""
    from silica.driver import DRIVER
    from silica.kernel.base.manifest import in_write_dir
    from silica.kernel.base import frontmatter

    name = " ".join(str(name).split())
    # a leading dot is a hidden file the walk never lists: a workspace no one sees
    if not name or NAME_UNSAFE_RE.search(name) or name.startswith("."):
        raise ValueError("a workspace name cannot be empty, start with a dot or hold / \\ : * ? \" < > | # ^ [ ]")
    if find_workspace(name):
        raise ValueError(f"a workspace named {name!r} already exists")
    path = f"{in_write_dir(DEFAULT_FOLDER)}/{name}.md"
    data = {
        "type": WORKSPACE,
        "notes": [f"[[{title_of(t).removesuffix('.md')}]]" for t in notes],
        "folders": [str(f).replace("\\", "/").strip("/") for f in folders],
        "where": [],
    }
    DRIVER.create(path, frontmatter.dump(data, f"# {name}\n"))
    return path


# --- the scope of a chat turn (point 4) ----------------------------------------

# Set by the web turn before run_agent and read by the retrieval tools, as
# user_memory's _TURN is: "the gates are properties of the turn" (ADR-0076).
# The members are frozen for the turn; a note the turn writes joins the
# workspace's lines and the next turn's scope.
_SCOPE: ContextVar[dict | None] = ContextVar("silica_workspace_scope", default=None)


def open_scope(ws: dict):
    """Narrow this turn's retrieval to `ws`'s members; returns the token for
    `close_scope`."""
    paths = frozenset(members(ws))
    return _SCOPE.set({"path": ws["path"], "name": ws["name"], "paths": paths,
                       "keys": frozenset(store_key(p) for p in paths)})


def close_scope(token) -> None:
    _SCOPE.reset(token)


def active_scope() -> dict | None:
    return _SCOPE.get()


def allow_keys() -> frozenset[str] | None:
    """The store keys a scoped turn may retrieve, None when the turn is not
    scoped (every caller then behaves exactly as before)."""
    s = _SCOPE.get()
    return s["keys"] if s else None


def in_scope(path: str) -> bool:
    s = _SCOPE.get()
    return s is None or store_key(path) in s["keys"]


def instructions_message(ws: dict, count: int) -> str:
    """The system line a chat opened in `ws` carries (point 4, Open 3): what
    the scope does, then the body of the workspace note as the user wrote it."""
    head = (f'This conversation is opened in the workspace "{ws["name"]}" ({count} notes). '
            "Searches return only its notes; a note the user names with @ or by path is read "
            "wherever it is. A note you write here joins the workspace. The vault map above "
            "describes the whole vault: do not open a note it names outside this workspace "
            "unless the user asks for it.")
    body = (ws.get("instructions") or "").strip()
    if not body:
        return head
    return head + "\n\nThe user's standing instructions for this workspace, in their words:\n\n" + body
