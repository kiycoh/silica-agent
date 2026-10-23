# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Settings › Skills (ADR-0097 point 4): the skills `/` can run, listed by the
root that holds them, and one SKILL.md installed into this vault after a preview.

The vault's root is the only one written: on the hosted pilot it is the only
root inside the account's directory (ADR-0086). `~/.silica/skills/` is listed
and stays a folder managed by hand. The checks are the loader's own
(silica_core.kernel.skills), run before a byte is written, so a file that
installs is a file `/` can run.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from silica.config import CONFIG

# The whole body is expanded into the turn a /<name> opens. The largest
# SKILL.md shipped on 2026-10-13 is 7,319 bytes (silica-core's `silica`), so
# this is four times the largest real one.
CAP = 32 * 1024


def _require_same_origin(request: Request) -> None:
    from silica.ui.web.server import _require_same_origin as check

    check(request)


router = APIRouter(prefix="/skills")
_SAME_ORIGIN = [Depends(_require_same_origin)]


def _vault() -> Path:
    if not CONFIG.vault_path:
        raise HTTPException(409, "no vault is open, and a skill is installed into one")
    return Path(CONFIG.vault_path)


def _skills_dir(vault: Path) -> Path:
    return vault / ".silica" / "skills"


def _inside(vault: Path, path: Path) -> Path:
    """`path`, refused when a link in its chain leads out of the vault.
    resolve() follows the links that exist and keeps the rest literal, so the
    check holds before mkdir creates anything."""
    if not path.resolve().is_relative_to(vault.resolve()):
        raise HTTPException(400, "the vault's .silica/skills leads outside the vault; not writing through it")
    return path


def _commands() -> set[str]:
    """The names a Python command answers: dispatch tries those before any skill."""
    from silica.ui.commands import COMMANDS

    return {c.name.lstrip("/") for c in COMMANDS}


def _first_line(text: str) -> str:
    return (text.splitlines() or [""])[0]


@router.get("")
def list_skills():
    """Every skill in every root, with what it does today: active, replaced
    by a nearer root, or inactive because a Python command has its name."""
    from silica.kernel.personas import is_persona
    from silica_core.kernel.skills import SKILLS, discover, load, roots

    vault = CONFIG.vault_path or None
    live = discover(vault)
    taken = _commands()
    groups: dict[str, list[dict]] = {"vault": [], "home": [], "builtin": []}
    shadowed: dict[str, list[str]] = {}
    for source, root in roots(vault):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/SKILL.md")):
            if root == SKILLS and path.parent.name == "silica":
                continue   # as discover(): it teaches another agent these tools
            skill = load(path, source)
            if skill is None:
                continue   # the loader logged why, once
            row = {"name": skill.name, "description": _first_line(skill.description),
                   "hint": skill.argument_hint, "source": source, "state": "active",
                   "persona": is_persona(skill)}
            winner = live.get(skill.name)
            if skill.name in taken:
                row["state"] = "inactive"
            elif winner is not None and winner.path != skill.path:
                row["state"], row["by"] = "replaced", winner.source
                shadowed.setdefault(skill.name, []).append(source)
            groups[source].append(row)
    for rows in groups.values():
        for row in rows:
            if row["state"] == "active" and row["name"] in shadowed:
                row["shadows"] = shadowed[row["name"]]
    from silica.kernel.proposed_skills import pending

    return {"vault": bool(vault), "proposed": pending(vault) if vault else [], "groups": [
        {"source": "vault", "dir": ".silica/skills/", "skills": groups["vault"]},
        {"source": "home", "dir": "~/.silica/skills/", "skills": groups["home"]},
        {"source": "builtin", "dir": "", "skills": groups["builtin"]},
    ]}


