# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0095 gate harness: the numbers rule over recorded chat answers.

Why this exists: the chat draws a line under an answer naming the numbers its
session did not read, and the line stays only if the owner's labels show it
points at wrong numbers often enough (ADR-0095, Gate, pre-registered
2026-10-11, labels due 2026-12-15). A flag means "not read", never "wrong", so
the warning rate alone cannot decide it. This builds what the labeller reads,
scores what the labeller wrote, and measures everything that needs no label.

The evidence is the product's own, through the product's functions
(`recall_watch.result_text`, `turn_evidence`, `provenance.unfounded_numbers`):
every successful raw result of the session up to the answer, the user's
messages, and the session's date line. The product's seed adds the system
prompt, the vault map and the memory snapshot, so it can only flag fewer.

No model call and no network. It reads copies: `build` copies the narration
into --scratch before reading it, and --evaluation must point at a copy of the
chat-probe sets. It writes only under --scratch and --out.

  uv run python -m evals.probe_answer_check build \\
      --narration ~/.silica/narration --evaluation <copy of docs/evaluation> \\
      --scratch <dir> --out <dir>
  uv run python -m evals.probe_answer_check score --out <dir>

From R_REPLAY_FROM, if R still holds fewer than R_SIZE real answers, `replay`
re-issues recorded real turns to the default local model with their context
fixed, one replay per turn, and the next `build` puts them in R:

  SILICA_VAULT=<the sessions' vault> uv run python -m evals.probe_answer_check \
      replay --scratch <dir> --out <dir>
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import random
import re
import shutil
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from silica.agent.recall_watch import result_text, turn_evidence
from silica.kernel.write.provenance import checked_numbers, number_tokens, unfounded_numbers

# The 57 recorded real answers end here; R takes the next 43 after it, by time.
R_RECORDED_TO = datetime.date(2026, 10, 9)
R_SIZE = 100
# If R holds fewer than R_SIZE answers by this day, fixed-context replays of
# recorded real turns fill the shortfall (ADR-0095, Gate).
R_REPLAY_FROM = datetime.date(2026, 11, 30)
LABELS_DUE = datetime.date(2026, 12, 15)
P_SIZE, P_SEED = 50, 90
RECALL_SEED = 95
PROBE_SETS = ("prompt-rules", "compaction", "read-query", "model-tier", "saas-models")

LABELS = ("wrong", "outside", "derivable")
LABEL_HELP = (
    "- wrong: contradicts the evidence, or is a checkable fact that is false (a changed\n"
    "  value, a wrong constant, an identifier that names another paper or none, a\n"
    "  miscount of items a result lists).\n"
    "- outside: not from the evidence and not shown wrong (a correct constant from general\n"
    "  knowledge, correct arithmetic from it, an assumption or an estimate the model chose).\n"
    "- derivable: the evidence or the user states it in another spelling, or it follows\n"
    "  from their numbers by the arithmetic the answer shows or one evident step (a count\n"
    "  of listed items, a date offset), or it numbers the answer's own plan.\n"
)
# The four ship conditions, fixed before the first label (ADR-0095, Gate).
R_WRONG_MIN = 3
WRONG_SHARE_MIN = 0.3
DERIVABLE_MAX = 0.30
WARN_MAX = 0.20

WINDOW = 160  # characters each side of a number in the labeller's windows


@dataclass
class Answer:
    src: str            # "real" or "probe"
    session: str
    turn: int
    ts: float
    driver: str
    model: str
    question: str
    text: str           # the answer as the model wrote it
    web: bool
    results: list[str] = field(default_factory=list)  # decoded, this turn and earlier
    users: list[str] = field(default_factory=list)    # user messages to this turn
    date_line: str = ""
    evidence: set[str] = field(default_factory=set)
    flagged: list[str] = field(default_factory=list)
    tag: str = ""       # "~replay" for a fixed-context replay of the turn

    @property
    def id(self) -> str:
        return f"{self.session}#{self.turn}{self.tag}"

    def evidence_text(self) -> str:
        return "\n".join([*self.results, *self.users, self.date_line])


