# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""How often ADR-0127's two rules would have fired on the chats already in
~/.silica/narration: a proposal (the same kind of request in N_SESSIONS chats,
none undone) and an offer (an installed skill that covers the request).

No model is called. Each vault's chats are replayed in the order they began,
each request seeing only the chats before its own, and a kind of request is
proposed once, as `_known` holds it after a proposal. Usage:

    uv run python evals/proposal_rate.py [--skills DIR ...]

`--skills` adds folders of SKILL.md files to the built-in ones, as if they
were installed in every vault (the offer rate a set of skills would have had).
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from silica.kernel import proposed_skills as ps


def _chats():
    """vault -> [(start, sid, user messages)], from the narration files."""
    from silica.agent import narration

    out = defaultdict(list)
    for f in sorted(narration.narration_dir().glob("*.jsonl")):
        try:
            head = json.loads(f.open(encoding="utf-8").readline())
        except (OSError, ValueError):
            continue   # an empty or damaged file is no chat
        vault = (head.get("payload") or {}).get("vault") or ""
        if head.get("kind") != "session" or (head.get("payload") or {}).get("driver") not in ("gui", "tui"):
            continue   # probes and benches are not a person's chats
        msgs = narration.load_session_messages(f.stem, vault) or []
        out[vault].append((head.get("ts", 0.0), f.stem, msgs))
    return out


def _skills(extra: list[str]):
    from silica_core.kernel.skills import discover, load

    found = list(discover(None).values())
    for d in extra:
        found += [s for p in sorted(Path(d).glob("*/SKILL.md")) if (s := load(p, "vault"))]
    return [ps._skill_words(s) for s in found], [s.name for s in found]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skills", action="append", default=[])
    args = ap.parse_args()
    skill_words, names = _skills(args.skills)
    requests = proposals = offers = 0
    offered: dict[str, int] = defaultdict(int)
    for vault, chats in _chats().items():
        chats.sort()
        titles = ps._titles(vault) if vault else {}
        seen: list[tuple[str, list[str], bool]] = []
        asked: list[frozenset] = []
        for _ts, sid, msgs in chats:
            undid = ps._undid(msgs)
            for a in ps._asks(msgs):
                requests += 1
                w = ps.words(a)
                if len(w) < ps.MIN_SHARED:
                    continue
                hit = next((n for sw, n in zip(skill_words, names) if ps.covers(sw, w)), None)
                if hit:
                    offers += 1
                    offered[hit] += 1
                    continue
                if undid or any(ps.same_kind(w, k, titles) for k in asked):
                    continue
                found = {s for s, asks, u in seen
                         if not u and any(ps.same_kind(w, ps.words(x), titles) for x in asks)}
                if len(found) + 1 >= ps.N_SESSIONS:
                    proposals += 1
                    asked.append(w)
                    print(f"proposal in {sid}: {a[:90]!r}")
            seen.append((sid, ps._asks(msgs), undid))
    print(f"requests {requests} · proposals {proposals} · offers {offers} {dict(offered)}")


if __name__ == "__main__":
    main()
