# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Other names for a note: /aliases' gated groups, one proposal per note.

The model proposed them on /aliases' dry run, and the ambiguity gate kept them;
a yes writes them through the same write_aliases --apply uses."""
from __future__ import annotations

from silica.kernel.review import Kind, Proposal, offered, offer, proposal_id, register


def offer_groups(vault: str, groups: dict[str, list[str]], path_by_title: dict[str, str]) -> None:
    if not vault:
        return
    rows = []
    for canonical, variants in sorted(groups.items()):
        path = path_by_title.get(canonical.lower())
        if not path:
            continue  # write_aliases would skip it too: no note carries the title
        rows.append(Proposal(
            id=proposal_id("alias", path, *sorted(variants)), kind="alias",
            title=f"{canonical}: also {', '.join(variants)}",
            why="other names the model proposed and no other note claims",
            notes=(path,), preview="aliases: + " + ", ".join(variants),
            data={"canonical": canonical, "path": path, "variants": list(variants)}))
    offer(vault, "alias", rows)


def _apply(vault: str, props: list[Proposal]) -> dict[str, str | None]:
    from silica.tools.aliases import write_aliases

    groups = {p.data["canonical"]: p.data["variants"] for p in props}
    paths = {p.data["canonical"].lower(): p.data["path"] for p in props}
    _written, skipped = write_aliases(groups, paths)
    why = {s["note"]: s["reason"] for s in skipped}
    # A group already declared writes nothing and is skipped nowhere: it is in
    # place, which is what the yes asked for.
    return {p.id: why.get(p.data["canonical"]) for p in props}


register(Kind("alias", "Other names", lambda vault: offered(vault, "alias"), _apply))