def date_line(ts: float | None) -> str:
    """The seed's date line (cli._today_line) for the day a session started."""
    if not ts:
        return ""
    d = datetime.date.fromtimestamp(ts)
    return f"Today is {d:%A, %d %B %Y} ({d.isoformat()})."


def _web_prefix() -> str:
    from silica.sources.web_research import web_turn_instruction

    # The /web turn's user message opens with the question line; the product
    # never checks that turn, whose answer is bound to banked quotes.
    return web_turn_instruction("").split("\n", 1)[0]


def _text_of(content) -> str:
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


def _beats(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    rows.sort(key=lambda r: r.get("seq") or 0)
    return rows


def session_turns(path: Path) -> tuple[dict, list[dict]]:
    """(session meta, turns): each turn is the user message, the raw results of
    its successful tool calls, the tools it called and its final answer."""
    rows = _beats(path)
    head = next((r for r in rows if r.get("kind") == "session"), {})
    models: dict[str, int] = {}
    for r in rows:
        if r.get("kind") == "call" and r.get("status") == "done":
            m = (r.get("payload") or {}).get("model")
            if m:
                models[m] = models.get(m, 0) + 1
    meta = {"driver": (head.get("payload") or {}).get("driver"), "ts": head.get("ts"),
            "model": max(models, key=models.__getitem__) if models else None}
    turns: list[dict] = []
    cur: dict | None = None
    args: dict = {}  # a tool span's opening beat holds the call's arguments
    for r in rows:
        kind, payload = r.get("kind"), r.get("payload") or {}
        if kind == "tool" and r.get("status") == "running":
            args[r.get("id")] = payload.get("args") or {}
        if kind == "turn":
            m = payload.get("message") or {}
            if m.get("role") == "user":
                cur = {"q": _text_of(m.get("content")), "results": [], "tools": [],
                       "answer": None, "ts": r.get("ts")}
                turns.append(cur)
            elif m.get("role") == "assistant" and cur is not None and not m.get("tool_calls"):
                cur["answer"] = _text_of(m.get("content"))
                cur["ts"] = r.get("ts")
        elif kind == "tool" and r.get("status") == "done" and cur is not None:
            cur["tools"].append(payload.get("name"))
            if isinstance(payload.get("result"), str):
                cur["results"].append((payload["result"], args.get(r.get("id"))))
    return meta, turns


def _checked_text(answer: str, tools: list[str]) -> str:
    """The answer before the callers' additions: relay_sources appends the web
    door's Sources block after a silica_web_answer call. The figures that
    place_figures adds are embeds, which the rule never checks."""
    if "silica_web_answer" in tools:
        i = answer.rfind("\n## Sources")
        if i != -1:
            return answer[:i]
    return answer


def answers_of(src: str, session: str, meta: dict, turns: list[dict],
               web_prefix: str) -> list[Answer]:
    out: list[Answer] = []
    seen: set[str] = set()
    results: list[str] = []
    users: list[str] = []
    line = date_line(meta.get("ts"))
    for k, t in enumerate(turns):
        decoded = [result_text(r, a) for r, a in t["results"]]
        turn_numbers: set[str] = set()
        for d in decoded:
            turn_numbers |= number_tokens(d)
        users.append(t["q"])
        answer = (t.get("answer") or "").strip()
        if answer and not answer.startswith("(silica:"):
            a = Answer(src=src, session=session, turn=k, ts=t.get("ts") or meta.get("ts") or 0.0,
                       driver=meta.get("driver") or "", model=meta.get("model") or "",
                       question=t["q"], text=_checked_text(answer, t["tools"]),
                       web=t["q"].startswith(web_prefix),
                       results=[*results, *decoded], users=list(users), date_line=line)
            msgs = [{"role": "system", "content": line}] + [
                {"role": "user", "content": u} for u in users]
            a.evidence = turn_evidence(turn_numbers, msgs, set(seen))
            a.flagged = [] if a.web else unfounded_numbers(a.text, a.evidence)
            out.append(a)
        seen |= turn_numbers
        results += decoded
    return out


def copy_narration(src: Path, scratch: Path) -> Path:
    """The harness reads a copy: a live session file is appended to under a flock."""
    dst = scratch / "narration"
    dst.mkdir(parents=True, exist_ok=True)
    for f in sorted(src.glob("*.jsonl")):
        target = dst / f.name
        st = f.stat()
        if target.exists() and target.stat().st_size == st.st_size \
                and target.stat().st_mtime == st.st_mtime:
            continue
        shutil.copy2(f, target)
    return dst


def load_real(narration: Path) -> list[Answer]:
    prefix = _web_prefix()
    out: list[Answer] = []
    for f in sorted(narration.glob("*.jsonl")):
        meta, turns = session_turns(f)
        if meta["driver"] not in ("gui", "tui"):
            continue
        out += answers_of("real", f.stem, meta, turns, prefix)
    out.sort(key=lambda a: (a.ts, a.session, a.turn))
    return out


def load_probes(evaluation: Path) -> list[Answer]:
    """The chat-probe answers: the case file's recorded turns, paired with the
    user turns of its sibling narration, which holds the raw results. The probe
    driver never narrated the final answer, hence the pairing."""
    prefix = _web_prefix()
    out: list[Answer] = []
    for name in PROBE_SETS:
        for j in sorted((evaluation / name).rglob("*.json")):
            try:
                case = json.loads(j.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if not isinstance(case, dict) or not case.get("turns"):
                continue
            narr = j.with_name(j.stem + ".narration.jsonl")
            if not narr.exists():
                continue
            meta, turns = session_turns(narr)
            users = [t.get("user") or "" for t in case["turns"]]
            answers = [t.get("answer") for t in case["turns"]]
            k = 0
            for t in turns:
                t["answer"] = None
                if k < len(users) and (t["q"].strip()[:40] in users[k][:4000]
                                       or users[k].strip()[:40] in t["q"]):
                    t["answer"] = answers[k]
                    k += 1
            meta["model"] = case.get("model") or meta["model"]
            session = str(j.relative_to(evaluation).with_suffix(""))
            out += answers_of("probe", session, meta, turns, prefix)
    return out


# --- the label sets ----------------------------------------------------------

def set_r(real: list[Answer]) -> tuple[list[Answer], int]:
    """(R, how many of its answers were recorded to R_RECORDED_TO)."""
    cutoff = datetime.datetime.combine(R_RECORDED_TO, datetime.time.max).timestamp()
    recorded = [a for a in real if a.ts <= cutoff]
    later = [a for a in real if a.ts > cutoff]
    return recorded + later[:max(0, R_SIZE - len(recorded))], len(recorded)


def set_p(probes: list[Answer]) -> list[tuple[Answer, str]]:
    """A fixed random P_SIZE flagged numbers, one per answer before any second."""
    rng = random.Random(P_SEED)
    warned = [a for a in probes if a.flagged]
    rng.shuffle(warned)
    queues = {a.id: rng.sample(a.flagged, len(a.flagged)) for a in warned}
    picks: list[tuple[Answer, str]] = []
    rnd = 0
    while len(picks) < P_SIZE and any(len(q) > rnd for q in queues.values()):
        for a in warned:
            if len(picks) == P_SIZE:
                break
            if len(queues[a.id]) > rnd:
                picks.append((a, queues[a.id][rnd]))
        rnd += 1
    return picks


def _token_re(num: str) -> re.Pattern:
    return re.compile(r"(?<![0-9A-Za-z])" + re.escape(num) + r"(?![0-9])")


def _value(tok: str) -> float | None:
    t = tok.replace(",", ".") if tok.count(",") == 1 and "." not in tok else tok.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def _window(text: str, start: int, end: int) -> str:
    lo, hi = max(0, start - WINDOW), min(len(text), end + WINDOW)
    body = text[lo:start] + "**" + text[start:end] + "**" + text[end:hi]
    return ("..." if lo else "") + " ".join(body.split()) + ("..." if hi < len(text) else "")


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[^\W\d_]{3,}", s.lower())}


def answer_window(a: Answer, num: str) -> str:
    m = _token_re(num).search(a.text)
    return _window(a.text, m.start(), m.end()) if m else a.text[:2 * WINDOW]


def evidence_windows(a: Answer, num: str) -> list[tuple[str, str]]:
    """The evidence passages the labeller most likely needs: around the
    nearest value (243 finds "242 columns", the case `wrong` exists for), and
    around the number whose surroundings share the most words with the
    answer's (what `derivable` is built from). One when both are the same."""
    text = a.evidence_text()
    near = _words(answer_window(a, num))
    want = _value(num)
    by_value, by_words = None, None
    for m in re.finditer(r"(?<![0-9A-Za-z])-?\d+(?:[.,]\d+)*", text):
        got = _value(m.group(0))
        close = 0.0
        if want is not None and got is not None:
            close = max(0.0, 1 - abs(want - got) / max(abs(want), abs(got), 1e-9))
        shared = len(near & _words(text[max(0, m.start() - WINDOW):m.end() + WINDOW]))
        if by_value is None or (close, shared) > by_value[0]:
            by_value = ((close, shared), m)
        if by_words is None or (shared, close) > by_words[0]:
            by_words = ((shared, close), m)
    if by_value is None or by_words is None:
        return [("evidence", " ".join(text.split())[:2 * WINDOW])]
    out = [("nearest value", _window(text, by_value[1].start(), by_value[1].end()))]
    if by_words[1].start() != by_value[1].start():
        out.append(("most shared words", _window(text, by_words[1].start(), by_words[1].end())))
    return out


def _quote(s: str) -> str:
    return "\n".join("> " + line for line in s.splitlines() or [""])


_ROW_RE = re.compile(r"^## (\S+) · ", re.M)
_LABEL_RE = re.compile(r"^- label:[ \t]*(\S*)[ \t]*$", re.M)
_NOTE_RE = re.compile(r"^- note:[ \t]*(.*)$", re.M)
_ANSWER_RE = re.compile(r"^- answer: (\S+) · ", re.M)


def _rows_of(path: Path) -> list[tuple[str, str]]:
    """(row id, row body) of a label file, in order."""
    text = path.read_text(encoding="utf-8")
    heads = list(_ROW_RE.finditer(text))
    return [(h.group(1), text[h.end():heads[i + 1].start() if i + 1 < len(heads) else len(text)])
            for i, h in enumerate(heads)]


def written(path: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """{(answer id, number): (label, note)} already in a label file. A rebuild
    carries them over by answer and number, never by row id: R grows as real
    answers arrive, and the owner's labels must survive that."""
    if not path.exists():
        return {}
    out = {}
    for _rid, body in _rows_of(path):
        number = body.split("\n", 1)[0].strip()
        answer, label, note = (_ANSWER_RE.search(body), _LABEL_RE.search(body),
                               _NOTE_RE.search(body))
        if answer and ((label and label.group(1)) or (note and note.group(1).strip())):
            out[(answer.group(1), number)] = (label.group(1) if label else "",
                                              note.group(1).strip() if note else "")
    return out


def write_labels(path: Path, name: str, title: str, rows: list[tuple[str, Answer, str]],
                 intro: str) -> None:
    keep = written(path)
    parts = [f"# ADR-0095 labels, set {name}: {title}\n", intro,
             f"Label each number on its `- label:` line with one of {', '.join(LABELS)}. "
             f"A blank label keeps the row pending. Due {LABELS_DUE.isoformat()}.\n",
             LABEL_HELP]
    for rid, a, num in rows:
        when = datetime.date.fromtimestamp(a.ts).isoformat() if a.ts else "?"
        label, note = keep.get((a.id, num), ("", ""))
        parts.append(
            f"\n## {rid} · {num}\n"
            f"- label:{' ' + label if label else ''}\n- note:{' ' + note if note else ''}\n"
            f"- answer: {a.id} · {a.src} · {a.driver} · {a.model} · {when}\n"
            f"- question: {' '.join(a.question.split())[:300]}\n\n"
            f"answer:\n{_quote(answer_window(a, num))}\n"
            + "".join(f"\nevidence, {how}:\n{_quote(w)}\n" for how, w in evidence_windows(a, num)))
    path.write_text("\n".join(parts), encoding="utf-8")


# --- the replays that fill R's shortfall ------------------------------------

def turn_context(path: Path, k: int) -> list[dict]:
    """The recorded messages of a session up to turn *k*'s answer: everything
    the model had when it wrote it, the answer itself out. System messages are
    left out as `probe_study_plan_replay.load_turn` leaves them: the replay puts
    today's seed in front instead."""
    msgs = [b["payload"]["message"] for b in _beats(path)
            if b.get("kind") == "turn" and isinstance((b.get("payload") or {}).get("message"), dict)]
    out: list[dict] = []
    users = -1
    for m in msgs:
        if m.get("role") == "user":
            users += 1
            if users > k:
                break
        if m.get("role") != "system":
            out.append(m)
    while out and out[-1].get("role") == "assistant" and not out[-1].get("tool_calls"):
        out.pop()
    return out


def replay(narration: Path, out: Path, model: str, *, call=None,
           today: datetime.date | None = None) -> list[dict]:
    """Replay recorded real turns, in time order, until R holds R_SIZE answers.

    Not before R_REPLAY_FROM: until then the shortfall may still fill with real
    answers, which are the distribution the gate is about. Each replay is saved
    as it lands, so a failure costs the turns not yet called, and a second run
    pays for nothing already replayed.
    """
    path = out / "replays.json"
    done: list[dict] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    real = load_real(narration)
    r, _ = set_r(real)
    short = R_SIZE - len(r)
    if len(done) >= short:
        return done
    if (today or datetime.date.today()) < R_REPLAY_FROM:
        raise SystemExit(f"R holds {len(r)} of {R_SIZE}: replays start on "
                         f"{R_REPLAY_FROM.isoformat()}, while real answers may still arrive")
    if call is None:
        from silica.agent.llm import call_llm
        from silica.cli import seed_messages

        seed = seed_messages(math=True)

        def call(messages: list[dict]) -> str:
            return call_llm(model, seed + messages).text or ""
    have = {(d["session"], d["turn"]) for d in done}
    cutoff = datetime.datetime.combine(R_RECORDED_TO, datetime.time.max).timestamp()
    out.mkdir(parents=True, exist_ok=True)
    for a in (a for a in real if a.ts <= cutoff and (a.session, a.turn) not in have):
        if len(done) >= short:
            break
        text = call(turn_context(narration / f"{a.session}.jsonl", a.turn))
        done.append({"session": a.session, "turn": a.turn, "model": model, "answer": text,
                     "replayed": (today or datetime.date.today()).isoformat()})
        path.write_text(json.dumps(done, indent=1, ensure_ascii=False), encoding="utf-8")
    return done


def replayed_answers(real: list[Answer], out: Path) -> list[Answer]:
    """The saved replays as answers: the recorded turn's evidence, the new text."""
    path = out / "replays.json"
    if not path.exists():
        return []
    by_turn = {(a.session, a.turn): a for a in real}
    answers = []
    for d in json.loads(path.read_text(encoding="utf-8")):
        a = by_turn.get((d["session"], d["turn"]))
        text = (d.get("answer") or "").strip()
        if a is None or not text or text.startswith("(silica:"):
            continue
        answers.append(dataclasses.replace(
            a, src="replay", tag="~replay", model=d.get("model") or a.model, text=text,
            flagged=[] if a.web else unfounded_numbers(text, a.evidence)))
    return answers


# --- what needs no label ------------------------------------------------------

def _last_plus(t: str) -> str:
    i = max(i for i, c in enumerate(t) if c.isdigit())
    return t[:i] + str((int(t[i]) + 1) % 10) + t[i + 1:]


def _shift(t: str) -> str | None:
    """The decimal point moved one place: 0.025 -> 0.25, 242 -> 24.2."""
    neg, body = t.startswith("-"), t.lstrip("-")
    if re.fullmatch(r"\d+", body):
        if len(body) < 3:
            return None
        out = body[:-1] + "." + body[-1]
    elif re.fullmatch(r"\d+\.\d+", body):
        a, b = body.split(".")
        if len(b) < 2:
            return None
        out = ((a + b[0]).lstrip("0") or "0") + "." + b[1:]
    else:
        return None
    return ("-" if neg else "") + out


def _digits(t: str) -> int:
    return len(t.lstrip("-").replace(".", "").replace(",", ""))


def seeded_recall(answers: list[Answer], rng: random.Random) -> dict:
    """Would the rule flag a changed number? Every grounded number of every
    answer, each occurrence as the 2026-10-11 prototype counted them, changed,
    against the same evidence. A changed value escapes exactly when the
    evidence holds it anywhere as a token."""
    plus, shift, one, cover = [0, 0], [0, 0], [0, 0], []
    for a in sorted(answers, key=lambda a: (a.session, a.turn)):
        if a.web:
            continue
        grounded = [t for t in checked_numbers(a.text) if t not in a.flagged]
        cover.append(sum(1 for n in range(10, 100) if str(n) in a.evidence) / 90)
        for t in grounded:
            p = _last_plus(t)
            if p != t and _digits(p) >= 2:
                plus[1] += 1
                plus[0] += bool(unfounded_numbers(p, a.evidence))
            s = _shift(t)
            if s:
                shift[1] += 1
                shift[0] += bool(unfounded_numbers(s, a.evidence))
        if grounded:
            p = _last_plus(rng.choice(grounded))
            one[1] += 1
            one[0] += bool(unfounded_numbers(p, a.evidence))
    cover.sort()

    def share(c):
        return {"caught": c[0], "of": c[1], "rate": c[0] / c[1] if c[1] else None}
    return {
        "last_digit_plus_one_every": share(plus),
        "last_digit_plus_one_one_per_answer_seed95": share(one),
        "decimal_point_moved": share(shift),
        "share_10_99_in_evidence": {
            "median": statistics.median(cover) if cover else None,
            "p75": cover[int(.75 * len(cover))] if cover else None,
            "max": cover[-1] if cover else None},
    }


def _pct(xs: list[float], q: float) -> float:
    return sorted(xs)[min(len(xs) - 1, int(q * len(xs)))]


def cost(real: list[Answer]) -> dict:
    """The check's cost per real answer, the pass over the raw text included:
    every result's tokens, then the rule."""
    times, chars = [], []
    for a in real:
        text = a.evidence_text()
        t0 = time.perf_counter()
        ev = number_tokens(text)
        unfounded_numbers(a.text, ev)
        times.append((time.perf_counter() - t0) * 1000)
        chars.append(len(text))
    return {"ms_median": statistics.median(times), "ms_p95": _pct(times, .95),
            "ms_max": max(times), "evidence_chars_median": statistics.median(chars),
            "evidence_chars_max": max(chars)}


def session_set_sizes(narration: Path) -> dict:
    sizes = []
    for f in sorted(narration.glob("*.jsonl")):
        meta, turns = session_turns(f)
        if meta["driver"] not in ("gui", "tui"):
            continue
        s: set[str] = set()
        for t in turns:
            for r, args in t["results"]:
                s |= number_tokens(result_text(r, args))
        sizes.append(len(s))
    return {"sessions": len(sizes), "median": statistics.median(sizes),
            "p90": _pct(sizes, .9), "max": max(sizes)}


def rate(answers: list[Answer]) -> dict:
    warned = sum(1 for a in answers if a.flagged)
    return {"answers": len(answers), "warned": warned,
            "rate": warned / len(answers) if answers else None,
            "numbers": sum(len(a.flagged) for a in answers)}


# --- build and score ----------------------------------------------------------

def build(narration_src: Path, evaluation: Path, scratch: Path, out: Path) -> dict:
    narration = copy_narration(narration_src, scratch)
    real = load_real(narration)
    probes = load_probes(evaluation)
    r, recorded = set_r(real)
    replays = replayed_answers(real, out)[:max(0, R_SIZE - len(r))]
    r += replays
    p = set_p(probes)
    out.mkdir(parents=True, exist_ok=True)

    r_rows = [(f"R-{i:03d}", a, num) for i, (a, num) in
              enumerate(((a, n) for a in r for n in a.flagged), 1)]
    p_rows = [(f"P-{i:02d}", a, num) for i, (a, num) in enumerate(p, 1)]
    today = datetime.date.today().isoformat()
    write_labels(out / "labels-R.md", "R", "real answers", r_rows, (
        f"Built {today} from {len(r)} real GUI and TUI answers ({recorded} recorded to "
        f"{R_RECORDED_TO.isoformat()}, {len(r) - recorded - len(replays)} after, "
        f"{len(replays)} fixed-context replays); "
        f"{sum(1 for a in r if a.flagged)} warned; {len(r_rows)} flagged numbers, every one.\n"))
    write_labels(out / "labels-P.md", "P", "probe numbers", p_rows, (
        f"Built {today}: {len(p_rows)} of the numbers flagged in {len(probes)} probe answers, "
        f"one per answer before any second, seed {P_SEED}.\n"))

    gate = {
        "built": today,
        "R": {"answers": len(r), "recorded_to_cutoff": recorded, "replays": len(replays),
              "size": R_SIZE,
              "replay_from": R_REPLAY_FROM.isoformat(),
              "warned": sum(1 for a in r if a.flagged),
              "rows": {rid: a.id for rid, a, _n in r_rows}},
        "P": {"rows": {rid: a.id for rid, a, _n in p_rows}},
        "rates": {"real_all": rate(real), "R": rate(r), "probes": rate(probes),
                  **{f"probes:{s}": rate([a for a in probes if a.session.startswith(s + "/")])
                     for s in PROBE_SETS}},
        "web_answers_not_checked": {"real": sum(a.web for a in real),
                                    "probes": sum(a.web for a in probes)},
        "real_flagged": [{"answer": a.id, "model": a.model, "numbers": a.flagged}
                         for a in real if a.flagged],
    }
    rng = random.Random(RECALL_SEED)  # real first, then probes, as on 2026-10-11
    gate["seeded_recall"] = {"real": seeded_recall(real, rng), "probes": seeded_recall(probes, rng)}
    gate["cost"] = cost(real)
    gate["session_set"] = session_set_sizes(narration)
    (out / "gate.json").write_text(json.dumps(gate, indent=1, ensure_ascii=False), encoding="utf-8")
    return gate


def read_labels(path: Path) -> dict[str, str | None]:
    """{row id: label or None}. An unknown label raises: a typo must not count
    as pending or as any class."""
    out: dict[str, str | None] = {}
    for rid, body in _rows_of(path):
        m = _LABEL_RE.search(body)
        label = (m.group(1).lower() if m else "") or None
        if label is not None and label not in LABELS:
            raise ValueError(f"{path.name} {rid}: unknown label {label!r}")
        out[rid] = label
    return out


def score(out: Path) -> dict:
    gate = json.loads((out / "gate.json").read_text(encoding="utf-8"))
    res: dict = {}
    for name in ("R", "P"):
        labels = read_labels(out / f"labels-{name}.md")
        rows = gate[name]["rows"]
        pending = sum(1 for rid in rows if labels.get(rid) is None)
        by_answer: dict[str, list[str | None]] = {}
        for rid, aid in rows.items():
            by_answer.setdefault(aid, []).append(labels.get(rid))
        done = [lab for lab in labels.values() if lab]
        res[name] = {
            "rows": len(rows), "pending": pending,
            "wrong": done.count("wrong"), "outside": done.count("outside"),
            "derivable": done.count("derivable"),
            "warned_answers": len(by_answer),
            "warned_with_wrong": sum(1 for ls in by_answer.values() if "wrong" in ls),
        }
    r, p = res["R"], res["P"]
    r_complete = gate["R"]["answers"] >= gate["R"]["size"]

    def share_cond(s: dict, num: int, den: int, ok) -> bool | None:
        if s["pending"] or not den:
            return None
        return ok(num / den)

    conds = {}
    conds["1_R_holds_3_wrong"] = (True if r["wrong"] >= R_WRONG_MIN else
                                  False if (not r["pending"] and r_complete) else None)
    c2r = share_cond(r, r["warned_with_wrong"], r["warned_answers"],
                     lambda x: x >= WRONG_SHARE_MIN)
    c2p = share_cond(p, p["warned_with_wrong"], p["warned_answers"],
                     lambda x: x >= WRONG_SHARE_MIN)
    conds["2_3_in_10_warned_hold_wrong"] = (False if False in (c2r, c2p) else
                                            None if None in (c2r, c2p) or not r_complete
                                            else True)
    c3r = share_cond(r, r["derivable"], r["rows"], lambda x: x <= DERIVABLE_MAX)
    c3p = share_cond(p, p["derivable"], p["rows"], lambda x: x <= DERIVABLE_MAX)
    conds["3_derivable_at_most_30pc"] = (False if False in (c3r, c3p) else
                                         None if None in (c3r, c3p) or not r_complete else True)
    pr = gate["rates"]["probes"]["rate"]
    rr = gate["rates"]["R"]["rate"]
    conds["4_warned_at_most_20pc"] = (False if (pr is not None and pr > WARN_MAX)
                                      or (r_complete and rr is not None and rr > WARN_MAX)
                                      else True if r_complete else None)
    verdict = ("CLOSE" if False in conds.values() else
               "SHIP" if all(v is True for v in conds.values()) else "PENDING")
    return {"verdict": verdict, "conditions": conds, "R": r, "P": p,
            "R_answers": gate["R"]["answers"], "R_size": gate["R"]["size"]}


def _print_build(g: dict) -> None:
    def r(x):
        return f"{x['warned']}/{x['answers']} = {x['rate']:.1%} ({x['numbers']} numbers)" \
            if x["rate"] is not None else "no answers"
    print(f"R: {g['R']['answers']} of {g['R']['size']} answers "
          f"({g['R']['recorded_to_cutoff']} recorded to {R_RECORDED_TO}); rows {len(g['R']['rows'])}")
    print(f"P: {len(g['P']['rows'])} rows")
    for k, v in g["rates"].items():
        print(f"warned {k:22s} {r(v)}")
    print(f"web answers not checked: {g['web_answers_not_checked']}")
    for a in g["real_flagged"]:
        print(f"  real {a['answer']} {a['model']}: {', '.join(a['numbers'])}")
    for src, s in g["seeded_recall"].items():
        for k, v in s.items():
            if "rate" in v:
                print(f"seeded {src:6s} {k:40s} {v['caught']}/{v['of']} = "
                      f"{v['rate']:.0%}" if v["rate"] is not None else f"seeded {src} {k}: none")
            else:
                print(f"seeded {src:6s} {k:40s} median {v['median']:.0%} p75 {v['p75']:.0%} "
                      f"max {v['max']:.0%}")
    c = g["cost"]
    print(f"cost ms median {c['ms_median']:.2f} p95 {c['ms_p95']:.2f} max {c['ms_max']:.2f}; "
          f"evidence chars median {c['evidence_chars_median']:.0f} max {c['evidence_chars_max']}")
    s = g["session_set"]
    print(f"session set over {s['sessions']} sessions: median {s['median']} p90 {s['p90']} max {s['max']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--narration", type=Path, required=True)
    b.add_argument("--evaluation", type=Path, required=True)
    b.add_argument("--scratch", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True)
    s = sub.add_parser("score")
    s.add_argument("--out", type=Path, required=True)
    rp = sub.add_parser("replay")
    rp.add_argument("--scratch", type=Path, required=True, help="where build copied the narration")
    rp.add_argument("--out", type=Path, required=True)
    rp.add_argument("--model", default=None, help="defaults to SILICA_MODEL, the local chat model")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        _print_build(build(a.narration.expanduser(), a.evaluation, a.scratch, a.out))
    elif a.cmd == "replay":
        from silica.config import CONFIG

        done = replay(a.scratch / "narration", a.out, a.model or CONFIG.model)
        print(f"{len(done)} replays in {a.out / 'replays.json'}")
    else:
        print(json.dumps(score(a.out), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

