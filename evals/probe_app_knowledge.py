# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0115 gate: does the chat answer how the app works once the seed carries
the help panel, and say it does not know what the panel does not cover?

Twenty questions in the pilot's two languages, Italian and Chinese: fifteen the
panel answers, the first four in t3's and t4's words of 2026-10-15
(docs/feedback/2026-10-15-t4.md), and five about things the app does not have,
each checked absent from the tree when written (grep, 2026-10-15). On the
hosted surface Examine is one of the absent ones: the panel there has no row
for it, and an answer that sends a student to it sends them nowhere.

Arms: `current` is the seed before ADR-0115, the TUI's; `app` inserts
`app_message()` after the vault scope, as the GUI seed does since; `app_v1` is
the same with the first closing paragraph. The results of 2026-10-16 also hold
`app_v2` (views written "Learn (#learn)") and `app_v4` (one sentence more on
addresses), texts no arm rebuilds: docs evaluation/app-knowledge-2026-10-16. Each (arm, surface, run) is one subprocess on its own scratch
copy of the test vault and its own HOME, as in probe_prompt_ab, and every
question is a fresh conversation. The model and its provider pin are the
host's (silica-hosted deploy/host.env.example, read 2026-10-15).

Usage:
  uv run python -m evals.probe_app_knowledge --runs 3 --out DIR
  uv run python -m evals.probe_app_knowledge --table --out DIR
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOST_MODEL = "openrouter/qwen/qwen3.7-flash-20260727"
HOST_ENV = {
    "SILICA_PROVIDER": "openrouter",
    "SILICA_MODEL": HOST_MODEL,
    "SILICA_FALLBACK_MODELS": "openrouter/z-ai/glm-5.3-flash",
    "OPENROUTER_PROVIDER_BY_MODEL": json.dumps({
        "qwen/qwen3.7-flash-20260727": {"order": ["alibaba/us-east-1"], "allow_fallbacks": False},
        "z-ai/glm-5.3-flash": {"zdr": True}}),
    "SILICA_MAX_CONTEXT": "32000",
    # Both arms read the same vault the same way; the reranker and the memory
    # lane only add variance to a question about the app.
    "SILICA_RERANK_BASE_URL": "",
    "SILICA_MEMORY": "false",
}
ARMS = ("current", "app_v1", "app")

# The first arm's closing paragraph, run 2026-10-15. It sent the person to the
# help panel for what the panel does not name; the shipped LIMIT replaced it.
LIMIT_V1 = (
    "These rows are everything you know about the app's screens. For a control, "
    "screen or setting they do not name, say you do not know where it is in the app "
    "and point to \"Shortcuts and help\" (the ? key), which also starts the guided "
    "tour; never describe one you were not given here. You cannot press anything in "
    "the app: say where to click, never that you clicked. A view named here can be "
    "linked by its address, as in [Learn](#learn), and the link opens it."
)
SURFACES = ("local", "hosted")

