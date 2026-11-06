# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The workspaces panel (ADR-0112)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from silica.ui.web.failures import explain as _failure
from silica.ui.web.session import SESSION

router = APIRouter()


# --- workspaces (ADR-0112) --------------------------------------------------------

@router.get("/workspaces")
def workspaces_list():
    """Every workspace as the drawer shows it, and the one this chat is
    opened in."""
    from silica.kernel import workspaces

    try:
        rows = [workspaces.summary(w) for w in workspaces.list_workspaces()]
    except Exception as exc:
        return _failure(exc)
    return {"workspaces": rows, "current": SESSION.chat.workspace}


@router.post("/workspaces")
def workspaces_create(payload: dict):
    """A new workspace note, optionally holding the note or folder its menu
    was opened on."""
    from silica.kernel import workspaces

    notes, folders = payload.get("notes") or [], payload.get("folders") or []
    if not all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in (notes, folders)):
        raise HTTPException(status_code=400, detail="notes and folders must be lists of names")
    try:
        path = workspaces.create_workspace(str(payload.get("name") or ""), notes=notes, folders=folders)
    except ValueError as exc:
        # create_workspace's own sentences (name refused or taken), never a path
        raise HTTPException(status_code=400, detail=exc.args[0]) from None
    except FileExistsError:
        raise HTTPException(status_code=409, detail="a note with that name is already there") from None
    except OSError:
        raise HTTPException(status_code=400, detail="that name cannot be a file name here") from None
    return workspaces.summary(workspaces.read_workspace(path))


@router.post("/workspaces/line")
def workspaces_line(payload: dict):
    """Add or remove one line of a workspace: the user's act from the
    drawer's menu. Removing a line never touches the note or folder it names."""
    from silica.kernel import workspaces

    ws = workspaces.find_workspace(str(payload.get("workspace") or ""))
    if ws is None:
        raise HTTPException(status_code=404, detail="no such workspace")
    key, value = str(payload.get("key") or ""), str(payload.get("value") or "")
    on = payload.get("on", True)
    if key not in workspaces.LINES or not value.strip() or not isinstance(on, bool):
        raise HTTPException(status_code=400, detail="key must be notes, folders or where, with a value")
    try:
        if on:
            workspaces.add_line(ws["path"], key, value)
        else:
            workspaces.remove_line(ws["path"], key, value)
    except (OSError, RuntimeError) as exc:
        # not 200: the menu checks r.ok and would show the line as saved
        return JSONResponse(_failure(exc), status_code=500)
    return workspaces.summary(workspaces.read_workspace(ws["path"]))


@router.get("/workspaces/membership")
def workspaces_membership(path: str, folder: bool = False):
    """For the drawer's menu on one note or folder: every workspace, whether
    the row is a listed line of it, and any other reason it is a member."""
    from silica.kernel import workspaces

    return {"path": path, "folder": folder, "workspaces": workspaces.membership_of(path, is_folder=folder)}