def _parse(text: str):
    """The skill `text` holds, the body the model receives, and whether it is
    a persona (ADR-0127), or a 400 that names the check it failed and what to
    change."""
    # _NAME and _COMMENT are the loader's own rules, imported rather than
    # copied so the refusal here and the loader's never disagree.
    from silica.kernel.personas import is_persona
    from silica_core.kernel.skills import _COMMENT, _NAME, load
    from silica_core.kernel.write.frontmatter import split

    data, raw, _body = split(text)
    if raw is None:
        raise HTTPException(400, "This file has no frontmatter. A skill opens with a --- block "
                                 "holding name: and description:, then the instructions.")
    if data is None:
        raise HTTPException(400, "The frontmatter between the --- lines is not valid YAML.")
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise HTTPException(400, "The frontmatter has no name:. The name is what you type after /, "
                                 "and it names the folder the skill goes into.")
    if len(name) > 64 or not _NAME.fullmatch(name):
        raise HTTPException(400, f"{name!r} is not a skill name: lowercase letters, digits and "
                                 "single hyphens, as in exam-prep.")
    if name in _commands():
        raise HTTPException(400, f"/{name} is a built-in command, and built-in commands answer before "
                                 "skills, so this skill would never run. Change name: in the file to "
                                 "another name.")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name / "SKILL.md"
        path.parent.mkdir()
        path.write_text(text, encoding="utf-8")
        skill = load(path, "vault")
        persona = skill is not None and is_persona(skill)   # read while the file is there
    if skill is None:
        raise HTTPException(400, "The skill loader refused this file; the server log says why.")
    return skill, _COMMENT.sub("", skill.body).strip(), persona


def _write(vault: Path, dest: Path, data: bytes) -> None:
    """Whole or not at all: a temp file beside `dest`, then one rename."""
    _inside(vault, dest.parent)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".SKILL-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@router.post("", dependencies=_SAME_ORIGIN)
async def install(file: UploadFile = File(...), confirm: bool = Form(False)):
    """`confirm` false answers the preview and writes nothing; true writes it."""
    from silica_core.kernel.skills import discover

    vault = _vault()
    data = await file.read(CAP + 1)
    if len(data) > CAP:
        raise HTTPException(413, "This file is over 32 KiB. A skill may be at most 32 KiB, "
                                 "because its whole body goes into the turn you open with it.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "This file is not UTF-8 text, and a SKILL.md is Markdown.") from None
    skill, body, persona = _parse(text)
    dest = _skills_dir(vault) / skill.name / "SKILL.md"
    live = discover(str(vault)).get(skill.name)
    preview = {
        "name": skill.name,
        "description": skill.description,
        "hint": skill.argument_hint,
        "persona": persona,
        "body": body,
        "bytes": len(data),
        "cap": CAP,
        "path": f".silica/skills/{skill.name}/SKILL.md",
        "replaces": dest.is_file(),
        "shadows": live.source if live is not None and live.source != "vault" else "",
        "stripped": body != skill.body.strip(),
    }
    if not confirm:
        return {"ok": True, "installed": False, "skill": preview}
    _write(vault, dest, data)
    return {"ok": True, "installed": True, "skill": preview}


@router.post("/proposed/{name}/install", dependencies=_SAME_ORIGIN)
def install_proposed(name: str):
    """A proposed skill (ADR-0127 point 5) becomes a vault skill: the person's
    act, the only way one ever runs."""
    from silica.kernel.proposed_skills import install as move

    vault = _vault()
    _inside(vault, _skills_dir(vault))
    if (err := move(str(vault), name)) is not None:
        raise HTTPException(409 if "exists" in err else 404, err)
    return {"ok": True, "name": name}


@router.post("/proposed/{name}/dismiss", dependencies=_SAME_ORIGIN)
def dismiss_proposed(name: str):
    """Not this one: its text goes, its requests stay so it is not proposed again."""
    from silica.kernel.proposed_skills import dismiss

    if (err := dismiss(str(_vault()), name)) is not None:
        raise HTTPException(404, err)
    return {"ok": True}


@router.delete("/{name}", dependencies=_SAME_ORIGIN)
def remove(name: str):
    """A vault skill's SKILL.md, and its folder when nothing else is in it: a
    folder placed by hand can hold files of its author's that are not ours to
    delete."""
    from silica_core.kernel.skills import _NAME

    vault = _vault()
    folder = _skills_dir(vault) / name
    if len(name) > 64 or not _NAME.fullmatch(name) or not (folder / "SKILL.md").is_file():
        raise HTTPException(404, f"no vault skill is named {name}")
    _inside(vault, folder)
    (folder / "SKILL.md").unlink()
    kept = sorted(p.name for p in folder.iterdir())
    if not kept:
        folder.rmdir()
    return {"ok": True, "kept": kept}
