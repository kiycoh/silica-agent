# SPDX-License-Identifier: AGPL-3.0-or-later
"""Field check of the sniff with the typed decision on, read-only.

Runs the production ladder (`forms.resolve`) twice on documents that would
reach the sniff in a /nucleate run (unstamped, unconverted .md/.txt), once
with SILICA_DECISION_MODEL set and once without, recording what Jev
answered, whether the verdict was taken, what the LLM said when it ran, and
the verdict the shipped sniff gives today. Nothing is written to any vault:
the write side (filing a draft, distilling the rest) is not exercised, only
the decision that selects it.

Run:  uv run python -m evals.field_sniff
Out:  docs/evaluation/sniff-field-<date>.{json,md}; the .md carries a head of
      every document and an empty `review` column to fill by hand.
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OBS = Path("~/Documents/Obsidian").expanduser()
DATE = "2026-09-19"
DECISION_MODEL = "typesafe/jev-1.13"
# (folder, how many to sample): other courses and authors than the 63-head gate
POOL = [
    (OBS / "alex_second_brain/Inbox/algebra_lineare", 6),
    (OBS / "alex_second_brain/Inbox/machine_learning", 4),
    (OBS / "alex_second_brain/Inbox/Prompt Engineering", 1),
    (OBS / "alex_second_brain/Inbox/reti_internet", 6),
    (OBS / "alex_second_brain/Inbox/teoria_probabilità", 6),
    (OBS / "alex_second_brain/Inbox/visione_artificiale", 8),
    (OBS / "alex_second_brain/Inbox/lab_natural_language_processing", 3),
    (OBS / "alex_second_brain/Inbox/lab_reti_internet", 2),
    (OBS / "alex_second_brain/Inbox/statistica", 2),
    (OBS / "test/Inbox/An Introduction to Support Vector Machines and Other Kernel-based Learning Methods (Nello Cristianini, John Shawe-Taylor)", 4),
    (OBS / "test/Inbox", 2),            # giovanni_castelli.md, scheda_trasparenza (converted, no stamp)
    (OBS / "joe-felz-library/Inbox", 2),  # INDEX.md, README.md
]


def _unstamped(p: Path) -> bool:
    head = p.read_text(encoding="utf-8", errors="replace")[:1500]
    return not any(line.startswith(("form:", "source_file:")) for line in head.splitlines()[:25])


def sample() -> list[Path]:
    rng = random.Random(19)
    out: list[Path] = []
    for folder, n in POOL:
        files = sorted(p for p in folder.rglob("*") if p.suffix in (".md", ".txt")
                       and p.is_file() and "/done/" not in p.as_posix() and _unstamped(p)
                       and "machine_learning_md" not in p.as_posix())
        if folder.name == "Inbox":  # top level only for the two plain-inbox entries
            files = [p for p in files if p.parent == folder]
        rng.shuffle(files)
        out.extend(files[:n])
    return out


class _Tap:
    """Records what forms.decide and forms._sniff_llm returned for the current file."""
    def __init__(self):
        self.jev = None
        self.llm = None

    def wrap(self, forms):
        real_decide, real_llm = forms.decide, forms._sniff_llm

        def decide(state, questions):
            t0 = time.perf_counter()
            ans = real_decide(state, questions)
            a = (ans or {}).get("form") or {}
            self.jev = {"choice": a.get("choice"), "p": (a.get("probabilities") or {}).get(a.get("choice")),
                        "confidence": a.get("confidence"), "s": round(time.perf_counter() - t0, 3),
                        "answered": ans is not None}
            return ans

        def llm(text, **kw):
            t0 = time.perf_counter()
            word = real_llm(text, **kw)
            self.llm = {"word": word, "s": round(time.perf_counter() - t0, 3)}
            return word

        forms.decide, forms._sniff_llm = decide, llm


def main() -> int:
    from silica.config import CONFIG
    import silica.kernel.forms as forms

    tap = _Tap()
    tap.wrap(forms)
    files = sample()
    print(f"{len(files)} documents", flush=True)
    rows = []
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        rel = p.relative_to(OBS).as_posix()
        row = {"file": rel, "chars": len(text), "head": " ".join(text[:600].split())[:320]}
        for mode, model in (("with_jev", DECISION_MODEL), ("shipped", "")):
            CONFIG.decision_model = model
            forms._sniff_memo.clear()
            tap.jev = tap.llm = None
            t0 = time.perf_counter()
            res = forms.resolve(text)
            row[mode] = {"form": res.form, "profile": res.profile, "origin": res.origin,
                         "s": round(time.perf_counter() - t0, 3), "jev": tap.jev, "llm": tap.llm}
        j = row["with_jev"]["jev"] or {}
        print(f"  {rel[:70]:70} jev={j.get('choice')} p={j.get('p')} -> {row['with_jev']['form'] or '-'} ({row['with_jev']['origin']}) | shipped {row['shipped']['form'] or '-'}", flush=True)
        rows.append(row)
    out = REPO / f"docs/evaluation/sniff-field-{DATE}"
    out.with_suffix(".json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    lines = [f"# Sniff field check, {DATE}", "",
             "Read-only run of `forms.resolve` on documents that reach the sniff, with and",
             "without `SILICA_DECISION_MODEL`. `review` is filled by hand: the form the",
             "reader assigns, and whether the taken verdict routes the file correctly.", "",
             "| # | file | chars | Jev (p) | taken | LLM after | shipped sniff | review |",
             "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        j = r["with_jev"]["jev"] or {}
        w = r["with_jev"]
        taken = w["form"] if w["origin"] == "sniff" and not w["llm"] else ("LLM path" if w["llm"] else w["origin"])
        llm_after = (w["llm"] or {}).get("word", "")
        lines.append(f"| {i} | `{r['file']}` | {r['chars']} | {j.get('choice')} ({j.get('p')}) | {taken} | {llm_after} | {r['shipped']['form'] or 'fallback'} | |")
    lines += ["", "## Heads", ""]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. `{r['file']}`: {r['head']}")
        lines.append("")
    out.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}.md/.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
