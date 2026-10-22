# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Proposed skills (ADR-0127 point 5): a skill the model wrote from a kind of
request the person made again and again and never undid, waiting until the
person installs it.

Three steps, each with its own guarantee:

- **Detect**, by rule, at the end of a chat turn: the request's instruction
  (its first paragraph, as silica_rework reads it) shares its content words
  with requests of at least two earlier chats, none of which ran /undo or
  /revert. No model is asked whether two requests are alike.
- **Draft**, one worker call, which sees only the person's own instructions:
  never a tool result, a note, a pasted document or an answer. That is the
  form user memory's untainted gate takes here (silica/tools/user_memory.py):
  what could carry someone else's instruction never reaches the call that
  writes the skill, so no session needs to have been untainted, which a study
  session that opened a PDF never is.
- **Stage**, under `.silica/skills-proposed/<name>/`, where discover() never
  looks: nothing lists it and nothing runs it until the person installs it from
  Settings › Skills, which moves it into `.silica/skills/`.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

STAGING = ".silica/skills-proposed"
# The owner's number (2026-10-22): the same kind of request in three separate
# chats, the one in progress included.
N_SESSIONS = 3
# The instruction, as silica_rework's `ask`: the person's words before the
# first blank line, at most 300 characters. The instruction repeats across
# chats; the pasted material after it does not.
ASK_CHARS = 300
# Two requests are of one kind when they share at least MIN_SHARED stems and
# those are at least JACCARD of the stems either uses. Jaccard and not the
# overlap with the shorter one: "fammi un quiz sul capitolo del manuale" shares
# capitolo and manuale with "integra gli appunti col manuale, capitolo per
# capitolo", and only the union notices the verb that differs. A skill covers
# a request when it holds COVER of the request's stems. Fixed on the phrasings
# in tests/test_proposed_skills.py and measured on the owner's own chats
# (ADR-0127); reopen when a proposal is dismissed as "not the same request"
# more than once in ten.
MIN_SHARED = 2
JACCARD = 0.5
COVER = 0.6
STEM = 6   # letters kept of a word: integra/integrare, capitolo/capitoli meet
DRAFT_TIMEOUT_S = 45.0
CAP = 32 * 1024   # an installed skill's cap (ui/web/skills_routes.CAP); a proposal installs as one

# Letters only, four or more: articles, prepositions and numbers drop out in
# Italian and English alike. What is left of the function words is listed.
_WORD = re.compile(r"[^\W\d_]{4,}")
_STOP = frozenset("""
questo questa questi queste quello quella quelli quelle della delle dello degli
nella nelle nello negli dalla dalle dallo dagli alla alle allo agli sono come
anche molto dove quando quale quali perché perche cosa fammi puoi potresti vorrei
voglio grazie ciao miei mie tuoi tue suoi sue nostri ogni tutto tutti tutte
sull dell nell dall
about this that these those with from what which into your have please could
would there their them then than just also some
""".split())
_UNDO = ("/undo", "/revert")
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def ask(text: str) -> str:
    """The instruction a request opens with."""
    from silica.agent import mentions

    text = mentions.strip(str(text or "")).strip()
    return re.split(r"\n\s*\n", text, maxsplit=1)[0][:ASK_CHARS].strip()


def words(text: str) -> frozenset:
    """The stems of a text's content words."""
    return frozenset(w[:STEM] for w in _WORD.findall(text.lower()) if w not in _STOP)


def alike(a: frozenset, b: frozenset) -> bool:
    """Two requests of one kind."""
    shared = len(a & b)
    return shared >= MIN_SHARED and shared >= JACCARD * len(a | b)


# stem -> the stem sets of the vault's note names holding it, per vault, read
# again after TITLES_TTL_S: a note added a minute ago is rarely a request's
# subject yet, and a walk per turn would cost a large vault every turn.
TITLES_TTL_S = 600.0
_TITLES: dict[str, tuple[float, dict[str, list[frozenset]]]] = {}


def _titles(vault: str) -> dict[str, list[frozenset]]:
    hit = _TITLES.get(vault)
    if hit is not None and time.monotonic() - hit[0] < TITLES_TTL_S:
        return hit[1]
    root = Path(vault)
    index: dict[str, list[frozenset]] = {}
    for p in root.rglob("*.md") if root.is_dir() else ():
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue   # .silica, .obsidian, .trash: no subject of anyone's request
        stems = words(p.stem)
        for s in stems:
            index.setdefault(s, []).append(stems)
    _TITLES[vault] = (time.monotonic(), index)
    return index


def one_subject(shared: frozenset, titles: dict[str, list[frozenset]]) -> bool:
    """Every shared stem is in one note's name: two requests about Machine
    learning share a subject, not an instruction. Measured 2026-10-22, "Affronta
    in breve Machine learning" met "Cos'è il Machine Learning?" on those two
    words alone. Only the shared stems are tested, never removed from the
    requests: a student vault has notes called Appunti or Manuale, and
    "integra gli appunti col manuale" must still meet its repetitions."""
    if not shared:
        return False
    rare = min(shared, key=lambda s: len(titles.get(s, ())))
    return any(shared <= t for t in titles.get(rare, ()))


