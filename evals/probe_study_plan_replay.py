# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Fixed-context replay of one chat turn from its narration file.

Why this exists: the study-plan answer of 2026-09-22 (narration 5459ab327c30)
cited five wikilinks that resolve to nothing, attributed Lezione 1's textbook
list to the course sheet, and dropped the CNN lecture it had been handed. Each
fix candidate (mark unresolved links, deliver a note whole, a prompt rule, a
different model) changes ONE thing; a live re-run changes retrieval and the
model's tool plan too. The narration holds every tool result verbatim, so the
final synthesis call can be rebuilt and re-issued with one substitution at a
time (Astra's review, 2026-09-22: same question, same model, one variable).

Checks are deterministic string/regex tests on four separate axes (facts,
attributions, links, prerequisites). Attribution judgments that need reading
stay manual; the .md written next to each result is for that.

Usage:
  uv run python -m evals.probe_study_plan_replay \\
      --narration ~/.silica/narration/5459ab327c30.jsonl \\
      --variant baseline --variant links --out docs/evaluation/study-plan-replay
Variants compose with '+': --variant links+dedup+attrib_rule
"""
from __future__ import annotations

import argparse
import collections
import copy
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------- gold ------
# One question, one project: the gold is what the mail and the project's own
# argomenti_lezione_toccati.md make NECESSARY, not the whole syllabus (Astra:
# an omission is an error only when the path becomes insufficient).
# Each entry: name -> (regex, want) with want True = must appear, False = must
# not appear, None = manual (flagged, never scored).
FACTS = {
    "sensor_cols_242": (r"\b242\b", True),
    "sensor_cols_243_wrong": (r"\b243\b", False),
    "two_labels_forced_wrong": (r"due etichette|entrambe le etichette", False),
}
ATTRIBUTIONS = {
    "hastie_named": (r"Hastie", True),
    "castelli_is_big_data": (r"Tecnologie per i Big Data", True),
    "embedding_lstm_wrong": (r"embedding.{0,40}LSTM|LSTM.{0,40}embedding", False),
    "sklearn_params_manual": (r"max_depth|min_samples", None),
}
# Paragraph co-occurrence: (regex A, regex B) both in one paragraph = wrong.
ATTRIBUTION_PAIRS = {
    "berthold_to_scheda_wrong": (r"scheda", r"Berthold"),
    "castelli_as_ida_wrong": (r"Castelli", r"Intelligent Data Analysis"),
}
PREREQS_NECESSARY = {
    "cnn": r"convolu|\bCNN\b",
    "mlp": r"\bMLP\b|feedforward|multistrato|percettrone multi",
    "nn_regularisation": r"dropout|early stopping",
    "lda_theory_lectures": r"Lezion[ei]\s*(8|9|10)\b",
    "subject_split": r"soggett|leakage|per sessione|per persona",
}
PREREQS_OPTIONAL = {
    "autoencoder": r"autoencoder|autocodific",
    "lezione_29": r"Lezione 29",
}
DA_NOTE = "Analisi del discriminante (DA)"

WIKILINK = re.compile(r"(?<!!)\[\[([^\]|#\n]+)(?:#[^\]|\n]*)?(?:\|[^\]\n]*)?\]\]")
BLOCK_HEAD = re.compile(r"^\[#(\d+) \| ([^|\]\n]+)((?: \| [^\]\n]*)?)\]$", re.M)
NO_NOTE = " (no such note)"
LEGEND = ("[legend: a wikilink followed by \"(no such note)\" names a note that does "
          "not exist in this vault; link only notes you saw delivered]\n\n")

# ------------------------------------------------------------ narration -----

def load_turn(narr_path: Path) -> tuple[list[dict], str, str]:
    """(messages up to the last tool result, original answer, vault path)."""
    rows = [json.loads(line) for line in narr_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    vault = next((r["payload"].get("vault") for r in rows if r["kind"] == "session"), "")
    turns = [r["payload"]["message"] for r in rows if r["kind"] == "turn"]
    original = ""
    if turns and turns[-1]["role"] == "assistant" and not turns[-1].get("tool_calls"):
        original = turns[-1].get("content") or ""
        turns = turns[:-1]
    # The forced final-turn system notice never fired on this run; a replay
    # that carried one would not be the same prompt.
    turns = [t for t in turns if t["role"] != "system"]
    return turns, original, vault


def _tool_name(messages: list[dict], tool_msg: dict) -> str:
    cid = tool_msg.get("tool_call_id")
    for m in messages:
        for tc in m.get("tool_calls") or []:
            if tc.get("id") == cid:
                return tc["function"]["name"]
    return ""


# ------------------------------------------------------------- variants -----

class Ctx:
    """Vault access for the variants; built once per process."""

    def __init__(self) -> None:
        from silica.driver import DRIVER
        from silica.kernel.recall.mindmap import note_resolver
        from silica.kernel.write import frontmatter
        self.resolve = note_resolver()
        self._driver = DRIVER
        self._fm = frontmatter
        self._bodies: dict[str, str] = {}

    def body(self, path: str) -> str:
        if path not in self._bodies:
            p = path if path.endswith(".md") else path + ".md"
            try:
                content = self._driver.read_note(p).content or ""
            except Exception:
                content = ""
            _d, _r, body = self._fm.split(content)
            self._bodies[path] = body or ""
        return self._bodies[path]

    def title(self, path: str) -> str:
        m = re.search(r"^# (.+?)\s*$", self.body(path), re.M)
        return m.group(1).strip() if m else ""


def _recall_msgs(messages: list[dict]):
    for m in messages:
        if m.get("role") == "tool" and _tool_name(messages, m) == "silica_recall":
            yield m


def _edit_context(m: dict, fn) -> None:
    d = json.loads(m["content"])
    d["context"] = fn(d.get("context") or "")
    m["content"] = json.dumps(d, ensure_ascii=False)


def v_links(messages: list[dict], ctx: Ctx) -> None:
    """Mark every wikilink whose target does not resolve, in recall contexts
    and search_context snippets. The brackets stay: the model must see that a
    note named this way does not exist, not merely lose the link."""
    def mark(text: str) -> str:
        def sub(m: re.Match) -> str:
            target = m.group(1).strip()
            return m.group(0) if ctx.resolve(target) else m.group(0) + NO_NOTE
        return WIKILINK.sub(sub, text)
    for m in messages:
        if m.get("role") != "tool":
            continue
        name = _tool_name(messages, m)
        if name == "silica_recall":
            _edit_context(m, lambda c: LEGEND + mark(c))
        elif name == "silica_search_context":
            m["content"] = mark(m["content"])


def v_dedup(messages: list[dict], ctx: Ctx) -> None:
    """The product's per-turn collapse (compaction.dedup_recall_excerpts),
    applied to the recorded recalls in order."""
    from silica.agent.compaction import dedup_recall_excerpts
    seen: set[str] = set()
    for m in _recall_msgs(messages):
        m["content"] = dedup_recall_excerpts(m["content"], seen)


def v_scheda_whole(messages: list[dict], ctx: Ctx) -> None:
    """The course sheet's first block carries the whole note (12 KB): the
    'TESTI CONSIGLIATI' cell never made it into any of its five windows."""
    path = "Inbox/scheda_trasparenza_173558"
    done = False

    def fn(c: str) -> str:
        nonlocal done
        if done:
            return c
        heads = list(BLOCK_HEAD.finditer(c))
        for i, h in enumerate(heads):
            if h.group(2).strip() != path:
                continue
            end = heads[i + 1].start() if i + 1 < len(heads) else len(c)
            done = True
            return c[:h.end()] + "\n" + ctx.body(path).strip() + "\n\n---\n\n" + c[end:]
        return c
    for m in _recall_msgs(messages):
        _edit_context(m, fn)


def v_title_header(messages: list[dict], ctx: Ctx) -> None:
    """Block headers carry an identity when the path stem has none:
    'Inbox/giovanni_castelli' says nothing, 'Tecnologie per i Big Data' does.
    The first H1 stands in for the `title:` an import would declare; the
    product reads frontmatter only (the H1 rule mislabels 372 of 728 notes)."""
    def fn(c: str) -> str:
        def sub(m: re.Match) -> str:
            path = m.group(2).strip()
            title = ctx.title(path)
            stem = path.rsplit("/", 1)[-1]
            if not title or title.lower() == stem.lower():
                return m.group(0)
            return f"[#{m.group(1)} | {path} | title: {title}{m.group(3)}]"
        return BLOCK_HEAD.sub(sub, c)
    for m in _recall_msgs(messages):
        _edit_context(m, fn)


ATTRIB_RULE = (
    "Before you answer, keep four things apart and say which is which: what the "
    "professor's message asks, what the notes say, facts you verified about the "
    "data, and your own suggestions. Attribute a statement to a note only if the "
    "excerpt you received shows it; if the part that would support it was not "
    "delivered, say that you did not see it instead of completing it from memory."
)
LINK_RULE = (
    "Link a note with [[...]] only if it exists in this vault: a note you saw in a "
    "recall header or read yourself. A [[link]] inside another note's text is not "
    "evidence that its target exists; name that concept without brackets."
)


def _append_system(messages: list[dict], text: str) -> None:
    messages.append({"role": "system", "content": text})


def v_attrib_rule(messages: list[dict], ctx: Ctx) -> None:
    _append_system(messages, ATTRIB_RULE)


def v_link_rule(messages: list[dict], ctx: Ctx) -> None:
    _append_system(messages, LINK_RULE)


VARIANTS = {
    "baseline": lambda messages, ctx: None,
    "links": v_links,
    "link_rule": v_link_rule,
    "dedup": v_dedup,
    "scheda_whole": v_scheda_whole,
    "title_header": v_title_header,
    "attrib_rule": v_attrib_rule,
}


def apply_variant(name: str, messages: list[dict], ctx: Ctx) -> list[dict]:
    msgs = copy.deepcopy(messages)
    for part in name.split("+"):
        if part not in VARIANTS:
            raise SystemExit(f"unknown variant {part!r}; known: {', '.join(VARIANTS)}")
        VARIANTS[part](msgs, ctx)
    return msgs


# --------------------------------------------------------------- checks -----

def _paragraphs(text: str) -> list[str]:
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


def check_answer(answer: str, ctx: Ctx) -> dict:
    out: dict = {"facts": {}, "attributions": {}, "links": {}, "prereqs": {}}
    for name, (rx, want) in FACTS.items():
        out["facts"][name] = _score(rx, want, answer)
    for name, (rx, want) in ATTRIBUTIONS.items():
        out["attributions"][name] = _score(rx, want, answer)
    for name, (a, b) in ATTRIBUTION_PAIRS.items():
        hit = any(re.search(a, p, re.I) and re.search(b, p, re.I) for p in _paragraphs(answer))
        out["attributions"][name] = {"hit": hit, "ok": not hit}
    targets = [m.group(1).strip() for m in WIKILINK.finditer(answer)]
    dead = sorted({t for t in targets if not ctx.resolve(t)})
    out["links"] = {"total": len(targets), "distinct": len(set(targets)), "dead": dead,
                    "dead_count": len(dead), "da_cited": DA_NOTE.lower() in answer.lower()}
    nec = {n: bool(re.search(rx, answer, re.I)) for n, rx in PREREQS_NECESSARY.items()}
    opt = {n: bool(re.search(rx, answer, re.I)) for n, rx in PREREQS_OPTIONAL.items()}
    out["prereqs"] = {"necessary": nec, "necessary_hit": sum(nec.values()),
                      "necessary_total": len(nec), "optional": opt}
    out["summary"] = {
        "facts_ok": sum(1 for v in out["facts"].values() if v.get("ok") is True),
        "facts_scored": sum(1 for v in out["facts"].values() if v.get("ok") is not None),
        "attrib_ok": sum(1 for v in out["attributions"].values() if v.get("ok") is True),
        "attrib_scored": sum(1 for v in out["attributions"].values() if v.get("ok") is not None),
        "dead_links": len(dead),
        "da_cited": out["links"]["da_cited"],
        "prereqs": f"{sum(nec.values())}/{len(nec)}",
    }
    return out


def _score(rx: str, want, text: str) -> dict:
    hit = bool(re.search(rx, text, re.I))
    return {"hit": hit, "ok": None if want is None else (hit == want)}


# ------------------------------------------------------------------ run -----

def run_variant(name: str, base: list[dict], seed: list[dict], ctx: Ctx, model: str,
                tools, payload_path: Path | None = None) -> dict:
    from silica.agent.llm import call_llm
    from silica.agent.providers import _to_wire
    msgs = seed + apply_variant(name, base, ctx)
    if payload_path is not None:
        # The exact bytes the provider sees: 63014 prompt tokens equal to the
        # recorded call is evidence, not identity (Astra, 2026-09-22).
        wire = {"messages": [_to_wire(m) for m in msgs], "tools": tools}
        raw = json.dumps(wire, ensure_ascii=False, sort_keys=True)
        payload_path.write_text(raw, encoding="utf-8")
    resp = call_llm(model, msgs, tools=tools)
    answer = resp.text or ""
    u = resp.usage or {}
    return {"variant": name, "model": model, "answer": answer,
            "finish_reason": resp.finish_reason,
            "tool_calls": [tc.name for tc in (resp.tool_calls or [])],
            "prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"),
            "checks": check_answer(answer, ctx)}


def _md(rec: dict) -> str:
    c = rec["checks"]
    lines = [f"# {rec['variant']} · {rec['model']}", "",
             f"prompt_tokens {rec['prompt_tokens']} · completion {rec['completion_tokens']} · "
             f"finish {rec['finish_reason']} · tool_calls {rec['tool_calls']}", "",
             "## Checks", "", "| axis | check | hit | ok |", "|---|---|---|---|"]
    for axis in ("facts", "attributions"):
        for k, v in c[axis].items():
            lines.append(f"| {axis} | {k} | {v['hit']} | {v['ok']} |")
    for k, v in c["prereqs"]["necessary"].items():
        lines.append(f"| prereq necessary | {k} | {v} | {v} |")
    for k, v in c["prereqs"]["optional"].items():
        lines.append(f"| prereq optional | {k} | {v} | |")
    lines += ["", f"dead links ({c['links']['dead_count']}/{c['links']['distinct']} distinct): "
              + ", ".join(f"[[{d}]]" for d in c["links"]["dead"]),
              f"DA cited: {c['links']['da_cited']}", "", "## Answer", "", rec["answer"]]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--narration", required=True)
    ap.add_argument("--vault", default=None, help="defaults to the narration's session vault")
    ap.add_argument("--model", default=None, help="defaults to SILICA_MODEL")
    ap.add_argument("--variant", action="append", default=None)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--with-tools", action="store_true",
                    help="offer the chat tool schemas (a call is reported, not executed)")
    ap.add_argument("--out", default="docs/evaluation/study-plan-replay")
    ap.add_argument("--check-only", default=None,
                    help="score this answer file instead of calling the model")
    ap.add_argument("--rescore", action="store_true",
                    help="re-score every saved run in --out with the v2 scorer and print the table")
    ap.add_argument("--save-payload", action="store_true",
                    help="write the exact wire messages and tool schemas of each call next to its result")
    args = ap.parse_args(argv)

    narr = Path(args.narration).expanduser()
    base, original, vault = load_turn(narr)
    vault = args.vault or vault
    if vault:
        os.environ["SILICA_VAULT"] = vault  # before any silica import
    from silica.config import CONFIG
    from evals._shared import git_sha, run_id

    ctx = Ctx()
    if args.rescore:
        rescore(Path(args.out), ctx)
        return 0
    if args.check_only:
        text = Path(args.check_only).read_text(encoding="utf-8")
        print(json.dumps(check_answer(text, ctx), ensure_ascii=False, indent=1))
        return 0

    from silica.cli import seed_messages, _chat_tool_schemas
    seed = seed_messages(math=True)
    tools = _chat_tool_schemas(seed) if args.with_tools else None
    model = args.model or CONFIG.model
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rid = run_id()
    import subprocess
    tree_diff = subprocess.run(["git", "diff", "HEAD"], capture_output=True, text=True).stdout
    prov = {"run_id": rid, "git_sha": git_sha(), "narration": str(narr),
            "narration_sha256": hashlib.sha256(narr.read_bytes()).hexdigest(), "vault": vault,
            # An uncommitted tree is not identified by HEAD: the diff's digest is.
            "worktree_diff_sha256": hashlib.sha256(tree_diff.encode()).hexdigest(),
            "model": model, "with_tools": bool(tools)}

    # The recorded answer is scored too: the harness must reproduce the error
    # profile it was built for before any variant means anything.
    orig_checks = check_answer(original, ctx)
    print("original answer:", json.dumps(orig_checks["summary"], ensure_ascii=False))

    per_variant: dict[str, list[dict]] = {}
    for name in (args.variant or ["baseline"]):
        for i in range(args.runs):
            slug = re.sub(r"[^a-z0-9]+", "-", f"{name}-{model}".lower()).strip("-")
            stem = out_dir / f"{slug}-{rid}-r{i}"
            try:
                rec = run_variant(name, base, seed, ctx, model, tools,
                                  stem.with_suffix(".payload.json") if args.save_payload else None)
            except Exception as e:  # a transient provider error (SSL EOF seen
                # 2026-09-22 at call 16 of 24) must cost one run, not the batch
                print(f"{name} r{i}: FAILED {type(e).__name__}: {str(e)[:120]}", flush=True)
                continue
            rec["provenance"] = prov
            rec["run"] = i
            if args.save_payload:
                rec["payload_sha256"] = hashlib.sha256(stem.with_suffix(".payload.json").read_bytes()).hexdigest()
            rec["checks_v2"] = score_v2(rec, ctx)
            stem.with_suffix(".json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
            stem.with_suffix(".md").write_text(_md(rec), encoding="utf-8")
            s = rec["checks"]["summary"]
            per_variant.setdefault(name, []).append(rec)
            print(f"{name} r{i}: {json.dumps(s, ensure_ascii=False)} "
                  f"prompt={rec['prompt_tokens']} finish={rec['finish_reason']} "
                  f"tools={rec['tool_calls']} -> {stem.name}.md", flush=True)
    # Rates over the answered runs: one run is a sample of the model, not a
    # verdict (Astra, 2026-09-22). A run that asked for tools instead of
    # answering is counted apart, never scored as an empty answer.
    print("\n| variant | answered | dead links (mean) | DA cited | facts ok | attrib ok | necessary prereqs |")
    print("|---|---|---|---|---|---|---|")
    for name, recs in per_variant.items():
        answered = [r for r in recs if r["answer"].strip() and not r["tool_calls"]]
        n = len(answered)
        if not n:
            print(f"| {name} | 0/{len(recs)} | | | | | |")
            continue
        summ = [r["checks"]["summary"] for r in answered]
        dead = sum(x["dead_links"] for x in summ) / n
        da = sum(1 for x in summ if x["da_cited"])
        facts = sum(x["facts_ok"] for x in summ) / n
        attrib = sum(x["attrib_ok"] for x in summ) / n
        pre = sum(int(x["prereqs"].split("/")[0]) for x in summ) / n
        print(f"| {name} | {n}/{len(recs)} | {dead:.1f} | {da}/{n} | {facts:.1f}/3 | {attrib:.1f}/5 | {pre:.1f}/5 |")
    return 0



# ------------------------------------------------------- scorer v2 ---------
# Astra's review of the first table (2026-09-22): an empty answer scored 2/3 on
# facts because it contained none of the forbidden forms, and "4/5 prereqs"
# counted lecture numbers, not correct attributions. Every check now yields
# one of four outcomes, links split three ways, and every "Lezione N + topic"
# pair on one line is verified against the lecture file itself.

OUTCOMES = ("correct", "wrong", "addressed_unverified", "not_addressed")
# name -> (topic regex, wrong regex or None, right regex or None,
#          paragraph pair (A, B) that marks WRONG, or None)
CLAIMS = {
    "sensor_columns": (r"\b(242|243|250)\b|colonne|canali|variabili del dataset",
                       r"\b243\b", r"\b242\b", None),
    "labels_and_or": (r"etichett|\blabel|locomozion|gesti\b",
                      r"due etichette|entrambe le etichette", None, None),
    "textbooks_of_sheet": (r"Bishop|Goodfellow|Cristianini|Hastie|Berthold|libri di testo|testi consigliati",
                           None, r"Hastie", (r"scheda", r"Berthold")),
    "castelli_identity": (r"Castelli", None, r"Tecnologie per i Big Data",
                          (r"Castelli", r"Intelligent Data Analysis")),
    "lstm_lecture": (r"\bLSTM\b", r"embedding.{0,40}LSTM|LSTM.{0,40}embedding", None, None),
}
# A link is "declared" when the SENTENCE that holds it says the note is missing:
# the SVM run wrote "link rotti ([[Kernel]] non esiste, come anche [[A]], [[B]])",
# which names the vault's own gap, the behaviour the marker exists for.
DECLARED = re.compile(r"no such note|nota inesistente|non esist|inesistent|\bmanca|link rott|rotti\b|non c'è|not exist|broken link", re.I)
LECTURE_KW = {
    "svm": r"support vector|\bSVM\b", "kernel": r"\bkernel", "pca": r"\bPCA\b|componenti principali",
    "lda": r"discriminant analysis|analisi discriminante|\bLDA\b|Fisher", "qda": r"quadratic discr\w*iminant|\bQDA\b",  # the vault spells it "Discrtiminant" in Lezione 22
    "logistic": r"regressione logistica|logistic", "early_stop": r"early stopping|dropout",
    "cnn": r"convolu|\bCNN\b", "rnn_lstm": r"ricorrent|\bLSTM\b|\bRNN\b", "autoencoder": r"autoencoder|autocodific",
    "ensemble": r"ensemble|random forest|boosting|bagging", "knn": r"\bKNN\b|nearest neigh",
    "tree": r"alber[oi] di decisione|decision tree", "metrics": r"matrice di confusione|confusion matrix|\bROC\b|precision|richiamo|\brecall\b|\bF1\b",
    "cv": r"cross.?valid|k-?fold|validazione incrociata", "mlp": r"\bMLP\b|percettrone|feedforward|backpropagation|multistrato",
    "regular": r"regolarizz|ridge|lasso", "overfit": r"overfitting|underfitting|generalizzazione",
}
_LECTURE_REF = re.compile(r"Lezion[ei]\s*(\d{1,2})(?:\s*[–\-]\s*(\d{1,2}))?((?:\s*(?:,|e|/)\s*\d{1,2})*)")
_LECTURE_MIN_HITS = 2  # a lecture "covers" a topic when its file names it at least twice


def claim_outcomes(answer: str) -> dict[str, str]:
    out = {}
    paras = _paragraphs(answer)
    for name, (topic, wrong, right, pair) in CLAIMS.items():
        addressed = bool(re.search(topic, answer, re.I))
        is_wrong = bool(wrong and re.search(wrong, answer, re.I)) or bool(
            pair and any(re.search(pair[0], p, re.I) and re.search(pair[1], p, re.I) for p in paras))
        is_right = bool(right and re.search(right, answer, re.I))
        if is_wrong:
            out[name] = "wrong"
        elif not addressed:
            out[name] = "not_addressed"
        elif is_right:
            out[name] = "correct"
        else:
            out[name] = "addressed_unverified"
    return out


def _sentence_around(text: str, pos: int) -> str:
    start = max(text.rfind(".", 0, pos), text.rfind("\n", 0, pos)) + 1
    end_candidates = [i for i in (text.find(".", pos), text.find("\n", pos)) if i >= 0]
    end = min(end_candidates) if end_candidates else len(text)
    return text[start:end]


def link_outcomes(answer: str, ctx: Ctx) -> dict:
    resolved, declared, dead = [], [], []
    for m in WIKILINK.finditer(answer):
        target = m.group(1).strip()
        if ctx.resolve(target):
            resolved.append(target)
        elif DECLARED.search(_sentence_around(answer, m.start())):
            declared.append(target)
        else:
            dead.append(target)
    return {"resolved": len(resolved), "declared": sorted(set(declared)), "dead": sorted(set(dead)),
            "total": len(resolved) + len(declared) + len(dead)}


def _lecture_numbers(m: re.Match) -> list[int]:
    a = int(m.group(1)); b = m.group(2)
    nums = list(range(a, int(b) + 1)) if b and int(b) >= a and int(b) - a < 12 else [a]
    nums += [int(x) for x in re.findall(r"\d{1,2}", m.group(3) or "")]
    return [n for n in nums if 1 <= n <= 32]


def lecture_attributions(answer: str, ctx: Ctx) -> dict:
    """Every topic named on a line that OPENS with a lecture reference, checked
    against the lectures that line names. A line like "Lezioni 7-9: LDA, QDA,
    logistica" is one group claim: the topic is covered when any lecture of
    the group names it, wrong when none does. Threshold is one mention, so an
    exercise sheet that names LDA once counts; the cost is leniency, never a
    false "wrong". Prose lines that mention a lecture mid-sentence are skipped:
    their topics are not attributions."""
    cache: dict[int, str] = {}

    def body(n: int) -> str:
        if n not in cache:
            cache[n] = ctx.body(f"Inbox/machine_learning_md/Lezione {n}")
        return cache[n]
    correct, wrong = [], []
    for raw in answer.splitlines():
        line = re.sub(r"^[\s\-*•>#\[]+", "", raw)
        refs = list(_LECTURE_REF.finditer(line))
        if not refs or refs[0].start() > 40:
            continue
        group = sorted({n for m in refs for n in _lecture_numbers(m)})
        topics = [k for k, rx in LECTURE_KW.items() if re.search(rx, line, re.I)]
        for k in topics:
            covered = any(re.search(LECTURE_KW[k], body(n), re.I) for n in group)
            tag = f"L{'-'.join(map(str, group))}:{k}"
            (correct if covered else wrong).append(tag)
    return {"correct": sorted(set(correct)), "wrong": sorted(set(wrong)),
            "n_correct": len(set(correct)), "n_wrong": len(set(wrong))}


def score_v2(rec: dict, ctx: Ctx) -> dict:
    answer = rec.get("answer") or ""
    if rec.get("aborted"):
        state = "aborted"
    elif rec.get("tool_calls"):
        state = "wanted_tools"
    elif not answer.strip():
        state = "no_answer"
    else:
        state = "answered"
    v2 = {"state": state}
    if state == "answered":
        v2["claims"] = claim_outcomes(answer)
        v2["links"] = link_outcomes(answer, ctx)
        v2["lectures"] = lecture_attributions(answer, ctx)
        v2["prereqs_mentioned"] = {n: bool(re.search(rx, answer, re.I)) for n, rx in PREREQS_NECESSARY.items()}
    return v2


def rescore(out_dir: Path, ctx: Ctx) -> None:
    """Re-read every saved run, add `checks_v2`, print the table by arm × model."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for f in sorted(out_dir.glob("*.json")):
        if f.name.endswith(".payload.json"):
            continue
        rec = json.loads(f.read_text(encoding="utf-8"))
        rec["checks_v2"] = score_v2(rec, ctx)
        f.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        tools = rec.get("provenance", {}).get("with_tools")
        if tools is None and "variant" in rec:  # runs saved before the flag existed
            tools = (rec.get("prompt_tokens") or 0) > 60_000
        lane = "live" if "arm" in rec else ("replay+tools" if tools else "replay-no-tools")
        key = (f"{lane}:{rec.get('variant') or rec.get('arm')}", rec["model"].split("/")[-1])
        groups.setdefault(key, []).append(rec)
    print("| arm | model | n | answered / wanted tools / empty / aborted | claims correct / wrong / unverified / not addressed | links resolved / declared / dead | lecture pairs ok / wrong | prereqs mentioned |")
    print("|---|---|---|---|---|---|---|---|")
    for (arm, model), recs in sorted(groups.items()):
        st = collections.Counter(r["checks_v2"]["state"] for r in recs)
        ans = [r["checks_v2"] for r in recs if r["checks_v2"]["state"] == "answered"]
        cl = collections.Counter(o for a in ans for o in a["claims"].values())
        lk = [sum(a["links"]["resolved"] for a in ans), sum(len(a["links"]["declared"]) for a in ans),
              sum(len(a["links"]["dead"]) for a in ans)]
        le = [sum(a["lectures"]["n_correct"] for a in ans), sum(a["lectures"]["n_wrong"] for a in ans)]
        pre = sum(sum(a["prereqs_mentioned"].values()) for a in ans)
        n_ans = len(ans)
        print(f"| {arm} | {model} | {len(recs)} | {st['answered']} / {st['wanted_tools']} / {st['no_answer']} / {st['aborted']} "
              f"| {cl['correct']} / {cl['wrong']} / {cl['addressed_unverified']} / {cl['not_addressed']} "
              f"| {lk[0]} / {lk[1]} / {lk[2]} | {le[0]} / {le[1]} | {pre}/{5 * n_ans} |")

if __name__ == "__main__":
    sys.exit(main())
