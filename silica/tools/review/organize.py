# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""/organize's dry run, waiting: one move per note the taxonomy places elsewhere.

The classifier (and its model arbiter, for the notes it cannot place) ran when
the dry run did; a yes is the move alone, graph-safe as /organize --apply's,
each journalled so the batch's revert moves it back."""
from __future__ import annotations

from silica.kernel.review import Kind, Proposal, offer, offered, proposal_id, register
from silica.kernel.text.note_parts import clean_name


def offer_moves(vault: str, plan: list[dict]) -> None:
    """The dry run's moves, `{from, to, confidence, evidence}` each, in place
    of the last dry run's."""
    if not vault:
        return
    offer(vault, "move", [Proposal(
        id=proposal_id("move", m["from"], m["to"]), kind="move",
        title=f"Move {clean_name(m['from'])} to {m['to'].rsplit('/', 1)[0] or 'the vault root'}",
        why=f"the taxonomy places it there ({m.get('evidence') or 'rules'})",
        notes=(m["from"],), preview=f"{m['from']}\n→ {m['to']}",
        data={"from": m["from"], "to": m["to"]})
        for m in plan if m.get("from") and m.get("to")])


def _apply(vault: str, props: list[Proposal]) -> dict[str, str | None]:
    from silica.kernel.write import gate

    out: dict[str, str | None] = {}
    ready = []
    # Each move checked alone, so one taken destination refuses that note and
    # not the batch, as move_preflight would refuse the whole set.
    for p in props:
        why = gate.move_preflight([(p.data["from"], p.data["to"])])
        if why:
            out[p.id] = f"not moved: {why}"
        else:
            ready.append(p)
    if not ready:
        return out
    res = gate.move_notes_with_restore_point([(p.data["from"], p.data["to"]) for p in ready],
                                             "review", run=gate.current_run() or "")
    moved = {src for src, _dst in res["moved"]}
    for p in ready:
        out[p.id] = None if p.data["from"] in moved else (res.get("error") or "not moved")
    return out


register(Kind("move", "Moves to a folder", lambda vault: offered(vault, "move"), _apply))
