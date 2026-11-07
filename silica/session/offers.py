# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What a turn offers around its answer: study plans, the escalated aid, proposed skills.

Each is an extra, so each costs the offer and never the answer. `chat` is the
conversation's id, which the study ledger keys on.
"""
from __future__ import annotations

import logging

from silica.agent.recall_watch import read_refs
from silica.config import CONFIG, demo

logger = logging.getLogger(__name__)


def skill_after_turn(text: str, messages: list[dict], reserved) -> tuple[dict | None, dict | None]:
    """(turn note, offer) for the turn that just ended (ADR-0127 points 5, 6):
    a proposal written from a request the person made in enough chats, or
    else the installed skill that answers it, offered as the next turn. An
    incognito chat proposes nothing: it asked to leave no trace. `reserved`
    names the commands a proposal may not shadow."""
    from silica.obs import narration as _narr
    from silica.kernel import proposed_skills as ps
    from silica.kernel import user_memory

    from silica.kernel.learn.offer import says_lost

    vault = CONFIG.vault_path or ""
    # "non ho capito la Lezione 14" said in three chats is a reader stuck, not
    # a request to make reusable: it became the skill /non-ho-capito
    # (2026-10-23). The study offer answers that message. A demo neither
    # drafts one (a worker call no question counts) nor offers one it may
    # refuse, /explain among them (ADR-0139 point 6).
    if not vault or demo() or user_memory.incognito() or says_lost(text):
        return None, None
    try:
        evidence = ps.detect(text, messages, vault, _narr.NARRATOR.sid)
        if evidence is not None:
            drafted = ps.draft(evidence["asks"], CONFIG)
            name = ps.stage(vault, drafted, evidence, CONFIG.worker_model or CONFIG.model or "",
                            reserved=reserved) if drafted else None
            if name:
                return {"kind": "skill", "text": f"/{name} written from a request you made in "
                                                 f"{len(evidence['sessions'])} chats · review it in "
                                                 "Settings › Skills"}, None
        return None, ps.offer(text, vault)
    except Exception:
        # A proposal is an extra: a chat file that does not parse or a worker
        # that is down costs the person the proposal, never the answer.
        logger.warning("skill proposal skipped", exc_info=True)
        return None, None


def study_offer(text: str, messages: list[dict], chat: str, plan_draft: dict | None = None) -> dict | None:
    """The study offer for the turn that just ended, from the notes it read
    (learn.offer.decide). An incognito chat gets none, as it gets no proposal,
    and a demo none: its commands are Learn's and the demo's refused ones.
    `plan_draft` is the plan this turn computed (plan_context), offered for
    saving."""
    from silica.kernel import user_memory
    from silica.kernel.learn.offer import decide

    if demo() or user_memory.incognito():
        return None
    try:
        return decide(text, read_refs(messages), chat, plan_draft)
    except Exception:
        # An offer is an extra: a ledger that does not parse costs the offer,
        # never the answer.
        logger.warning("study offer skipped", exc_info=True)
        return None


def escalation(text: str, messages: list[dict], chat: str) -> str:
    """The escalation block for this turn, or "" (learn.offer.decide_escalation).
    Nothing in an incognito chat."""
    from silica.kernel import user_memory
    from silica.kernel.learn.offer import decide_escalation

    if user_memory.incognito():
        return ""
    try:
        history = [str(m.get("content") or "") for m in messages if m.get("role") == "user"][-3:]
        return decide_escalation(text, history, chat)
    except Exception:
        # An escalation is an extra: a ledger that does not parse costs it,
        # never the turn.
        logger.warning("study escalation skipped", exc_info=True)
        return ""


def plan_context(text: str, messages: list[dict]) -> tuple[str, str, dict | None]:
    """Learn's plan for this message (learn.offer.plan_context), or ("", "",
    None): the block for the model, the plan or calendar appended under its
    answer, and the plan to save when this turn computed one. `messages` is
    the chat before this turn, whose last exchange may be the question for
    the material that this message answers. An incognito chat gets none, as
    it gets no offer, and an account Memorize is not served to none: the
    plan it would offer to save lands on a route that answers 404."""
    from silica.config import withheld
    from silica.kernel import user_memory

    if "/learn" in withheld("routes") or user_memory.incognito() or not text or text.startswith("/"):
        return "", "", None
    try:
        from silica.kernel.learn import offer
        from silica.kernel.learn.plans import list_plans

        prev_user, prev_answer = "", ""
        for m in reversed(messages):
            if m.get("role") == "assistant" and not prev_answer and not prev_user:
                prev_answer = str(m.get("content") or "")
            elif m.get("role") == "user":
                # the person's words: the blocks this server appended to the
                # turn start at a blank line and a bracket, and the question
                # for the material said "study plan" itself
                prev_user = str(m.get("silica_said") or m.get("content") or "").split("\n\n[", 1)[0]
                break
        return offer.plan_context(text, list_plans(), prev_user, prev_answer)
    except Exception:
        # The plan's numbers are an extra: a plan note that does not parse,
        # or a reading model that is down, costs the block, never the turn.
        logger.warning("plan context skipped", exc_info=True)
        return "", "", None
