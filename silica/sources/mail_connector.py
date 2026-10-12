# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The Mail row of Settings › Connectors (ADR-0090 "GUI and TUI"; the shell
and its block API are ADR-0089 point 8's, in silica/connectors).

Part A only: the row imports a file the GUI uploaded into the inbox
(`POST /mail/upload`), a dry run first (`plan`), then `import` in one undo
run, through the same `mail.plan` and `mail.apply` the /mail command uses;
`undo` takes the newest import back by its run id.
Part B (sync) shows nothing until its prerequisites land.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

DETAIL = "Keep chosen mail threads as notes"


def _inside_vault(path: str) -> Path:
    """The uploaded export, by its vault-relative path. The panel names a file
    the upload route put in the inbox; a path that leaves the vault, or one
    into Silica's own folder, is a request this row has no reason to serve."""
    from silica.config import CONFIG, in_config_dir

    vault = Path(CONFIG.vault_path or ".").resolve()
    p = (vault / path).resolve()
    if not path or Path(path).is_absolute() or not p.is_relative_to(vault) or in_config_dir(p):
        raise ValueError(f"{path or 'no file'}: the Mail row imports a file inside the vault")
    return p


def _rows(p) -> dict[str, Any]:
    from silica.sources import mail

    return {
        "file": p.file.name, "takeout": p.takeout, "me": list(p.me), "guessed": p.guessed,
        "in_file": p.in_file, "took_part": p.took_part, "added_by_all": p.added_by_all,
        "present": p.present, "grown": p.grown, "over_limit": p.over_limit,
        "threads": [{"date": t.mails[0].date.date().isoformat() if t.mails[0].date else "",
                     "subject": t.mails[0].subject, "messages": len(t.mails),
                     "path": mail.note_path(t)} for t in p.write],
    }


def _last_undoable(state: dict) -> dict[str, Any] | None:
    """The newest import the journal can still take back, as the row offers
    it: one run at a time, newest first, the way /revert peels runs. By its
    run id and not /revert's "latest run", which after a chat write since the
    import is that write (2026-10-12)."""
    from silica.kernel.write.undo_journal import get_undo_journal

    journal = get_undo_journal()
    for imp in reversed(state.get("imports") or []):
        run_id = str(imp.get("run_id") or "")
        info = journal.run_info(run_id) if run_id and imp.get("written") else None
        if info is not None and info.get("reverted_at") is None:
            return {"run_id": run_id, "file": str(imp.get("file") or ""),
                    "at": str(imp.get("at") or ""), "written": int(imp.get("written") or 0)}
    return None


class MailBlock:
    name = "mail"

    def status(self) -> dict[str, Any]:
        from silica.sources import mail

        row = {"name": self.name, "title": "Mail", "detail": DETAIL,
               "actions": ["plan", "import", "undo"]}
        try:
            state = mail.load_state()
        except (OSError, ValueError) as exc:
            return {**row, "state": "failed", "account": "", "detail": str(exc)}
        return {**row, "state": "local-only", "account": ", ".join(state.get("me") or []),
                "imported": len(mail.imported_threads()),
                "imports": len(state.get("imports") or []),
                "last": _last_undoable(state)}

    def _undo(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Take one import back by its run id. Only a run this vault's record
        names as a mail import: the row has no business reverting any other."""
        from silica.kernel.write.undo_journal import revert_run
        from silica.sources import mail

        run_id = str(payload.get("run_id") or "")
        try:
            runs = {str(i.get("run_id")) for i in mail.load_state().get("imports") or []}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        if not run_id or run_id not in runs:
            return {"ok": False, "error": "that is not a mail import of this vault"}
        res = revert_run(run_id)
        return {"ok": True, "removed": len(res["reverted"]), "kept": len(res["skipped"]),
                "gone": len(res["stale"]),
                "errors": [f"{e['path']}: {e['error']}" for e in res["errors"]]}

    def act(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        from silica.sources import mail

        if action == "undo":
            return self._undo(payload)
        if action not in ("plan", "import"):
            return {"ok": False, "error": "unknown action"}
        try:
            src = _inside_vault(str(payload.get("path") or ""))
            me = tuple(a for a in str(payload.get("me") or "").split(",") if a.strip())
            if me:
                me = mail.remember_addresses(me)  # typed in the row: the confirmation point 4 asks
            p = mail.plan(src, me=me or None, label=str(payload.get("label") or ""),
                          all_=bool(payload.get("all")),
                          limit=mail.parse_limit(payload.get("limit")))
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        if action == "plan":
            return {"ok": True, **_rows(p)}
        if p.guessed and not p.takeout:
            return {"ok": False, "error": "Confirm your address first: type it in the address field."}
        out = mail.apply(p)
        done = {"ok": True, "written": out["written"], "run_id": out["run_id"]}
        if out["failed"]:
            done["failed"] = [{"subject": s, "error": e} for s, e in out["failed"]]
        return done


MAIL_BLOCK = MailBlock()
