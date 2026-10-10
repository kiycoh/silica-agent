# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The two gates of ADR-0082 on the chat model.

Gate 1, figures: does a study turn show the figure its passage carries. The
model's own answers come from `run`; `place` replays silica.agent.figures over
them, which is what ships. `--arm` only labels a run: `off` names the 40 turns
run without the prompt section ADR-0082 then removed, kept as its record.
Gate 2, /diagram: does the skill's mermaid parse under mermaid's own parser
(the vendored bundle, in headless Chrome), and does it keep the skill's shape.

  uv run python -m evals.probe_chat_figures build
  uv run python -m evals.probe_chat_figures run figures --arm on
  uv run python -m evals.probe_chat_figures run diagram
  uv run python -m evals.probe_chat_figures parse
  uv run python -m evals.probe_chat_figures place --arm on    # code placement, replayed
  uv run python -m evals.probe_chat_figures stats

The sets and the outputs quote the vault, so they live in
~/.silica/evals/chat-figures/, out of the repo. Each run is one subprocess
under a fresh HOME (no narration lands in the owner's chat list, no memory
lane leaks in); the index is linked, not copied, because the turns only read.
Every write tool is stubbed: a study question is not consent to a write.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

from evals.probe_prompt_ab import PRISTINE_VAULT, WRITE_TOOLS, _calls_from_narration, _stub_egress

OUT = Path.home() / ".silica" / "evals" / "chat-figures"
STATIC = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static"
EMBED = re.compile(r"!\[\[([^\]|\n]+?\.(?:png|jpe?g|gif|webp|bmp|svg))(?:\|[^\]\n]*)?\]\]", re.I)
FENCE = re.compile(r"```mermaid[ \t]*\n(.*?)```", re.S)
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
TABLE = re.compile(r"^\s*\|.*\|\s*$\n^\s*\|?[\s:|-]*-[\s:|-]*$", re.M)
# The folders the owner studies from; Inbox/ and sources/ hold raw material and
# Silica's own output, which would measure the pipeline instead of the notes.
STUDY = ("Informatica/", "Matematica/", "Psicologia/", "Scienze naturali/", "Giurisprudenza/",
         "Scienze umanistiche/", "Filosofia/", "Economia e Finanza/")
SEED = 82


def _notes() -> list[Path]:
    return sorted(p for p in PRISTINE_VAULT.rglob("*.md")
                  if not any(part.startswith(".") for part in p.relative_to(PRISTINE_VAULT).parts))


def _first_figure(text: str) -> tuple[str, str] | None:
    """(heading, file) of the first embed whose section has prose around it:
    a figure with nothing to explain is not a figure a study turn would use."""
    heading, section, first = "", [], None
    for line in text.splitlines() + ["# end"]:
        h = HEADING.match(line)
        if h:
            if first and len(EMBED.sub("", "\n".join(section)).strip()) >= 200:
                return heading, first
            heading, section, first = h.group(2).strip(), [], None
            continue
        section.append(line)
        m = EMBED.search(line)
        if m and first is None:
            first = m.group(1).strip()
    return None