def same_kind(a: frozenset, b: frozenset, titles: dict[str, list[frozenset]]) -> bool:
    return alike(a, b) and not one_subject(a & b, titles)


def covers(skill: frozenset, request: frozenset) -> bool:
    """A skill's name and description answer this kind of request."""
    shared = len(skill & request)
    return shared >= MIN_SHARED and shared >= COVER * len(request)


def _skill_words(s) -> frozenset:
    return words(f"{s.name.replace('-', ' ')} {s.description}")


def _undid(messages: list[dict]) -> bool:
    return any(m.get("role") == "user" and str(m.get("content") or "").lstrip().lower().startswith(_UNDO)
               for m in messages)


def _asks(messages: list[dict]) -> list[str]:
    """A chat's typed requests: a slash command is a skill or a command already."""
    return [a for m in messages
            if m.get("role") == "user" and m.get("origin") != "cli" and isinstance(m.get("content"), str)
            and not m["content"].lstrip().startswith("/") and (a := ask(m["content"]))]


# sid -> (last-active time, its requests, whether it undid). A finished chat
# never changes, so each is read once per process; the first turn of a process
# pays the walk (0.17 s for 39 chats, measured 2026-10-06 for session search).
_HISTORY: dict[str, tuple[float, list[str], bool]] = {}


def _history(vault: str, current: str | None) -> list[tuple[str, list[str], bool]]:
    from silica.agent import narration

    out = []
    for row in narration.list_sessions(vault):
        sid = row["id"]
        if sid == current:
            continue
        hit = _HISTORY.get(sid)
        if hit is None or hit[0] != row["updated"]:
            msgs = narration.load_session_messages(sid, vault) or []
            hit = _HISTORY[sid] = (row["updated"], _asks(msgs), _undid(msgs))
        out.append((sid, hit[1], hit[2]))
    return out


def _staged(vault: Path) -> list[Path]:
    root = vault / STAGING
    return sorted(root.glob("*/proposal.json")) if root.is_dir() else []


def offer(text: str, vault: str) -> dict | None:
    """The installed skill that answers the kind of request `text` is, to offer
    as the next turn (ADR-0127 point 6): the person launches it, the model
    never does. The same rule as detection, against the skill's name and
    description; the first in name order when several do."""
    from silica.kernel.personas import is_persona
    from silica_core.kernel.skills import discover

    if not vault or text.lstrip().startswith("/"):
        return None
    w = words(ask(text))
    if len(w) < MIN_SHARED:
        return None
    for s in discover(vault).values():
        if covers(_skill_words(s), w):
            return {"command": "/" + s.name, "description": (s.description.splitlines() or [""])[0],
                    "persona": is_persona(s)}
    return None


def _known(vault: Path, w: frozenset, titles: dict) -> bool:
    """A skill already answers this kind of request, or a proposal for it is
    waiting or was dismissed: the person has been asked once."""
    from silica_core.kernel.skills import discover

    for s in discover(str(vault)).values():
        if covers(_skill_words(s), w):
            return True
    for p in _staged(vault):
        try:
            asks = json.loads(p.read_text(encoding="utf-8")).get("asks") or []
        except (OSError, ValueError):
            continue   # a proposal file damaged by hand counts as no proposal
        if any(same_kind(w, words(a), titles) for a in asks):
            return True
    return False


def detect(text: str, messages: list[dict], vault: str, current: str | None) -> dict | None:
    """The evidence for a proposal at the end of a turn, or None.

    `text` is the request as the person typed it, `messages` this chat."""
    if not vault or text.lstrip().startswith("/") or _undid(messages):
        return None
    mine = ask(text)
    w = words(mine)
    if len(w) < MIN_SHARED:
        return None
    titles = _titles(vault)
    if _known(Path(vault), w, titles):
        return None
    found: dict[str, str] = {}
    for sid, asks, undid in _history(vault, current):
        if undid:
            continue
        match = next((a for a in asks if same_kind(w, words(a), titles)), None)
        if match is not None:
            found[sid] = match
    if len(found) + 1 < N_SESSIONS:
        return None
    return {"asks": [mine, *found.values()], "sessions": [current or "", *found]}


_DRAFT_PROMPT = """\
You turn a request a person keeps making into a reusable command for their \
notes assistant, Silica. You get the person's own words from several chats; \
nothing else. Write the command so that typing it does what they kept asking \
for, in general and not only for the subject of one of the requests.

Reply with one JSON object and nothing else:
{"name": "...", "description": "...", "body": "..."}

- name: 2 to 4 lowercase words joined by single hyphens, in the requests' \
language, naming the action (e.g. integra-appunti).
- description: one sentence in the requests' language saying what the \
command does and when to use it.
- body: the instructions the assistant receives, in the requests' language, \
in Markdown, at most 1,500 characters. Address the assistant. Keep every \
preference the person stated in more than one request; leave out the \
subject of any single request. Where the person's material goes, write \
$ARGUMENTS.
"""


