# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Formulas on a slide read by a vision model at conversion time, against the
text layer PDFium keeps: what Qwen 3.7 Flash (the hosted chat model, through
the production page-reading call) and scaledown/extract (an image in, formula
spans out) make of the pages of three formula-dense ML lectures.

Why this exists: PDFium keeps a slide's text layer and loses its formulas
(Greek letters and operators drawn in Cambria Math or Symbol come out as
debris), while ADR-0087's `_read_pages` sends to the chat model only the pages
with no text at all. Widening that trigger to formula pages is the cheap
alternative to running MinerU on the host; this measures what the reading
would put in the note, and what it costs.

Reference: the lectures' MinerU markdown in the test vault (not ground truth,
so a sample of pages is also graded by eye from the PNGs this writes).

Usage:
  uv run python -m evals.probe_page_formulas --out ~/.cache/silica-formulas
"""
from __future__ import annotations

import argparse
import base64
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LECTURES = (12, 13, 23)   # the three with the most $$ blocks in MinerU's output
# The child runs under a fresh HOME, so `~` is the parent's, passed down.
_HOME = Path(os.environ.get("PROBE_REAL_HOME") or Path.home())
PDFS = _HOME / "Documents/dev/machine_learning_uni_project/lezioni"
MINERU = _HOME / "Documents/Obsidian/test/Inbox/machine_learning_md"
READERS = 4               # convert._PAGE_READERS
MAX_TOKENS = 2500         # convert._PAGE_MAX_TOKENS

# PAGE_PROMPT (silica/kernel/recall/image_readings.py) with the formulas asked
# for in LaTeX, and its backslashes escaped: the reply is JSON, where an
# unescaped \frac, \beta or \nabla parses as a form feed, backspace or newline.
LATEX_PROMPT = (
    "This is one page of a document, a slide or a page of notes, whose text layer lost its "
    "formulas. Reply with JSON only: "
    '{{"kind": "...", "description": "...", "text": "..."}}. '
    "text: every word printed on the page, verbatim and in reading order, headings, paragraphs, "
    "lists, table cells and captions included; every formula in LaTeX, inline as $...$ and on its "
    "own line as $$...$$, each backslash escaped for JSON; keep paragraph breaks as blank lines; "
    "do not summarise or skip anything. "
    "kind: one or two words (slide, lecture notes, article page, table, handwriting). "
    "description: one sentence in {language} on what the page is about; start with the content "
    "itself, never with 'This page' or 'The image'."
)
EXTRACT_ENTITIES = {"formula": "a mathematical formula, equation or expression"}


def math_font_pages(pdf) -> set[int]:
    """Pages with a glyph in a math font: the trigger a widened `_read_pages`
    would use. PowerPoint's equation editor draws in Cambria Math, and Symbol
    carries the Greek letters of older slides."""
    import ctypes

    import pypdfium2.raw as raw

    out = set()
    buf = ctypes.create_string_buffer(256)
    flags = ctypes.c_int()
    for i in range(len(pdf)):
        tp = pdf[i].get_textpage()
        try:
            for c in range(raw.FPDFText_CountChars(tp.raw)):
                n = raw.FPDFText_GetFontInfo(tp.raw, c, buf, 256, ctypes.byref(flags))
                name = buf.raw[:max(n - 1, 0)].decode("latin-1")
                if "Math" in name or "Symbol" in name:
                    out.add(i)
                    break
        finally:
            tp.close()
    return out


def ask_qwen(png: Path) -> dict:
    """The production page reading (`convert._read_pages` -> read_image ->
    ask_chat_model), with the LaTeX prompt and the raw reply kept."""
    from silica.agent.providers import get_provider
    from silica.config import CONFIG
    from silica.kernel.recall import image_readings

    box: dict = {}

    def ask(messages):
        resp = get_provider(CONFIG, role="router").call_llm(
            messages=messages, max_tokens=MAX_TOKENS, temperature=0.0, reasoning=False)
        u = resp.usage or {}
        box.update(raw=resp.text or "", finish=resp.finish_reason, model=resp.request.get("model"),
                   prompt=u.get("prompt_tokens"), completion=u.get("completion_tokens"), usd=u.get("cost"))
        return resp.text or ""

    t0 = time.time()
    try:
        reading = image_readings.read_image(png, "Italian", prompt=LATEX_PROMPT, ask=ask)
    except Exception as exc:  # one page lost is a row with its error, not the run
        reading, box["error"] = None, f"{type(exc).__name__}: {exc}"[:300]
    box["seconds"] = round(time.time() - t0, 1)
    box["text"] = (reading or {}).get("text")
    return box


def ask_extract(png: Path) -> dict:
    """scaledown/extract on the page image. The image goes as a content part:
    the API's `document` field, sent in provider options, came back with no
    entities (2026-10-14)."""
    b64 = base64.b64encode(png.read_bytes()).decode()
    body = {"model": "scaledown/extract", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
        {"type": "text", "text": "Extract every formula on this slide."}]}],
        "provider": {"options": {"scaledown": {"entities": EXTRACT_ENTITIES, "context_chars": 0}}}}
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", json.dumps(body).encode(),
                                 {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                                  "Content-Type": "application/json"})
    t0 = time.time()
    try:
        r = json.load(urllib.request.urlopen(req, timeout=180))
        content = json.loads(r["choices"][0]["message"]["content"])
        u = r.get("usage") or {}
        out = {"formulas": [e["text"] for e in content.get("entities") or []],
               "confidence": [e.get("confidence") for e in content.get("entities") or []],
               "prompt": u.get("prompt_tokens"), "usd": u.get("cost")}
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as exc:
        out = {"error": f"{type(exc).__name__}: {exc}"[:300], "formulas": []}
    out["seconds"] = round(time.time() - t0, 1)
    return out


def ask_summarize(png: Path) -> dict:
    """scaledown/summarize on the page image, told to keep the formulas: an
    abstractive model, so what it drops or rewrites is the measurement."""
    b64 = base64.b64encode(png.read_bytes()).decode()
    body = {"model": "scaledown/summarize", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
        {"type": "text", "text": "Summarize this slide. Keep every formula exactly, in LaTeX."}]}]}
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", json.dumps(body).encode(),
                                 {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                                  "Content-Type": "application/json"})
    t0 = time.time()
    try:
        r = json.load(urllib.request.urlopen(req, timeout=180))
        u = r.get("usage") or {}
        out = {"text": r["choices"][0]["message"]["content"], "finish": r["choices"][0].get("finish_reason"),
               "prompt": u.get("prompt_tokens"), "completion": u.get("completion_tokens"), "usd": u.get("cost")}
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError) as exc:
        out = {"error": f"{type(exc).__name__}: {exc}"[:300], "text": ""}
    out["seconds"] = round(time.time() - t0, 1)
    return out


def add_summarize(out: Path) -> None:
    """Asked after the first run (2026-10-14): the same PNGs, one more arm."""
    path = out / "pages.json"
    pages = json.loads(path.read_text(encoding="utf-8"))
    todo = [p for p in pages if p.get("png") and "summarize" not in p]
    with ThreadPoolExecutor(max_workers=READERS) as pool:
        for p, r in zip(todo, pool.map(lambda p: ask_summarize(Path(p["png"])), todo)):
            p["summarize"] = r
    path.write_text(json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8")


def run(out: Path, max_usd: float) -> None:
    """In the child, under the hosted env and a fresh HOME."""
    import pypdfium2 as pdfium

    from silica.sources import pdf_extract

    pages = []
    for n in LECTURES:
        pdf = pdfium.PdfDocument(PDFS / f"Lezione {n}.pdf")
        mathy = math_font_pages(pdf)
        for i, png in pdf_extract._render(pdf, list(range(len(pdf)))):
            path = out / "png" / f"L{n}-p{i + 1:02d}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            if png:
                path.write_bytes(png)
            tp = pdf[i].get_textpage()
            text = tp.get_text_range()
            tp.close()
            pages.append({"lecture": n, "page": i + 1, "math_font": i in mathy, "png": str(path) if png else None,
                          "pdfium": text})
    spent = [0.0]
    lock = threading.Lock()

    def one(p):
        with lock:
            if spent[0] > max_usd:
                return p
        if p["png"]:
            p["qwen"] = ask_qwen(Path(p["png"]))
            p["extract"] = ask_extract(Path(p["png"]))
            with lock:
                spent[0] += (p["qwen"].get("usd") or 0) + (p["extract"].get("usd") or 0)
        print(f"L{p['lecture']} p{p['page']:02d} math={p['math_font']} "
              f"qwen {p.get('qwen', {}).get('seconds')}s {p.get('qwen', {}).get('finish')} "
              f"extract {len(p.get('extract', {}).get('formulas', []))} spent {spent[0]:.4f}", flush=True)
        return p

    with ThreadPoolExecutor(max_workers=READERS) as pool:
        pages = list(pool.map(one, pages))
    (out / "pages.json").write_text(json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8")


def run_production(out: Path) -> None:
    """The gate after the amendment shipped: `convert()` itself on the three
    lectures, in the child under the hosted env, so the trigger, the
    markdown prompt and the body replacement are the ones a tenant gets."""
    import silica.agent.llm as llm

    calls: list[dict] = []
    real_emit = llm.emit

    def capture(event, **f):
        if event == "llm_call":
            calls.append(f)
        return real_emit(event, **f)

    llm.emit = capture
    from silica.sources import pdf_extract
    from silica.sources.convert import convert

    vault = Path(os.environ["SILICA_VAULT"])
    rows = []
    for n in LECTURES:
        rel = f"Inbox/Lezione {n}.pdf"
        shutil.copy(PDFS / f"Lezione {n}.pdf", vault / rel)
        before, t0 = len(calls), time.time()
        notes = convert(rel)
        seen = pdf_extract.cached(vault / rel) or {}
        rows.append({"lecture": n, "seconds": round(time.time() - t0, 1), "notes": notes,
                     "body": "\n".join((vault / r).read_text(encoding="utf-8") for r in notes),
                     "math": seen.get("math", []), "render": seen.get("render", []),
                     "math_read": seen.get("math_read", []), "calls": calls[before:]})
    (out / "production.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def score_production(out: Path) -> str:
    rows = json.loads((out / "production.json").read_text(encoding="utf-8"))
    bare = re.compile(r"\\[a-zA-Z]+|\^\{|_\{")
    lines = ["| lecture | MinerU formulas | ratio>=.9 | ratio>=.7 | math pages | read | not read | "
             "quoted lines with LaTeX outside $ | USD | seconds |", "|---|---|---|---|---|---|---|---|---|---|"]
    tot = [0, 0, 0]
    for r in rows:
        refs = latex_formulas((MINERU / f"Lezione {r['lecture']}.md").read_text(encoding="utf-8"))
        cands = windows(text_units(r["body"]), 0)
        hits = [best(ref, cands) for ref in refs]
        quoted = "\n".join(ln[2:] for ln in r["body"].splitlines() if ln.startswith("> ") and "read by" not in ln)
        # Block-aware: a $$ block over several lines has LaTeX on lines without a $.
        outside = re.sub(r"\$[^$\n]+?\$", " ", re.sub(r"\$\$.+?\$\$", " ", quoted, flags=re.S))
        naked = sum(1 for ln in outside.splitlines() if bare.search(ln))
        usd = sum(c.get("usd") or 0 for c in r["calls"])
        tot = [tot[0] + len(refs), tot[1] + sum(x >= 0.9 for x, _, _ in hits), tot[2] + usd]
        lines.append(f"| {r['lecture']} | {len(refs)} | {sum(x >= 0.9 for x, _, _ in hits)} | "
                     f"{sum(x >= 0.7 for x, _, _ in hits)} | {len(r['math'])} | {len(r['math_read'])} | "
                     f"{len([i for i in r['render'] if i in r['math'] and i not in r['math_read']])} | "
                     f"{naked} of {len(quoted.splitlines())} | {usd:.4f} | {r['seconds']} |")
    lines.append(f"| all | {tot[0]} | {tot[1] / tot[0]:.2f} | | | | | | {tot[2]:.4f} | |")
    return "\n".join(lines)


# --- scoring against MinerU ----------------------------------------------------

_MACRO = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ",
    "tau": "τ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
    "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "in": "∈", "notin": "∉", "sum": "∑", "prod": "∏", "int": "∫", "partial": "∂", "nabla": "∇",
    "infty": "∞", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "cdot": "·", "times": "×", "to": "→", "rightarrow": "→", "leftarrow": "←",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔", "iff": "⇔", "implies": "⇒",
    "forall": "∀", "exists": "∃", "pm": "±", "sqrt": "√", "ell": "ℓ", "propto": "∝",
    "subset": "⊂", "subseteq": "⊆", "cup": "∪", "cap": "∩", "emptyset": "∅", "sim": "∼",
    "equiv": "≡", "circ": "∘", "ldots": "…", "cdots": "⋯", "dots": "…", "vdots": "⋮",
    "lVert": "‖", "rVert": "‖", "Vert": "‖", "|": "‖", "langle": "⟨", "rangle": "⟩",
    "mid": "|", "top": "T", "intercal": "T",
}
_DROP = re.compile(r"\\(frac|dfrac|tfrac|pmb|underset|overset|mathop|mathbf|boldsymbol|bm|mathrm|mathit|mathsf|mathcal|text|textbf|textit|operatorname\*?"
                   r"|left|right|big|Big|bigg|Bigg|displaystyle|limits|nolimits|hat|bar|tilde|overline"
                   r"|widehat|widetilde|vec|dot|mathbb|begin|end|quad|qquad)(?![a-zA-Z])|\\[,;:! ]|~|&|\\\\")
_MACRO_RE = re.compile(r"\\([a-zA-Z]+|\|)")
_UNI = {"−": "-", "∥": "‖", "ℝ": "R", "ᵀ": "^T", "⊤": "T", "′": "'", "∗": "*", "•": "·", "⋅": "·"}


def canon(s: str) -> str:
    """A formula with its formatting gone, in one alphabet for both arms:
    LaTeX macros become the symbols they draw, so MinerU's \\boldsymbol{\\alpha}
    and an extract span's α compare equal."""
    s = _DROP.sub("", s)
    s = _MACRO_RE.sub(lambda m: _MACRO.get(m.group(1), m.group(1)), s)
    s = s.replace("||", "‖")
    for a, b in _UNI.items():
        s = s.replace(a, b)
    s = re.sub(r"\s", "", s)
    s = re.sub(r"\{(array|matrix|bmatrix|pmatrix|cases|aligned|align)\}|\{[clr|]+\}", "", s)
    return re.sub(r"[{}$]", "", s)


