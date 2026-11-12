# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A deep research (ADR-0158 point 8): the person's question, a plan they
approve, one capped web-research loop per sub-question run as a job, and one
note in the Inbox with a part per sub-question.

- **Plan.** One tool-less call writes the sub-questions (and, for a literature
  research, the protocol) from the shape, the question and the titles of the
  vault's nearest notes, marking a sub-question the vault already answers. The
  person edits and approves it in the page; nothing is fetched before that,
  and the approval is the spend gate, not the admission (the Inbox to Vault
  step stays /nucleate's).
- **Acquire.** The code starts one `web_research` loop per approved
  sub-question, one after another, each with a search state of its own
  (`own_turn`), so a chat question asked meanwhile cannot clear the job's
  bank. The plan holds: topics met on the way come back as a list at the end
  and are not pursued in the same run.
- **Deliver.** Each loop writes its part, a findings note whose citations name
  the quote they stand on (point 9), into a folder of the Inbox; the parent
  note beside the folder lists the parts and is flagged `document: true`, so
  /nucleate skips it (ADR-0129 point 5). The finished job offers to nucleate
  the parts the person picks.

The model never decides how many loops run or how long: the approved plan and
the caps below do (point 1).
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from silica.config import CONFIG
from silica.kernel import job_records

logger = logging.getLogger(__name__)

JOB_KIND = "research"
# The narration stream forwards it to the browser as `event: research`.
TOPIC = "research/update"
INTERRUPTED = "the server restarted while this research was running"

# The four shapes of point 10. Each one changes the plan's instructions; the
# loop under every sub-question is the same.
SHAPES = ("literature", "investigation", "anchored", "cluster")

# Depth: how many sub-questions a plan may hold and how many steps each loop
# may spend.
# shortcut: the per-loop caps are ADR-0158 Open 7, which a probe on the hosted
# model is to set; 8, 12 and 20 sit under /web-search's 48 because support
# falls as one trajectory grows (78.6% at 2 tool calls, 16.7% at 150,
# 2605.06635). Replace them with the probe's numbers when Open 7 is answered.
DEPTHS = {
    "quick": {"parts": 4, "searches": 8},
    "standard": {"parts": 8, "searches": 12},
    "deep": {"parts": 12, "searches": 20},
}
QUESTION_CHARS = 2000
PART_CHARS = 300
# A part with fewer banked quotes than this arrives unticked in the chooser:
# it rests on too little to nucleate unread.
THIN_QUOTES = 3

_STOPS: dict[str, threading.Event] = {}


class Refused(ValueError):
    """A research action turned away. The text is the sentence the person reads."""


# --- the plan ------------------------------------------------------------------

_PLAN_SYSTEM = """You plan a deep research for a person's knowledge vault. \
You get the research's shape, the question (or a note to look into), and the \
titles of notes the vault already holds near it.

Reply with JSON only, in this form:
{{"protocol": "...", "subquestions": [{{"text": "...", "held": ""}}]}}

- At most {parts} sub-questions. Each is one question that a single web \
search loop can answer; together they cover the question and none overlaps \
another.
- `held` is the exact title of a vault note from the list that already \
answers that sub-question, or "" when none does. Do not guess a title that is \
not in the list.
- Write the sub-questions and the protocol in the language of the question.
{shape}"""

_SHAPE_RULES = {
    "literature": (
        "- The shape is a literature review. `protocol` comes first, in two or "
        "three sentences: what counts as evidence (study types, reviews, "
        "regulatory assessments), what is left out, and that a preprint is "
        "marked apart from its published version. The sub-questions follow the "
        "parts of the topic and end with one on where the studies disagree."
    ),
    "investigation": (
        "- The shape is an investigation of a contested matter with named "
        "people. `protocol` says which primary documents count (filings, "
        "official records, statements) above reporting, and that every claim "
        "keeps who asserts it and its status (alleged, charged, convicted, "
        "denied, dismissed). Sub-questions follow the actors, the document "
        "sets or the periods, then the chronology. No sub-question presumes "
        "what a court has not found, and none asks about people only "
        "mentioned beside others."
    ),
    "anchored": (
        "- The shape looks deeper into one note, often a transcript. Extract "
        "its checkable claims, each rewritten so it stands on its own; each "
        "sub-question asks what the evidence says about one claim. When the "
        "note has timestamps, start the sub-question with the claim's, like "
        "[04:12]. Leave out the speaker's analogies and opinions. `protocol` "
        "is one sentence on what the note is."
    ),
    "cluster": (
        "- The shape is a cluster of notes on a theme. The sub-questions are "
        "the map of notes the theme needs: each one names the single concept "
        "one note would hold, as a question. `protocol` is one sentence on "
        "the theme's scope."
    ),
}


def _vault_near(text: str, k: int = 12) -> dict[str, str]:
    """{title: path} of the vault notes nearest `text`; {} when no index
    answers, which costs the plan its `held` marks and nothing else."""
    try:
        from silica.kernel.recall.search import facade_search

        hits = facade_search(text, k=k, memory=False).get("results") or []
    except Exception:
        # Tolerated: an index that is cold or an embedder that is down. The
        # plan is still written; it just cannot say what the vault holds.
        logger.debug("deep research: vault search failed", exc_info=True)
        return {}
    return {str(h.get("name") or Path(h["path"]).stem): h["path"] for h in hits if h.get("path")}


def _note_text(note: str) -> str:
    from silica.driver import DRIVER
    from silica.kernel.base import frontmatter

    try:
        _data, _raw, body = frontmatter.split(DRIVER.read_note(note).content or "")
    except Exception:
        raise Refused("that note could not be read") from None
    # ponytail: the first 24,000 characters of the note, a long transcript's
    # first hour or so; reading it whole is point 2's fan-out, due when an
    # anchored research is asked of a note longer than this.
    return (body or "")[:24000]


def plan(shape: str, question: str, *, note: str = "", depth: str = "standard",
         scholarly: bool = False, ask=None) -> dict:
    """The plan the person reviews: shape, question, protocol and the parts,
    each `{"q": sub-question, "held": vault path or ""}`. One model call;
    raises Refused with the person's sentence when it cannot be written."""
    from silica.model.reply import decode_json

    shape = shape if shape in SHAPES else ""
    if not shape:
        raise Refused("pick one of the four shapes")
    question = (question or "").strip()[:QUESTION_CHARS]
    if shape == "anchored":
        if not note:
            raise Refused("pick the note to look into")
        seed = _note_text(note)
        if not seed.strip():
            raise Refused("that note is empty")
    elif not question:
        raise Refused("write the question first")
    caps = DEPTHS.get(depth) or DEPTHS["standard"]
    near = _vault_near(question or Path(note).stem)
    titles = "\n".join(f"- {t}" for t in near) or "(none found)"
    user = f"Question: {question or '(look into the note below)'}\n\nVault notes near it:\n{titles}"
    if shape == "anchored":
        user += f"\n\nThe note, {Path(note).stem}:\n{seed}"
    messages = [
        {"role": "system", "content": _PLAN_SYSTEM.format(parts=caps["parts"], shape=_SHAPE_RULES[shape])},
        {"role": "user", "content": user},
    ]
    raw = (ask or _ask)(messages)
    try:
        data, _clean = decode_json(raw or "")
    except ValueError:
        data = None  # a reply that is not JSON is a plan that could not be written
    subs = data.get("subquestions") if isinstance(data, dict) else None
    if not isinstance(subs, list):
        raise Refused("the plan could not be written: try again")
    parts: list[dict] = []
    for item in subs:
        text = item.get("text") if isinstance(item, dict) else item
        text = re.sub(r"\s+", " ", str(text or "")).strip()[:PART_CHARS]
        if not text or any(p["q"] == text for p in parts):
            continue
        held = str(item.get("held") or "").strip() if isinstance(item, dict) else ""
        parts.append({"q": text, "held": near.get(held, "")})
        if len(parts) == caps["parts"]:
            break
    if not parts:
        raise Refused("the plan came back without sub-questions: try again")
    protocol = str(data.get("protocol") or "").strip()[:1200]
    return {"shape": shape, "question": question or Path(note).stem, "note": note, "depth": depth,
            "scholarly": bool(scholarly), "protocol": protocol, "parts": parts,
            "searches": caps["searches"], "max_parts": caps["parts"]}


def _ask(messages: list[dict]) -> str:
    from silica.model.llm import call_llm

    return call_llm(CONFIG.model, messages).text or ""


# --- the job -------------------------------------------------------------------


def _clean_plan(p: dict) -> dict:
    """The approved plan as the server will run it: the page sent it, so every
    cap is checked again here, where the spend happens."""
    shape = p.get("shape") if p.get("shape") in SHAPES else ""
    if not shape:
        raise Refused("pick one of the four shapes")
    question = re.sub(r"\s+", " ", str(p.get("question") or "")).strip()[:QUESTION_CHARS]
    if not question:
        raise Refused("write the question first")
    depth = str(p.get("depth") or "")
    depth = depth if depth in DEPTHS else "standard"
    caps = DEPTHS[depth]
    parts: list[dict] = []
    for item in p.get("parts") or []:
        if not isinstance(item, dict):
            continue
        q = re.sub(r"\s+", " ", str(item.get("q") or "")).strip()[:PART_CHARS]
        held = str(item.get("held") or "")
        if q:
            parts.append({"q": q, "held": held if held.endswith(".md") and ".." not in held else ""})
    if not parts:
        raise Refused("the plan has no sub-question left")
    if len(parts) > caps["parts"]:
        raise Refused(f"a {depth} research holds at most {caps['parts']} sub-questions")
    return {"shape": shape, "question": question, "note": str(p.get("note") or ""), "depth": depth,
            "scholarly": bool(p.get("scholarly")), "protocol": str(p.get("protocol") or "")[:1200],
            "parts": parts, "searches": caps["searches"]}


def _records() -> list[dict]:
    vault = str(CONFIG.vault_path or "")
    return [r for r in job_records.all_records() if r.get("kind") == JOB_KIND and r.get("vault") == vault]


def public(rec: dict) -> dict:
    """The record as the browser gets it: without the vault's path on disk."""
    return {k: v for k, v in rec.items() if k not in ("vault",)}


def _write(rec: dict) -> None:
    from silica.obs.bus import BUS

    rec["updated"] = time.time()
    job_records.write(rec)
    BUS.publish(TOPIC, public(rec))


def listing() -> list[dict]:
    """This vault's research jobs the person has not dismissed, oldest first."""
    return [public(r) for r in _records() if not r.get("dismissed")]


def read(job_id: str) -> dict | None:
    if not job_records.is_id(job_id):
        return None
    rec = job_records.read(job_records.path_of(job_id))
    return rec if rec and rec.get("kind") == JOB_KIND else None


def start(approved: dict, *, run=None) -> dict:
    """Record the approved plan as a job and run it on a thread of its own.
    One research at a time: the search lanes ration a process before the caps
    do (DuckDuckGo challenges after a couple of queries)."""
    from silica.model import llm
    from silica.sources.web_research import unique_inbox_path

    p = _clean_plan(approved)
    with job_records.LOCK:
        if any(r.get("state") in ("queued", "running") for r in _records()):
            raise Refused("a research is already running: it ends before another starts")
        # The parts' cut, so the folder never ends on ", an" (second live run).
        parent = unique_inbox_path(_part_name(p["question"]), fallback="deep-research")
        now = time.time()
        rec = {"id": job_records.new_id(), "kind": JOB_KIND, "origin": "research",
               "source": {"path": parent, "name": p["question"]},
               "vault": str(CONFIG.vault_path or ""), "session": None, "drop": "",
               "state": "running", "created": now, "started": now, "finished": None,
               "error": "", "dismissed": False, "nucleated": False,
               **{k: p[k] for k in ("shape", "question", "note", "depth", "scholarly", "protocol", "searches")},
               "parent": parent, "folder": parent.removesuffix(".md"),
               "parts": [{"q": x["q"], "held": x["held"], "state": "held" if x["held"] else "waiting",
                          "note": "", "quotes": 0, "pages": 0, "steps": 0} for x in p["parts"]],
               "topics": [], "meter": llm.new_meter()}
        _write(rec)
    stop = _STOPS[rec["id"]] = threading.Event()
    threading.Thread(target=run or _run, args=(rec, stop), daemon=True,
                     name=f"research-{rec['id']}").start()
    return public(rec)


def stop(job_id: str) -> dict:
    """Stop after the loop now running hands back what it has."""
    rec = read(job_id)
    if rec is None:
        raise Refused("there is no such research")
    if rec.get("state") != "running":
        raise Refused("this research is not running")
    ev = _STOPS.get(job_id)
    if ev is None:
        raise Refused("this research belongs to another process: it ends there")
    ev.set()
    return public(rec)


def dismiss(job_id: str) -> None:
    """The person is done with the row. The notes stay where they are."""
    with job_records.LOCK:
        rec = read(job_id)
        if rec is None:
            return
        if rec.get("state") == "running":
            raise Refused("this research is still running: stop it first")
        rec["dismissed"] = True
        _write(rec)


def mark_nucleated(job_id: str, parts: list[str]) -> list[str]:
    """The parts the person picked, checked against the record: only notes
    this job wrote can be nucleated through it."""
    with job_records.LOCK:
        rec = read(job_id)
        if rec is None or rec.get("state") != "done":
            raise Refused("this research has no parts to nucleate")
        written = [p["note"] for p in rec["parts"] if p.get("note")]
        chosen = [p for p in parts if p in written]
        if not chosen:
            raise Refused("pick at least one part")
        rec["nucleated"] = True
        _write(rec)
    return chosen


def admitted(job_id: str) -> list[str]:
    """The vault notes /nucleate wrote from this job's parts, which the row
    offers as a study plan in Memorize (ADR-0158 point 10, the cluster shape:
    notes the person never read are the vault's, not yet their knowledge).
    Found by the part's file name in a note's `sources:`, which the distiller
    writes and which a part keeps when /nucleate moves it out of the Inbox."""
    from silica.kernel.base import frontmatter
    from silica.kernel.base.layout import is_inbox_path
    from silica.kernel.base.noteshape import is_spine

    rec = read(job_id)
    if rec is None:
        raise Refused("there is no such research")
    names = {Path(p["note"]).name for p in rec["parts"] if p.get("note")}
    root = Path(CONFIG.vault_path or ".")
    out: list[str] = []
    # A walk of the files and not the note index: the notes were written a
    # moment ago, and the index catches up on its own schedule.
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if any(part.startswith(".") for part in path.relative_to(root).parts) \
                or is_inbox_path(rel) or rel.startswith("sources/"):
            continue
        try:
            data, _, _ = frontmatter.split(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue  # one unreadable note is one note not offered
        if not isinstance(data, dict):
            continue
        # Not the person's own notes the run patched (`AI: partial`, measured
        # on the first live run: two of thirteen), nor a spine, nor the
        # folder's hub (`Notes/Notes.md`): those are not what the run taught.
        if str(data.get("AI", "")).lower() == "partial" or is_spine(data) or path.stem == path.parent.name:
            continue
        srcs = data.get("sources")
        srcs = [srcs] if isinstance(srcs, str) else srcs or []
        if any(Path(str(s)).name in names for s in srcs):
            out.append(rel)
    return out


def _run(rec: dict, stop: threading.Event) -> None:
    from silica.model import llm
    from silica.model.usage_limits import UsageLimited
    from silica.sources.web_research import own_turn, web_research

    def update(**fields: Any) -> None:
        with job_records.LOCK:
            rec.update(fields)
            _write(rec)

    sub = Path(rec["parent"]).stem  # the parts' folder, beside the parent note
    topics_in: list[str] = []
    try:
        with llm.meter_scope(rec["meter"]):
            for part in rec["parts"]:
                if part["state"] == "held":
                    continue
                if stop.is_set():
                    part["state"] = "skipped"
                    continue
                part["state"] = "running"
                update()

                def step(event: Any, part: dict = part) -> None:
                    from silica.obs.events import ToolCompleteEvent

                    if isinstance(event, ToolCompleteEvent):
                        part["steps"] += 1
                        if event.name == "web_fetch":
                            part["pages"] += 1
                        update()

                with own_turn() as turn:
                    try:
                        part["note"] = web_research(
                            part["q"], max_searches=rec["searches"], tool_progress_callback=step,
                            folder=sub, name=_part_name(part["q"]), quote_ids=True,
                            scholarly=bool(rec.get("scholarly")), shape=rec["shape"], cancel_token=stop,
                        )
                        part["state"] = "done"
                    except UsageLimited:
                        raise
                    except ValueError:
                        # The loop's own word for "no findings": the stop
                        # cancelled it, or nothing was found. The run goes on.
                        part["state"] = "stopped" if stop.is_set() else "empty"
                    part["quotes"] = len(turn.bank)
                    topics_in += [q.why for q in turn.bank.values()]
                update()
            topics = _topics(rec, topics_in) if not stop.is_set() else []
    except UsageLimited as exc:
        for part in rec["parts"]:
            if part["state"] in ("waiting", "running"):
                part["state"] = "skipped"
        layer = {"window": "5-hour", "week": "weekly", "month": "monthly"}.get(exc.layer, exc.layer)
        _finish(rec, update, [], error=f"the {layer} usage limit was reached; "
                                       "the parts written so far are in the Inbox")
        return
    except Exception:
        # The thread has no caller to raise to: the record carries the failure
        # to the page, and the parts already written stay in the Inbox.
        logger.exception("research %s failed", rec["id"])
        _finish(rec, update, [], error="the research stopped on an error; the parts written are in the Inbox")
        return
    _finish(rec, update, topics)


def _finish(rec: dict, update, topics: list[str], error: str = "") -> None:
    written = [p for p in rec["parts"] if p.get("note")]
    if written or any(p["state"] == "held" for p in rec["parts"]):
        try:
            _write_parent(rec, topics)
        except Exception:
            # Tolerated: the parts are written and listed on the record; a
            # parent that failed costs the list, never a part.
            logger.exception("research %s: parent note not written", rec["id"])
    if not written and not error:
        error = "nothing was found for any sub-question"
    update(state="failed" if error and not written else "done", error=error, topics=topics,
           finished=time.time())
    _STOPS.pop(rec["id"], None)


def _part_name(q: str) -> str:
    """A file name for a part: the sub-question cut at a word near 80
    characters, never on a comma or inside a parenthesis it opened (measured
    on the first live run: "...formulations (mRNA construct,.md")."""
    q = re.sub(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", "", q).rstrip("?").strip()
    if len(q) > 80:
        q = q[:80].rsplit(" ", 1)[0] or q[:80]
        if q.count("(") > q.count(")"):
            q = q[:q.rindex("(")]
    return q.rstrip(" ,;:-(").strip() or "part"


_TOPICS_SYSTEM = """You get a research question, the sub-questions it was \
split into, and one line per piece of evidence the research banked. List up \
to six topics the evidence raises that none of the sub-questions asks about, \
each a short noun phrase in the language of the question. Reply with a JSON \
list of strings only; [] when there are none."""


def _topics(rec: dict, whys: list[str]) -> list[str]:
    """Topics met on the way and not researched (the plan holds): one call over
    the bank's lines. Best-effort; a failure costs the list only."""
    from silica.model.reply import decode_json

    if not whys:
        return []
    subs = "\n".join(f"- {p['q']}" for p in rec["parts"])
    lines = "\n".join(f"- {w}" for w in whys[:150])
    try:
        raw = _ask([{"role": "system", "content": _TOPICS_SYSTEM},
                    {"role": "user", "content": f"Question: {rec['question']}\n\nSub-questions:\n{subs}"
                                                f"\n\nEvidence:\n{lines}"}])
        data, _ = decode_json(raw)
    except Exception:
        # Tolerated: the list is an offer at the end, not part of the result.
        logger.debug("research %s: topics not listed", rec["id"], exc_info=True)
        return []
    return [re.sub(r"\s+", " ", str(t)).strip()[:120] for t in data if str(t).strip()][:6] \
        if isinstance(data, list) else []


def _write_parent(rec: dict, topics: list[str]) -> None:
    """The parent note: the question, the protocol, the parts in order as
    links, and the topics left for another run. `document: true` keeps it out
    of /nucleate (ADR-0129 point 5): it is a list of links, not a source."""
    from silica.driver import DRIVER

    today = datetime.date.today().isoformat()
    lines = [
        "---",
        f"title: {json.dumps(rec['question'])}",
        "source: deep-research",
        f"shape: {rec['shape']}",
        "document: true",
        f"fetched: {today}",
        "tags: [inbox, deep-research]",
        "---",
        "",
        rec["question"],
        "",
    ]
    if rec.get("protocol"):
        lines += ["## Protocol", "", rec["protocol"], ""]
    lines += ["## Parts", ""]
    for i, p in enumerate(rec["parts"], 1):
        if p.get("note"):
            stem = p["note"].removesuffix(".md")
            lines.append(f"{i}. [[{stem}|{p['q']}]] ({p['quotes']} quotes)")
        elif p["state"] == "held":
            lines.append(f"{i}. {p['q']}: your vault has [[{p['held'].removesuffix('.md')}]], not researched again")
        elif p["state"] == "empty":
            lines.append(f"{i}. {p['q']}: nothing found, no part written")
        else:
            lines.append(f"{i}. {p['q']}: not researched, the run was stopped")
    if topics:
        lines += ["", "## Met on the way, not researched", ""] + [f"- {t}" for t in topics]
    DRIVER.create(rec["parent"], "\n".join(lines) + "\n")