def build(n_fig: int, n_dia: int) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    figures, diagrams = [], []
    for p in _notes():
        rel = p.relative_to(PRISTINE_VAULT).as_posix()
        text = p.read_text(encoding="utf-8", errors="replace")
        hit = _first_figure(text)
        if hit:
            heading, file = hit
            ask = f"Spiegami «{heading}» dalla nota «{p.stem}»." if heading else f"Spiegami la nota «{p.stem}»."
            figures.append({"id": rel, "prompt": ask, "figure": file})
        elif rel.startswith(STUDY) and len(text) >= 1500 and sum(bool(HEADING.match(l)) for l in text.splitlines()) >= 3:
            diagrams.append({"id": rel, "prompt": f"/diagram {p.stem}"})
    figures = rng.sample(figures, min(n_fig, len(figures)))
    diagrams = rng.sample(diagrams, min(n_dia, len(diagrams)))
    for name, rows in (("figures", figures), ("diagram", diagrams)):
        (OUT / f"set-{name}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                               encoding="utf-8")
        print(f"set-{name}: {len(rows)} prompts")


def _fresh_home(root: Path) -> Path:
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    (root / ".silica").mkdir(parents=True)
    real = Path.home() / ".silica"
    shutil.copy2(real / ".env", root / ".silica" / ".env")
    (root / ".silica" / "index").symlink_to(real / "index")
    return root


def run_one(kind: str, arm: str, out_file: Path) -> None:
    """Inside the subprocess: HOME is fresh, SILICA_VAULT names the test vault."""
    import threading

    _stub_egress()
    from silica.tools import TOOLS
    for name in WRITE_TOOLS:
        if name in TOOLS:
            TOOLS[name].fn = lambda **kw: {"error": "read-only probe: nothing was written"}
    from silica.kernel.vault_manifest import apply_manifest_to_config
    apply_manifest_to_config()
    from silica.agent import narration as _narr
    from silica.agent.constraints import AgentConstraints, chat_deferred, chat_tools
    from silica.agent.loop import run_agent
    from silica.cli import _expand_workflow_shortcut, seed_messages
    from silica.config import CONFIG

    done = {json.loads(l)["id"] for l in out_file.read_text(encoding="utf-8").splitlines()} \
        if out_file.exists() else set()
    # HOME is the fresh one here, so the sets are found next to the output.
    rows = [json.loads(l) for l in (out_file.parent / f"set-{kind}.jsonl").read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["id"] in done:
            continue
        messages = [m for m in seed_messages(math=True) if m.get("content")]
        expanded = _expand_workflow_shortcut(row["prompt"]) if row["prompt"].startswith("/") else None
        messages.append({"role": "user", "content": expanded or row["prompt"],
                         **({"origin": "cli"} if expanded else {})})
        sid = _narr.NARRATOR.ensure_session(driver="probe", seed={"kind": kind, "arm": arm})
        _narr.NARRATOR.turn(messages[-1])
        t0 = time.time()
        try:
            answer = run_agent(messages, model=CONFIG.model, cancel_token=threading.Event(),
                               constraints=AgentConstraints(tools=chat_tools(messages), interactive=True,
                                                            deferred=chat_deferred(messages)))
            error = ""
        except Exception as e:  # one dead turn is a row that says so, not a dead run
            answer, error = "", f"{type(e).__name__}: {e}"
        _narr.NARRATOR.close()
        narr = _narr.narration_dir() / f"{sid}.jsonl"
        calls = (_calls_from_narration(narr) or [[]])[-1] if narr.exists() else []
        offered = sorted({m for c in calls for m in EMBED.findall(json.dumps(c.get("result"), ensure_ascii=False))})
        rec = {**row, "arm": arm, "model": CONFIG.model, "answer": answer or "", "error": error,
               "seconds": round(time.time() - t0, 1), "tools": [c["name"] for c in calls], "offered": offered}
        with out_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"{row['id']}: {rec['seconds']}s", flush=True)


def run(kind: str, arm: str) -> None:
    out_file = OUT / f"out-{kind}-{arm}.jsonl"
    home = _fresh_home(OUT / f"home-{kind}-{arm}")
    env = {**os.environ, "HOME": str(home), "SILICA_VAULT": str(PRISTINE_VAULT),
           "HF_HOME": str(Path.home() / ".cache" / "huggingface")}
    subprocess.run([sys.executable, "-m", "evals.probe_chat_figures", "--one", kind, arm, str(out_file)],
                   env=env, check=True)


def _rows(name: str) -> list[dict]:
    p = OUT / name
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _quote_labels_js() -> str:
    """The chat's own sanitizer, read out of app-chat.js like tests/webassets."""
    src = (STATIC / "app-chat.js").read_text(encoding="utf-8")
    m = re.search(r"^function quoteLabels\(src\) \{.*?^\}", src, re.S | re.M)
    return m.group(0) if m else ""


def parse() -> None:
    """Every /diagram block through mermaid.parse, the vendored 11.16 bundle,
    in headless Chrome: the parser the chat runs, not a regex of it."""
    rows = _rows("out-diagram-on.jsonl")
    blocks = [(r["id"], b) for r in rows for b in FENCE.findall(r["answer"])[:1]]
    page = OUT / "parse.html"
    page.write_text(
        "<!doctype html><meta charset=utf-8><body><pre id=out>pending</pre>"
        f"<script src='{(STATIC / 'mermaid.min.js').as_uri()}'></script><script>"
        f"const B={json.dumps([b for _i, b in blocks])};{_quote_labels_js()}"
        "mermaid.initialize({startOnLoad:false});"
        # As the chat does (drawDiagrams): the block as written, then quoted.
        "const ok=async s=>{try{return !!(await mermaid.parse(s))}catch(e){return String(e).slice(0,160)}};"
        "Promise.all(B.map(async s=>{const a=await ok(s);if(a===true)return true;"
        "const q=quoteLabels(s);return q!==s&&(await ok(q))===true?'quoted':a}))"
        ".then(r=>{document.getElementById('out').textContent=JSON.stringify(r)});</script>",
        encoding="utf-8")
    dom = subprocess.run(["google-chrome", "--headless=new", "--disable-gpu", "--allow-file-access-from-files",
                          f"--user-data-dir={OUT / 'chrome'}", "--virtual-time-budget=30000", "--dump-dom",
                          page.as_uri()], capture_output=True, text=True, timeout=180).stdout
    m = re.search(r"<pre id=\"out\">(.*?)</pre>", dom, re.S)
    if not m or m.group(1) == "pending":
        sys.exit("parse page never finished: " + dom[-400:])
    import html
    verdicts = json.loads(html.unescape(m.group(1)))
    out = {i: v for (i, _b), v in zip(blocks, verdicts)}
    (OUT / "parse-diagram.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{sum(v is True for v in out.values())} of {len(out)} blocks parse as written, "
          f"{sum(v == 'quoted' for v in out.values())} more once quoted")


def place(arm: str) -> None:
    """silica.agent.figures over the recorded turns, no model call: the
    conversation each turn had is in its narration, so code placement is
    measured on the very answers the prompt rule produced. Writes an audit
    sheet that names each placed figure and the paragraph it went after."""
    from silica.agent.figures import EMBED as FIG, place_figures
    rows = {r["prompt"]: r for r in _rows(f"out-figures-{arm}.jsonl")}
    names = {p.name: p for p in PRISTINE_VAULT.rglob("*") if p.is_file()}
    audit, n, fired, target = [], 0, 0, 0
    for narr in sorted((OUT / f"home-figures-{arm}" / ".silica" / "narration").glob("*.jsonl")):
        msgs = [json.loads(l)["payload"]["message"] for l in narr.read_text(encoding="utf-8").splitlines()
                if l.strip() and json.loads(l)["kind"] == "turn"]
        row = rows.get(msgs[0]["content"]) if msgs else None
        if not row:
            continue
        # The final answer is the caller's to narrate (the server does, run_one
        # does not): it comes from the output row.
        msgs.append({"role": "assistant", "content": row["answer"]})
        n += 1
        answer = row["answer"]
        out = place_figures(answer, msgs, exists=lambda f: Path(f).name in names)  # what /asset finds
        new = [e for e in FIG.findall(out) if e not in FIG.findall(answer)]
        fired += bool(new)
        target += row["figure"] in new
        for e in new:
            i = out.find(f"![[{e}")
            before = out[:i].rstrip().rsplit("\n\n", 1)[-1]
            audit.append(f"## {row['id']}\n\n- prompt: {row['prompt']}\n- figure: {names.get(Path(e).name, e)}\n"
                         f"- target: {e == row['figure']}\n\n> {before[-500:]}\n")
    (OUT / f"audit-place-{arm}.md").write_text("\n".join(audit), encoding="utf-8")
    print(f"place/{arm}: turns {n}, a figure placed in {fired}, the target figure in {target}, "
          f"figures placed {len(audit)}; audit: {OUT / f'audit-place-{arm}.md'}")


def _flow_shape(block: str) -> tuple[int, int, int] | None:
    """(nodes, edges, unlabelled edges) of a flowchart, by a regex that is good
    enough to count and no more: the parser above is the correctness check."""
    lines = block.strip().splitlines()
    if not lines or not re.match(r"\s*(flowchart|graph)\b", lines[0]):
        return None
    nodes, edges, bare = set(), 0, 0
    for ln in lines[1:]:
        for a, arrow, b in re.findall(r"([A-Za-z_][\w]*)[^-=.\n]*?\s*(--+>|==+>|-\.+->|--\s[^-]+?-->|--+>\|[^|]*\|)\s*([A-Za-z_][\w]*)", ln):
            nodes |= {a, b}
            edges += 1
            bare += "|" not in arrow and not re.match(r"--\s", arrow)
        nodes |= set(re.findall(r"^\s*([A-Za-z_][\w]*)\s*[\[({]", ln))
    return len(nodes), edges, bare


def stats() -> None:
    vault_files = {p.name for p in PRISTINE_VAULT.rglob("*") if p.is_file()}
    for arm in ("on", "off"):
        rows = _rows(f"out-figures-{arm}.jsonl")
        if not rows:
            continue
        with_fig = [r for r in rows if r["offered"]]
        placed = [r for r in with_fig if set(EMBED.findall(r["answer"])) & set(r["offered"])]
        invented = [(r["id"], e) for r in rows for e in EMBED.findall(r["answer"])
                    if e not in r["offered"] and Path(e).name not in vault_files]
        unoffered = [(r["id"], e) for r in rows for e in EMBED.findall(r["answer"]) if e not in r["offered"]]
        target = [r for r in rows if r["figure"] in r["offered"]]
        target_placed = [r for r in target if r["figure"] in EMBED.findall(r["answer"])]
        tables = sum(bool(TABLE.search(r["answer"])) for r in rows)
        secs = sorted(r["seconds"] for r in rows)
        print(f"figures/{arm}: n={len(rows)} errors={sum(bool(r['error']) for r in rows)} "
              f"median {secs[len(secs) // 2]}s")
        print(f"  a passage carried a figure: {len(with_fig)}; the answer placed one of those: {len(placed)}")
        print(f"  the target figure reached the model: {len(target)}; placed: {len(target_placed)}")
        print(f"  embeds not in any tool result: {len(unoffered)}; of which no such file: {len(invented)} {invented[:5]}")
        print(f"  answers with a table: {tables}")
    rows = _rows("out-diagram-on.jsonl")
    if rows:
        verdict = json.loads((OUT / "parse-diagram.json").read_text(encoding="utf-8")) \
            if (OUT / "parse-diagram.json").exists() else {}
        has = [r for r in rows if FENCE.search(r["answer"])]
        tables = [r for r in rows if not FENCE.search(r["answer"]) and TABLE.search(r["answer"])]
        shapes = [s for r in has if (s := _flow_shape(FENCE.findall(r["answer"])[0]))]
        print(f"diagram: n={len(rows)} errors={sum(bool(r['error']) for r in rows)} "
              f"mermaid={len(has)} table-instead={len(tables)} neither={len(rows) - len(has) - len(tables)}")
        print(f"  parse: {sum(v is True for v in verdict.values())} of {len(verdict)} as written, "
              f"{sum(v in (True, 'quoted') for v in verdict.values())} after quoteLabels")
        if shapes:
            print(f"  flowcharts {len(shapes)}: over 9 nodes {sum(n > 9 for n, _e, _b in shapes)}; "
                  f"edges {sum(e for _n, e, _b in shapes)}, unlabelled {sum(b for _n, _e, b in shapes)}")
        from collections import Counter
        print(f"  types: {dict(Counter(FENCE.findall(r['answer'])[0].split(None, 1)[0] for r in has).most_common())}")
        # A mindmap's nodes are its indented lines; a label written as an
        # identifier (Uomo_elaboratore) is the model treating text as an id.
        minds = [FENCE.findall(r["answer"])[0].strip().splitlines()[1:] for r in has
                 if FENCE.findall(r["answer"])[0].lstrip().startswith("mindmap")]
        if minds:
            print(f"  mindmaps {len(minds)}: over 9 nodes {sum(len([l for l in m if l.strip()]) > 9 for m in minds)}, "
                  f"median nodes {sorted(len([l for l in m if l.strip()]) for m in minds)[len(minds) // 2]}, "
                  f"with snake_case labels {sum(any(re.search(r'[A-Za-zÀ-ÿ]_[A-Za-zÀ-ÿ]', l) for l in m) for m in minds)}")
        ask = sum(bool(re.search(r"\?\s*$", r["answer"].strip())) for r in has)
        print(f"  ends on a question: {ask} of {len(has)}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=["build", "run", "parse", "place", "stats"])
    ap.add_argument("kind", nargs="?", choices=["figures", "diagram"])
    ap.add_argument("--arm", default="on", help="a label for the run's output files")
    ap.add_argument("--n-figures", type=int, default=40)
    ap.add_argument("--n-diagram", type=int, default=50)
    ap.add_argument("--one", nargs=3, help=argparse.SUPPRESS)  # the subprocess entry
    a = ap.parse_args(argv)
    if a.one:
        run_one(a.one[0], a.one[1], Path(a.one[2]))
    elif a.cmd == "build":
        build(a.n_figures, a.n_diagram)
    elif a.cmd == "run":
        run(a.kind, a.arm)
    elif a.cmd == "parse":
        parse()
    elif a.cmd == "place":
        place(a.arm)
    elif a.cmd == "stats":
        stats()
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