# kind "answer": the panel holds `key`; kind "absent": it holds nothing on it.
# `hosted` overrides a question's kind and key on the hosted surface.
QUESTIONS = [
    {"id": "examine", "lang": "it", "q": "Cosa controlla Examine?",
     "kind": "answer", "key": "Examine shows the vault's health: its shape, what to fix, what changed.",
     "hosted": {"kind": "absent"}},
    {"id": "new_note", "lang": "zh", "q": "如果我每天往 Vault 里添加新的笔记，会更新我的学习计划吗？",
     "kind": "answer", "key": "A new note never changes a plan. The chat reads it and Explore draws it at "
     "once; Learn suggests it only when a plan covers it, or when there is no plan yet."},
    {"id": "where_plans", "lang": "it", "q": "Ho fatto un piano di studio ma non lo trovo più. Dove sono i piani?",
     "kind": "answer", "key": "Study plans are made and kept in Learn; the Calendar shows the day each plan is due."},
    {"id": "add_folder", "lang": "zh", "q": "怎么把一个文件夹添加到 Vault？",
     "kind": "answer", "key": "The vault's + takes a folder; a file dropped anywhere on the window becomes a note in Inbox/."},
    {"id": "newline", "lang": "it", "q": "Come vado a capo nel messaggio senza inviarlo?",
     "kind": "answer", "key": "Shift+Enter makes a new line instead of sending."},
    {"id": "explore", "lang": "zh", "q": "Explore 是做什么用的？",
     "kind": "answer", "key": "Explore is a map of the notes: similar notes sit close, links draw lines."},
    {"id": "settings", "lang": "it", "q": "Come apro le impostazioni?",
     "kind": "answer", "key": "Ctrl + , (comma) opens the settings."},
    {"id": "new_chat", "lang": "zh", "q": "怎么开始一个新的对话？之前的对话会不会消失？",
     "kind": "answer", "key": "New chat at the top of the sidebar starts one; the old one stays in Chats."},
    {"id": "revert", "lang": "it", "q": "Se Silica modifica una nota per sbaglio, come torno indietro?",
     "kind": "answer", "key": "Every write gets a card in the transcript with its own revert (an /undo command is also correct)."},
    {"id": "mic", "lang": "zh", "q": "怎么用语音输入？",
     "kind": "answer", "key": "The mic dictates into the message; on your own machine it needs a transcription "
     "endpoint set in settings.",
     "hosted": {"key": "The mic dictates into the message."}},
    {"id": "3d_2d", "lang": "it", "q": "In Explore che differenza c'è tra 3D e 2D?",
     "kind": "answer", "key": "3D is a spatial layout of the whole vault, a node names itself on hover; in 2D "
     "names appear as you zoom in, biggest notes first, and semantic zones are drawn as hulls."},
    {"id": "calendar", "lang": "zh", "q": "Calendar 里会显示什么？",
     "kind": "answer", "key": "Events, deadlines, and the day each study plan is due."},
    {"id": "this_chat", "lang": "it", "q": "Come vedo quali note ha modificato questa chat?",
     "kind": "answer", "key": "'This chat' lists every note this chat touched; clicking one shows its diff."},
    {"id": "palette", "lang": "zh", "q": "怎么打开命令面板？",
     "kind": "answer", "key": "Typing / opens the command palette, then the up and down arrows pick."},
    {"id": "dictation_lang", "lang": "it", "q": "Come cambio la lingua della dettatura?",
     "kind": "answer", "key": "Settings (ctrl ,), section Language, row 'dictation language'."},
    {"id": "pdf_export", "lang": "it", "q": "Come esporto tutto il vault in PDF?", "kind": "absent"},
    {"id": "share", "lang": "zh", "q": "怎么把一条笔记分享给我的同学？", "kind": "absent"},
    {"id": "pomodoro", "lang": "it", "q": "Come imposto un timer pomodoro per studiare?", "kind": "absent"},
    {"id": "anki", "lang": "zh", "q": "怎么把 Learn 里的题目导出到 Anki？", "kind": "absent"},
    {"id": "password", "lang": "it", "q": "Come cambio la password del mio account?", "kind": "absent"},
]


def spec(q: dict, surface: str) -> dict:
    return {**q, **(q.get("hosted", {}) if surface == "hosted" else {})}


# ------------------------------------------------------------- one run ---