_DISPLAY = re.compile(r"\$\$(.+?)\$\$", re.S)
_INLINE = re.compile(r"(?<!\$)\$([^$\n]+?)\$(?!\$)")
MIN_LEN = 6   # x, \alpha, w_i: a symbol is not a formula a reader would miss


def latex_formulas(md: str) -> list[str]:
    out = [canon(m) for m in _DISPLAY.findall(md)]
    out += [canon(m) for m in _INLINE.findall(_DISPLAY.sub(" ", md))]
    return [f for f in out if len(f) >= MIN_LEN]


def text_units(text: str) -> list[str]:
    """A reading's formulas: its $-delimited ones and every line besides,
    because Qwen wrote whole slides of LaTeX with no delimiter (L12 p21-22)."""
    return latex_formulas(text) + [c for c in (canon(line) for line in text.splitlines()) if len(c) >= MIN_LEN]


def windows(units: list[str], page: int, k: int = 3) -> list[tuple[str, int]]:
    """Each unit and each run of up to `k` consecutive ones: a chain of
    equalities is one MinerU formula and three lines of a reading, or the
    other way round."""
    return [("".join(units[i:i + w]), page) for i in range(len(units)) for w in range(1, k + 1)
            if i + w <= len(units)]


def best(ref: str, cands: list[tuple[str, int]]) -> tuple[float, float, int | None]:
    """Best (ratio, share of the reference found in order, page) over the arm's
    formulas: the second forgives an arm that wrote a chain of equalities as
    one formula where MinerU cut it in three."""
    top = (0.0, 0.0, None)
    for c, page in cands:
        sm = difflib.SequenceMatcher(None, ref, c, autojunk=False)
        cover = sum(b.size for b in sm.get_matching_blocks()) / len(ref)
        if (sm.ratio(), cover) > top[:2]:
            top = (sm.ratio(), cover, page)
    return top