def draft(asks: list[str], config) -> dict | None:
    """One worker call over the person's instructions; None when it fails,
    overruns DRAFT_TIMEOUT_S or answers something that is not a skill."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as Late

    from silica.agent.providers import get_provider
    from silica.kernel.text.sanitize import parse_json

    cancel = threading.Event()
    user = "The person's requests, one per chat:\n\n" + "\n".join(f"- {a}" for a in asks)

    def call():
        provider = get_provider(config, role="worker")
        return provider.call_llm(messages=[{"role": "system", "content": _DRAFT_PROMPT},
                                           {"role": "user", "content": user}],
                                 tools=None, max_tokens=2000, cancel=cancel)

    pool = ThreadPoolExecutor(1)
    try:
        reply = pool.submit(call).result(timeout=DRAFT_TIMEOUT_S)
    except Late:
        cancel.set()
        logger.warning("skill draft overran %.0f s; no proposal this turn", DRAFT_TIMEOUT_S)
        return None
    finally:
        pool.shutdown(wait=False)
    try:
        out, _ = parse_json(reply.text or "", strict=False)
    except Exception as e:
        # A reply that is not JSON proposes nothing; the next repetition asks again.
        logger.info("skill draft unparsable: %s", e)
        return None
    if not isinstance(out, dict) or not all(isinstance(out.get(k), str) and out[k].strip()
                                            for k in ("name", "description", "body")):
        return None
    return {k: out[k].strip() for k in ("name", "description", "body")}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48].strip("-")
    return s if _NAME.fullmatch(s or "-") else "skill"


def stage(vault: str, proposal: dict, evidence: dict, model: str = "") -> str | None:
    """Write the proposal where only Settings reads it; its name, or None when
    the text does not load as a skill."""
    from silica.ui.commands import COMMANDS
    from silica_core.kernel.skills import discover, load

    root = Path(vault)
    taken = {c.name.lstrip("/") for c in COMMANDS} | set(discover(vault)) | {p.parent.name for p in _staged(root)}
    base = name = _slug(proposal["name"])
    n = 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    text = (f"---\nname: {name}\ndescription: {json.dumps(proposal['description'], ensure_ascii=False)}\n"
            f"argument-hint: \"[text or note]\"\n---\n{proposal['body']}\n")
    if len(text.encode("utf-8")) > CAP:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / name / "SKILL.md"
        probe.parent.mkdir()
        probe.write_text(text, encoding="utf-8")
        if load(probe, "vault") is None:
            return None
    folder = root / STAGING / name
    if not folder.parent.resolve().is_relative_to(root.resolve()):
        return None   # a .silica that leads out of the vault is not written through
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    (folder / "proposal.json").write_text(json.dumps({
        "asks": evidence["asks"], "sessions": evidence["sessions"], "created": time.time(),
        "model": model, "dismissed": False}, ensure_ascii=False, indent=1), encoding="utf-8")
    return name


def pending(vault: str) -> list[dict]:
    """The proposals waiting for the person, newest first, for Settings."""
    out = []
    for p in _staged(Path(vault)) if vault else []:
        try:
            meta = json.loads(p.read_text(encoding="utf-8"))
            text = (p.parent / "SKILL.md").read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue   # damaged by hand: Settings shows the rest
        if meta.get("dismissed"):
            continue
        from silica_core.kernel.write.frontmatter import split

        data, _raw, body = split(text)
        out.append({"name": p.parent.name, "description": str((data or {}).get("description") or ""),
                    "body": body.strip(), "asks": meta.get("asks") or [], "created": meta.get("created", 0)})
    return sorted(out, key=lambda r: -r["created"])


def install(vault: str, name: str) -> str | None:
    """Move a proposal into the vault's skills; an error sentence, or None."""
    folder = Path(vault) / STAGING / name
    dest = Path(vault) / ".silica" / "skills" / name
    if not _NAME.fullmatch(name) or not (folder / "SKILL.md").is_file():
        return f"no proposed skill is named {name}"
    if dest.exists():
        return f"a vault skill named {name} exists already: remove it first, or dismiss this one"
    dest.mkdir(parents=True)
    shutil.move(str(folder / "SKILL.md"), dest / "SKILL.md")
    shutil.rmtree(folder)
    return None


def dismiss(vault: str, name: str) -> str | None:
    """Keep the proposal's requests and drop its text: the same kind of request
    is never proposed again (`_known`)."""
    folder = Path(vault) / STAGING / name
    meta_path = folder / "proposal.json"
    if not _NAME.fullmatch(name) or not meta_path.is_file():
        return f"no proposed skill is named {name}"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["dismissed"] = True
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "SKILL.md").unlink(missing_ok=True)
    return None