def run_one(arm: str, out_json: Path, cap: int) -> None:
    """Inside the subprocess: SILICA_VAULT, HOME and SILICA_HOSTED are set."""
    import threading

    import silica.cli as cli
    from silica.agent import loop as _loop
    from silica.agent.constraints import AgentConstraints, chat_deferred, chat_tools
    from silica.agent.loop import run_agent
    from silica.tools import TOOLS

    # No live web: an answer that reaches for it is graded on what it says.
    TOOLS["silica_web_answer"].fn = lambda **kw: "The web is not reachable from this session."
    seed = [m for m in cli.seed_messages(math=True) if m.get("content")]
    if arm in ("app", "app_v1"):
        import silica.ui.web.app_help as app_help
        if arm == "app_v1":
            app_help.LIMIT = LIMIT_V1
        from silica.ui.web.app_help import app_message
        seed.insert(3, {"role": "system", "content": app_message()})   # prompt, date, scope, here
    model = os.environ["SILICA_MODEL"]
    surface = "hosted" if os.environ.get("SILICA_HOSTED") == "1" else "local"
    real = _loop.call_llm
    spent = {"prompt": 0, "completion": 0, "calls": 0}
    cancel = threading.Event()

    def metered(model_, messages_, **kw):
        resp = real(model_, messages_, **kw)
        u = resp.usage or {}
        spent["calls"] += 1
        spent["prompt"] += u.get("prompt_tokens") or 0
        spent["completion"] += u.get("completion_tokens") or 0
        if spent["prompt"] > cap:
            cancel.set()
        return resp
    _loop.call_llm = metered
    out = []
    only = set(filter(None, os.environ.get("PROBE_ONLY", "").split(",")))
    for q in (q for q in QUESTIONS if not only or q["id"] in only):
        messages = [dict(m) for m in seed] + [{"role": "user", "content": q["q"]}]
        cancel.clear()
        for k in spent:
            spent[k] = 0
        t0 = time.time()
        try:
            answer = run_agent(messages, model=model, cancel_token=cancel,
                               constraints=AgentConstraints(tools=chat_tools(messages), interactive=True,
                                                            deferred=chat_deferred(messages)))
            error = ""
        except Exception as exc:  # a failed question is recorded and graded as missing, not fatal to the run
            answer, error = "", f"{type(exc).__name__}: {exc}"
        calls = [tc["function"]["name"] for m in messages if m.get("role") == "assistant"
                 for tc in (m.get("tool_calls") or []) if isinstance(tc, dict) and tc.get("function")]
        out.append({"id": q["id"], "q": q["q"], "answer": answer or "", "error": error,
                    "aborted": cancel.is_set(), "tools": calls, "seconds": round(time.time() - t0, 1),
                    **spent})
    out_json.write_text(json.dumps({"arm": arm, "surface": surface, "model": model, "questions": out},
                                   ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------- orchestrate ---

def orchestrate(runs: int, out: Path, jobs: int, cap: int) -> None:
    from evals.probe_prompt_ab import fresh_home, reset_vault

    out.mkdir(parents=True, exist_ok=True)
    todo = [(a, s, r) for r in range(1, runs + 1) for a in ARMS for s in SURFACES
            if not (out / f"{a}.{s}.{r}.json").exists()]
    # Not /tmp: it is RAM here, and twelve vault copies with their index are not.
    scratch_root = Path(os.environ.get("PROBE_SCRATCH", "~/.cache/silica-probe-app-knowledge")).expanduser()

    def one(job):
        arm, surface, run = job
        tag = f"{arm}.{surface}.{run}"
        vault, home = scratch_root / tag / "vault", fresh_home(scratch_root / tag / "home")
        reset_vault(vault, None, home)
        env = {**os.environ, **HOST_ENV, "HOME": str(home), "SILICA_VAULT": str(vault),
               "SILICA_HOSTED": "1" if surface == "hosted" else ""}
        t0 = time.time()
        p = subprocess.run([sys.executable, "-m", "evals.probe_app_knowledge", "--one", arm,
                            "--out", str(out / f"{tag}.json"), "--cap", str(cap)],
                           env=env, capture_output=True, text=True)
        (out / f"{tag}.log").write_text(p.stdout + p.stderr, encoding="utf-8")
        shutil.rmtree(scratch_root / tag, ignore_errors=True)
        print(f"{tag}: rc {p.returncode} in {time.time() - t0:.0f}s", flush=True)

    with ThreadPoolExecutor(jobs) as ex:
        list(ex.map(one, todo))


# ---------------------------------------------------------------- check ---

# Each answerable question is passed by the facts its panel row states, every
# group matched, in the three languages the answers came in. An LLM judge ran
# first (deepseek-v4-flash, 2026-10-16) and was dropped: a change to its own
# prompt moved one run from 14 to 9 of 15, and it graded the documented #explore
# link as invented. A rule can be read and argued with; a judge's verdict cannot.
MUST = {
    "examine": [r"(?i)salute|health|健康"],
    "new_note": [r"(?i)non (si )?(cambia|modifica|aggiorna|tocca)[^.。]{0,40}(pian|automatic)|restano identic|never changes|"
                 r"不会[^。]{0,12}(更新|改变|修改|影响)[^。]{0,12}计划|计划[^。]{0,12}不会|不会自动更新"],
    "where_plans": [r"#learn|\bLearn\b(?!\.md)"],
    # The vault's +, quoted or named as a button: "Inbox + nucleazione" and
    # "ctrl + ," are not it.
    "add_folder": [r"\*\*\+\*\*|`\+`|[\"“「]\+[\"”」]|\+\s*(号|del vault|按钮|button|pulsante)|"
                   r"(pulsante|button|按钮)\s*\*{0,2}\+|vault's \+|旁的 \+"],
    "newline": [r"(?i)shift|⇧|maiusc"],
    "explore": [r"(?i)mapp?a|map\b|地图"],
    "settings": [r"(?i)ctrl\W{0,6}(,|，|virgola|comma|逗号)"],
    # The labels themselves: "新对话" and "不会消失" are in the question.
    "new_chat": [r"(?i)new chat", r"\bChats\b"],
    "revert": [r"(?i)revert|/undo|annull|ripristin"],
    # The app's mic dictates into the message; a phone keyboard's mic does not.
    "mic": [r"(?i)\bmic|microfono|麦克风|话筒|🎤|🎙",
            r"(?i)nel (campo del )?messaggio|into the message|(入|到)消息|消息(框|栏|中|里)|输入框|"
            r"campo (del messaggio|di input|di digitazione)|barra (della chat|dei messaggi|di digitazione)|nella chat"],
    "3d_2d": [r"(?i)zoom|缩放", r"(?i)spazial|spatial|空间|intero vault|whole vault"],
    "calendar": [r"(?i)event|事件|活动|日程", r"(?i)deadline|scadenz|截止"],
    "this_chat": [r"(?i)this chat|questa chat", r"(?i)diff"],
    # The / key, quoted or pressed; not the slash of "Windows / Linux".
    "palette": [r"`/`|\*\*/\*\*|“/”|\"/\"|「/」|按\s*`?/|press\s*`?/|premi\s*(il tasto\s*)?\*{0,2}`?/|"
                r"type\s*`?/|digita\s*`?/"],
    # The row and its section: "lingua della dettatura" alone is the question.
    "dictation_lang": [r"(?i)dictation language|dettat|dettaz|听写", r"\bLanguage\b"],
}
# An unanswerable question passes when the answer says the app has no such
# thing, or that it does not know of one.
SAYS_NO = re.compile(
    r"(?i)non (esiste|c'è|c’è|ho |trovo|vedo|risulta|è (prevista|disponibile|presente|documentat|supportat)|so |"
    r"conosco|offre|supporta|prevede|ha |include|menziona|fornisce|espone|documenta|descrive|appare|compare)|"
    r"non è (descritt|indicat|menzionat|elencat)|nessun|alcuna (opzione|funzion)|"
    r"没有|不知道|未(提及|提供|找到|提到)|并未|不支持|不提供|不清楚|无法|不包含|"
    r"not (available|supported|documented)|no (such|built-in)|doesn't|does not|don't know")
# Informational: an unanswerable answer that still sends the person to look in
# the help or the settings, which the first closing paragraph asked for.
SENDS_TO_LOOK = re.compile(r"(?i)\?\s*(\*\*|`)?\s*(key|tasto|键)|premi\s*\*{0,2}`?\?|按\s*`?\?|ctrl\s*\+?\s*,|impostazioni|设置")


def passed(rec: dict, surface: str) -> bool:
    s = spec(next(q for q in QUESTIONS if q["id"] == rec["id"]), surface)
    a = rec["answer"]
    if surface == "hosted" and re.search(r"#examine|#metrics", a):
        return False
    if s["kind"] == "answer":
        return all(re.search(p, a) for p in MUST[rec["id"]])
    return bool(SAYS_NO.search(a))


def table(out: Path) -> str:
    rows = ["| arm | surface | run | answerable | unanswerable: says so | of those, sends to look | prompt tok/q |",
            "|---|---|---|---|---|---|---|"]
    per_q: dict[tuple, list[bool]] = {}
    for f in sorted(out.glob("*.*.*.json")):
        arm, surface, run = f.name.split(".")[:3]
        qs = json.loads(f.read_text(encoding="utf-8"))["questions"]
        kind = {q["id"]: spec(q, surface)["kind"] for q in QUESTIONS}
        ans = [r for r in qs if kind[r["id"]] == "answer"]
        absent = [r for r in qs if kind[r["id"]] == "absent"]
        for r in qs:
            per_q.setdefault((arm, surface, r["id"]), []).append(passed(r, surface))
        look = sum(1 for r in absent if SENDS_TO_LOOK.search(r["answer"]))
        tok = round(sum(r["prompt"] for r in qs) / len(qs))
        rows.append(f"| {arm} | {surface} | {run} | {sum(passed(r, surface) for r in ans)}/{len(ans)} "
                    f"| {sum(passed(r, surface) for r in absent)}/{len(absent)} | {look} | {tok} |")
    rows += ["", "| arm | surface | question | passed runs |", "|---|---|---|---|"]
    rows += [f"| {a} | {s} | {q} | {sum(v)}/{len(v)} |" for (a, s, q), v in sorted(per_q.items())]
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--cap", type=int, default=80000, help="prompt tokens per question before cancel")
    ap.add_argument("--one", choices=ARMS)
    ap.add_argument("--table", action="store_true")
    a = ap.parse_args()
    if a.one:
        run_one(a.one, a.out, a.cap)
    elif a.table:
        print(table(a.out))
    else:
        orchestrate(a.runs, a.out, a.jobs, a.cap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