def score(out: Path) -> str:
    pages = json.loads((out / "pages.json").read_text(encoding="utf-8"))
    rows = []
    for n in LECTURES:
        refs = latex_formulas((MINERU / f"Lezione {n}.md").read_text(encoding="utf-8"))
        mine = [p for p in pages if p["lecture"] == n]
        qwen = [w for p in mine for w in windows(text_units(p.get("qwen", {}).get("text") or ""), p["page"])]
        ext = [w for p in mine for w in windows([c for c in map(canon, p.get("extract", {}).get("formulas", []))
                                                 if len(c) >= MIN_LEN], p["page"])]
        summ = [w for p in mine for w in windows(text_units(p.get("summarize", {}).get("text") or ""), p["page"])]
        arms = (("qwen", qwen), ("extract", ext)) + ((("summarize", summ),) if any("summarize" in p for p in mine) else ())
        for arm, cands in arms:
            hits = [best(r, cands) for r in refs]
            on_plain = sum(1 for _, cov, pg in hits if cov >= 0.9 and pg is not None
                           and not next(p for p in mine if p["page"] == pg)["math_font"])
            rows.append((n, arm, len(refs), len(cands),
                         sum(r >= 0.9 for r, _, _ in hits), sum(r >= 0.7 for r, _, _ in hits),
                         sum(c >= 0.9 for _, c, _ in hits), on_plain))
    lines = ["| lecture | arm | MinerU formulas | arm windows | ratio>=.9 | ratio>=.7 | covered>=.9 | of which on pages without a math font |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| " + " | ".join(str(x) for x in r) + " |")
    for arm in ("qwen", "extract", "summarize"):
        t = [r for r in rows if r[1] == arm]
        if not t:
            continue
        ref = sum(r[2] for r in t)
        lines.append(f"| all | {arm} | {ref} | {sum(r[3] for r in t)} | {sum(r[4] for r in t) / ref:.2f} | "
                     f"{sum(r[5] for r in t) / ref:.2f} | {sum(r[6] for r in t) / ref:.2f} | {sum(r[7] for r in t)} |")
    q = [p.get("qwen", {}) for p in pages if p.get("png")]
    e = [p.get("extract", {}) for p in pages if p.get("png")]
    bad = re.compile(r"[\x08\x0c]|\t[a-z]|\r[a-z]")
    lines += ["",
              f"pages read: {len(q)} · math-font pages: {sum(p['math_font'] for p in pages)} of {len(pages)}",
              f"qwen: USD {sum(x.get('usd') or 0 for x in q):.4f} · prompt {sum(x.get('prompt') or 0 for x in q):,} · "
              f"completion {sum(x.get('completion') or 0 for x in q):,} · median s "
              f"{sorted(x.get('seconds') or 0 for x in q)[len(q) // 2]} · finish length "
              f"{sum(x.get('finish') == 'length' for x in q)} · unparsed {sum(not x.get('text') for x in q)} · "
              f"errors {sum('error' in x for x in q)} · JSON-escape damage in parsed text "
              f"{sum(bool(bad.search(x.get('text') or '')) for x in q)} · models {sorted({str(x.get('model')) for x in q})}",
              f"extract: USD {sum(x.get('usd') or 0 for x in e):.4f} · prompt {sum(x.get('prompt') or 0 for x in e):,} · "
              f"median s {sorted(x.get('seconds') or 0 for x in e)[len(e) // 2]} · pages at the 10-span cap "
              f"{sum(len(x.get('formulas', [])) >= 10 for x in e)} · errors {sum('error' in x for x in e)}"]
    m = [p["summarize"] for p in pages if "summarize" in p]
    if m:
        lines.append(f"summarize: USD {sum(x.get('usd') or 0 for x in m):.4f} · prompt {sum(x.get('prompt') or 0 for x in m):,} · "
                     f"completion {sum(x.get('completion') or 0 for x in m):,} · median s "
                     f"{sorted(x.get('seconds') or 0 for x in m)[len(m) // 2]} · errors {sum('error' in x for x in m)}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(Path.home() / ".cache" / "silica-formulas"))
    ap.add_argument("--max-usd", type=float, default=0.08)
    ap.add_argument("--score", action="store_true", help="score an existing run")
    ap.add_argument("--add-summarize", action="store_true", help="add the summarize arm to an existing run")
    ap.add_argument("--production", action="store_true", help="the gate: convert() as shipped")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    out = Path(args.out).expanduser()
    if args.child:
        run_production(out) if args.production else run(out, args.max_usd)
        return 0
    if args.production and args.score:
        print(score_production(out))
        return 0
    if args.production:
        from evals.probe_context_cap import HOSTED_ENV
        from evals.probe_prompt_ab import fresh_home
        out.mkdir(parents=True, exist_ok=True)
        home = fresh_home(out / "home")
        vault = out / "vault"
        shutil.rmtree(vault, ignore_errors=True)
        (vault / "Inbox").mkdir(parents=True)
        env = {**os.environ, **HOSTED_ENV, "HOME": str(home), "PROBE_REAL_HOME": str(Path.home()),
               "SILICA_VAULT": str(vault), "SILICA_PDF_PROVIDER": "pdfium"}
        rc = subprocess.run([sys.executable, "-m", "evals.probe_page_formulas", "--child", "--production",
                             "--out", str(out)], env=env).returncode
        if rc:
            return rc
        table = score_production(out)
        (out / "production.md").write_text(table + "\n", encoding="utf-8")
        print(table)
        return 0
    if args.add_summarize:
        add_summarize(out)
    elif not args.score:
        from evals.probe_context_cap import HOSTED_ENV
        from evals.probe_prompt_ab import fresh_home
        out.mkdir(parents=True, exist_ok=True)
        home = fresh_home(out / "home")
        env = {**os.environ, **HOSTED_ENV, "HOME": str(home), "PROBE_REAL_HOME": str(Path.home())}
        rc = subprocess.run([sys.executable, "-m", "evals.probe_page_formulas", "--child",
                             "--out", str(out), "--max-usd", str(args.max_usd)], env=env).returncode
        if rc:
            return rc
    table = score(out)
    (out / "score.md").write_text(table + "\n", encoding="utf-8")
    print(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
