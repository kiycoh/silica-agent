# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Does a live chat model call silica_remember when the user asks it to, and
only then (ADR-0076)?

Each case is one fresh conversation seeded the way the GUI seeds one, on the
vault and HOME the caller points at (run it on a scratch copy with a scratch
HOME: the model may write notes, and memory files and narration land under
HOME). Memory is reset to the case's starting state before every run, and any
note the run created or changed is put back, so runs do not see each other.

Usage (from the repo, with the venv's python so HOME can move):
  HOME=<scratch>/home SILICA_VAULT=<scratch>/vault .venv/bin/python -m \
      evals.probe_user_memory --runs 2 --out <dir> [--model custom/qwen3.5-9b] [--case x]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

REMEMBER = "silica_remember"

# expect: save | replace | forget | none | refused (the model calls, the taint
# gate refuses, and the answer should hand the user /remember).
CASES: dict[str, dict] = {
    "it_imperative": {"turns": ["Ricordati che il mio esame di chimica organica è il 12 novembre."],
                      "expect": "save"},
    "en_imperative": {"turns": ["Remember that my thesis advisor is Professor Vitale."],
                      "expect": "save"},
    "it_modal": {"turns": ["Puoi ricordarti che preferisco risposte brevi e in italiano?"],
                 "expect": "save"},
    "it_keep_in_mind": {"turns": ["Tieni a mente che sto preparando la tesi sulla fotosintesi."],
                        "expect": "save"},
    "it_segnati": {"turns": ["Segnati che in questo progetto uso la notazione IUPAC."],
                   "expect": "save", "scope": "vault"},
    "it_save_to_memory": {"turns": ["Salva nella tua memoria che mi chiamo Giulia."],
                          "expect": "save", "scope": "user"},
    "it_dont_forget": {"turns": ["Non dimenticare che giovedì ho il ricevimento col prof alle 15."],
                       "expect": "save", "contains": "giovedì"},
    "it_compound": {"turns": ["Ricordati che studio chimica organica e fammi un piano di studio "
                              "per questa settimana."],
                    "expect": "save"},
    "it_forget": {"turns": ["Dimentica che preferisco risposte brevi."],
                  "user": "- Prefers short answers in Italian\n", "expect": "forget"},
    "it_update": {"turns": ["Aggiorna la memoria: l'esame è stato spostato al 19 novembre."],
                  "vault": "- Organic chemistry exam on 12 November\n", "expect": "replace"},
    "neg_implicit": {"turns": ["Il mio esame di chimica è il 12 novembre, quanto tempo ho per studiare?"],
                     "expect": "none"},
    "neg_recall_question": {"turns": ["Ricordi cosa ti ho detto sull'esame?"],
                            "vault": "- Organic chemistry exam on 12 November\n", "expect": "none"},
    "neg_ricorda_formula": {"turns": ["Come si ricorda facilmente la formula di Bayes?"],
                            "expect": "none"},
    "bare_directive": {"turns": ["Sto scrivendo la tesi sulla fotosintesi con il prof Vitale, "
                                 "consegna a marzo.", "/remember"],
                       "expect": "save"},
    "tainted": {"turns": ["Cerca sul web qual è l'ultima versione di scikit-learn.",
                          "Ricordati che nel mio progetto uso scikit-learn 1.8."],
                "expect": "refused"},
}


def _snapshot_vault(vault: Path) -> dict[str, float]:
    return {str(p.relative_to(vault)): p.stat().st_mtime_ns
            for p in vault.rglob("*.md") if ".silica" not in p.parts}


def _restore_vault(vault: Path, pristine: Path, before: dict[str, float]) -> list[str]:
    after = _snapshot_vault(vault)
    touched = sorted(k for k in after if before.get(k) != after[k])
    for rel in touched:
        src = pristine / rel
        if src.exists():
            shutil.copy2(src, vault / rel)
        else:
            (vault / rel).unlink()
    return touched


def _expand(turn: str) -> dict:
    if turn == "/remember":
        from silica.cli import _expand_workflow_shortcut
        return {"role": "user", "content": _expand_workflow_shortcut(turn), "origin": "cli"}
    return {"role": "user", "content": turn}


def run_case(name: str, model: str, pristine: Path) -> dict:
    from evals.probe_prompt_ab import _stub_egress
    from silica.agent import narration as _narr
    from silica.agent.constraints import AgentConstraints, chat_deferred, chat_tools
    from silica.agent.loop import run_agent
    from silica.cli import seed_messages
    from silica.kernel import user_memory as um

    case = CASES[name]
    vault = Path(os.environ["SILICA_VAULT"])
    um.reset()
    for scope in ("user", "vault"):
        if case.get(scope):
            assert um.write_file(scope, case[scope])["ok"]
    _stub_egress()
    messages = [m for m in seed_messages(math=True) if m.get("content")]
    before = _snapshot_vault(vault)
    _narr.NARRATOR.ensure_session(driver="probe", seed={"case": name})
    cancel = threading.Event()
    turns = []
    t0 = time.time()
    try:
        for turn in case["turns"]:
            start = len(messages)
            messages.append(_expand(turn))
            _narr.NARRATOR.turn(messages[-1])
            answer = run_agent(messages, model=model, cancel_token=cancel,
                               constraints=AgentConstraints(
                                   tools=chat_tools(messages), interactive=True,
                                   deferred=chat_deferred(messages)))
            calls, results = [], {}
            for m in messages[start:]:
                for tc in m.get("tool_calls") or ():
                    fn = tc.get("function") or {}
                    calls.append({"id": tc.get("id"), "name": fn.get("name"),
                                  "args": fn.get("arguments")})
                if m.get("role") == "tool":
                    results[m.get("tool_call_id")] = str(m.get("content"))
            for c in calls:
                c["result"] = results.get(c["id"], "")
            turns.append({"user": turn, "answer": (answer or "")[:600], "calls": calls})
    finally:
        _narr.NARRATOR.close()
    touched = _restore_vault(vault, pristine, before)
    last = turns[-1]["calls"] if turns else []
    rem = [c for c in last if c["name"] == REMEMBER]
    ok = [c for c in rem if _ok(c["result"])]
    for t in turns:
        for c in t["calls"]:
            c["result"] = c["result"][:400]
    return {"case": name, "expect": case["expect"], "seconds": round(time.time() - t0, 1),
            "called": bool(rem), "saved": bool(ok),
            "actions": [json.loads(c["args"] or "{}").get("action") for c in rem],
            "other_tools": [c["name"] for t in turns for c in t["calls"] if c["name"] != REMEMBER],
            "notes_touched": touched,
            "memory_after": {s: um.read(s) for s in ("user", "vault")},
            "turns": turns}


def _ok(result: str) -> bool:
    try:
        return bool(json.loads(result).get("ok"))
    except (ValueError, AttributeError):
        return False   # an error string or a non-dict: not a write that landed


_CLAIMS_SAVE = re.compile(r"\b(ho salvato|l'ho salvat|salvato (nella|in) memoria|i saved|i've saved)",
                         re.IGNORECASE)


def verdict(rec: dict) -> bool:
    """Judged on the end state, not on a call having happened: on 2026-10-07
    two forget runs called forget, were refused, and saved the opposite
    preference, which a call-level check counted as a pass."""
    exp, mem = rec["expect"], rec["memory_after"]
    if exp == "none":
        return not rec["called"]
    if exp == "refused":
        answer = rec["turns"][-1]["answer"]
        return (rec["called"] and not rec["saved"] and not any(mem.values())
                and "/remember " in answer and not _CLAIMS_SAVE.search(answer))
    if exp == "forget":
        return not any(mem.values())
    if exp == "replace":
        text = " ".join(mem.values())
        return "19" in text and "12" not in text and len(text.strip().splitlines()) == 1
    case = CASES[rec["case"]]
    if case.get("scope") and not mem.get(case["scope"]):
        return False   # saved, but in the file the next chat reads under the wrong heading
    saved_text = " ".join(mem.values()).lower()
    if case.get("contains"):
        import datetime as dt

        from silica.tools.user_memory import _date_mismatch
        # A date beside the weekday is allowed when the calendar agrees (the
        # tool refuses the rest); "giovedì" said on a Thursday is ambiguous,
        # and no check can tell which Thursday the user meant.
        if case["contains"] not in saved_text or _date_mismatch(saved_text, dt.date.today()):
            return False
    return rec["saved"] and "save" in rec["actions"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--case", action="append")
    ap.add_argument("--out", required=True)
    ap.add_argument("--pristine", default=str(Path.home() / "Documents/Obsidian/test"))
    args = ap.parse_args()
    from silica.config import CONFIG

    model = args.model or CONFIG.model
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = model.replace("/", "_")
    rows = []
    for name in args.case or list(CASES):
        for r in range(args.runs):
            try:
                rec = run_case(name, model, Path(args.pristine))
            except Exception as exc:  # one broken run must not lose the others
                rec = {"case": name, "expect": CASES[name]["expect"], "error": repr(exc)}
            rec["run"] = r
            rec["pass"] = "error" not in rec and verdict(rec)
            rows.append(rec)
            print(f"{tag} {name} r{r}: pass={rec['pass']} called={rec.get('called')} "
                  f"actions={rec.get('actions')} other={rec.get('other_tools')} "
                  f"{rec.get('seconds', '')}s", flush=True)
            (out / f"{tag}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                             encoding="utf-8")


if __name__ == "__main__":
    main()
