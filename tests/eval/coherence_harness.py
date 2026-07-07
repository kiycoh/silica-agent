"""Coherence eval harness — measures the *current* dedup pipeline on a labeled
set of true-duplicate pairs, decomposed by the stage at which each pair dies.

It does NOT touch the pipeline: it replays COLLISION's routing decision
(collision.py) on real embeddings and reports where a known duplicate would go.

Three stages, measured separately (see the module diagnosis):

    retrieval  → would the twin be presented at all?   (ASSUMED here: we feed
                 the labeled twin directly. Rank-among-the-whole-vault is the
                 deferred `--full-index` mode; this run isolates ROUTING.)
    routing    → does the pair reach the ternary judge, or leak as a new note?
                 This is the bottleneck the diagnosis predicts.
    verdict    → given it reaches the judge, does the judge say "duplicate"?
                 (opt-in `--judge`; costs LLM calls.)

The families below are the user's Louvain *relatedness clusters*, NOT a clean
duplicate set — many intra-cluster pairs are correctly DISTINCT. So truth lives
in TRUE_DUPS (analyst-labeled). `--dump` prints the full intra-family cosine
matrix so those labels can be corrected, then rerun.

Run:
    uv run python tests/eval/coherence_harness.py --vault /path/to/vault
    uv run python tests/eval/coherence_harness.py --vault ... --dump
    uv run python tests/eval/coherence_harness.py --vault ... --judge
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# --- Golden material -------------------------------------------------------
# EDIT ME. Families = relatedness clusters (from /graph). TRUE_DUPS = the pairs
# an analyst asserts are the SAME concept (surface variant / abbreviation /
# translation). Everything else in a family is treated as correctly-distinct.

FAMILIES: dict[str, list[str]] = {
    "Neurone artificiale": ["Neurone artificiale", "Neurone Artificiale (ANN)", "Neurone Artificiale 1", "Biological Neurons vs. Artificial Neural Networks", "Rete neurale artificiale"],
    "One-Class SVDD": ["One-class classification", "Classificatori One-class", "One-Class SVDD", "One-Class Support Vector Data Description", "One-Class Support Vector Data Descriptor", "One Class SVDD con kernel"],
    "Storia AI": ["Storia dell'AI", "Storia dell'intelligenza artificiale", "Walter Pitts (Biological Foundations)"],
    "MLE": ["Maximum Likelihood estimator", "Stimatore massima verosimiglianza", "Maximum Likelihood for Normal Distribution Parameters"],
    "Probabilità": ["Interpretazione frequentista e bayesiana della probabilità", "Probabilità frequentista e bayesiana"],
    "Variabili aleatorie": ["Variabile casuale", "Variabili aleatorie discrete", "Variabili Discrete e Continue in Statistica Applicata al ML", "Random variables"],
    "SVD": ["Singular Value Decomposition (SVD)", "SVD e matrici dei vettori singolari", "Vettori singolari sinistra (left singular vectors)", "Dimensionality Reduction"],
    "Classificazione lineare": ["How to Perform Classification", "Linear Classification Model", "Linear Least Squares for Classification", "Perceptron Algorithm", "Perceptron Error Definition", "Percettrone di Rosenblatt"],
}

# Analyst-labeled true duplicates (same concept, different surface). CORRECT ME.
TRUE_DUPS: list[tuple[str, str]] = [
    ("Neurone artificiale", "Neurone Artificiale (ANN)"),
    ("Neurone artificiale", "Neurone Artificiale 1"),
    ("Neurone Artificiale (ANN)", "Neurone Artificiale 1"),
    ("One-Class SVDD", "One-Class Support Vector Data Description"),
    ("One-Class Support Vector Data Description", "One-Class Support Vector Data Descriptor"),
    ("One-Class SVDD", "One-Class Support Vector Data Descriptor"),
    ("One-class classification", "Classificatori One-class"),
    ("Storia dell'AI", "Storia dell'intelligenza artificiale"),
    ("Maximum Likelihood estimator", "Stimatore massima verosimiglianza"),
    ("Interpretazione frequentista e bayesiana della probabilità", "Probabilità frequentista e bayesiana"),
    ("Variabile casuale", "Random variables"),
]

# --- Routing model ---------------------------------------------------------
# The harness calls the REAL routing function (collision.route_concept) — no
# replica, so it can never drift from the pipeline it measures.
# ponytail: v1 passes is_hub=False and assumes perfect retrieval (labeled twin
# is the candidate). Both are documented gaps, not the measured bottleneck.

DECISION_LABEL = {
    "patch": "merge (no judge)",   # score≥τ_high & names agree
    "defer": "→ judge",           # borderline OR high-cos & names disagree (fix #1)
    "keep":  "NEW NOTE (low sim)",  # score≤τ_low → retrieval/embed too weak
}


def _selftest() -> None:
    from silica.router.states.collision import route_concept
    r = lambda s, a: route_concept(s, names_agree=a, is_hub=False, tau_high=0.85, tau_low=0.65)
    assert r(0.90, True) == "patch"
    assert r(0.90, False) == "defer"   # fix #1: was a silent new note
    assert r(0.75, False) == "defer"
    assert r(0.50, True) == "keep"


# --- Vault I/O -------------------------------------------------------------

def resolve_names(vault: Path, names: set[str]) -> dict[str, Path]:
    """name (note title / file stem) → absolute path. Exact stem, else casefold."""
    by_stem: dict[str, list[Path]] = {}
    for p in vault.rglob("*.md"):
        by_stem.setdefault(p.stem, []).append(p)
        by_stem.setdefault(p.stem.casefold(), []).append(p)
    out: dict[str, Path] = {}
    for n in names:
        hit = by_stem.get(n) or by_stem.get(n.casefold())
        if hit:
            out[n] = sorted(hit, key=lambda x: len(str(x)))[0]
    return out


def note_body(path: Path) -> str:
    from silica.kernel import frontmatter
    raw = path.read_text(encoding="utf-8", errors="replace")
    _, _, body = frontmatter.split(raw)
    return body if body is not None else raw


# --- Main ------------------------------------------------------------------

def main() -> int:
    _selftest()
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=os.getenv("SILICA_VAULT"), required=False)
    ap.add_argument("--dump", action="store_true", help="full intra-family cosine matrix")
    ap.add_argument("--judge", action="store_true", help="call the real LLM judge on →judge pairs")
    ap.add_argument("--full-index", action="store_true",
                    help="embed the WHOLE vault; rank each labeled twin (validates perfect-retrieval, measures k=1 / difetto #2)")
    args = ap.parse_args()
    if not args.vault:
        print("need --vault PATH or SILICA_VAULT", file=sys.stderr)
        return 2
    vault = Path(args.vault).expanduser()

    import numpy as np
    from silica.config import CONFIG
    from silica.agent.providers import get_embedder
    from silica.kernel.embed import _note_text
    from silica.router.states.collision import _names_agree, route_concept

    tau_high = getattr(CONFIG, "sim_threshold_high", 0.85)
    tau_low = getattr(CONFIG, "sim_threshold_low", 0.65)
    embedder = get_embedder(CONFIG)

    all_names = {n for fam in FAMILIES.values() for n in fam}
    paths = resolve_names(vault, all_names)
    missing = sorted(all_names - set(paths))

    # Embed each note twice, exactly as the pipeline does:
    #   candidate/stored side → _note_text(name, body, folder=<vault-rel dir>)
    #   incoming/query side   → _note_text(name, body)   (no folder)
    # ponytail: body-as-excerpt is OPTIMISTIC (real incoming excerpts are shorter,
    # so real cosine ≤ this). A failure here is therefore a lower bound on failures.
    q_texts, c_texts, order = [], [], []
    for n, p in paths.items():
        body = note_body(p)
        rel_dir = str(p.parent.relative_to(vault))
        q_texts.append(_note_text(n, body))
        c_texts.append(_note_text(n, body, folder=rel_dir))
        order.append(n)
    qv = dict(zip(order, embedder.embed(q_texts)))
    cv = dict(zip(order, embedder.embed(c_texts)))

    def cos(a: str, b: str) -> float:
        x, y = np.array(qv[a]), np.array(cv[b])
        return float(x @ y / ((x @ x) ** 0.5 * (y @ y) ** 0.5 + 1e-12))

    print(f"vault={vault}  τ_high={tau_high}  τ_low={tau_low}")
    print(f"resolved {len(paths)}/{len(all_names)} names" + (f"  MISSING: {missing}" if missing else ""))
    print("=" * 100)

    # --- Stage: ROUTING on labeled true duplicates ---
    counts = {"patch": 0, "defer": 0, "keep": 0}
    judge_pairs: list[tuple[str, str, float]] = []
    print("TRUE-DUPLICATE ROUTING  (incoming A vs stored B; perfect retrieval assumed)\n")
    print(f"  {'cos':>6}  {'names':<8} {'route':<20}  pair")
    for a, b in TRUE_DUPS:
        if a not in paths or b not in paths:
            print(f"  {'--':>6}  {'--':<8} {'UNRESOLVED':<20}  {a!r} | {b!r}")
            continue
        s = cos(a, b)
        agree = _names_agree(a, b)
        d = route_concept(s, names_agree=agree, is_hub=False, tau_high=tau_high, tau_low=tau_low)
        counts[d] += 1
        if d == "defer":
            judge_pairs.append((a, b, s))
        print(f"  {s:>6.3f}  {str(agree):<8} {DECISION_LABEL[d]:<20}  {a!r} | {b!r}")

    n = sum(counts.values())
    reach = counts["patch"] + counts["defer"]
    print("\n" + "-" * 100)
    print(f"pairs labeled duplicate : {n}")
    print(f"reach dedup (merge/judge): {reach}/{n}   ({reach/n:.0%})" if n else "no pairs")
    print(f"  merged w/o judge       : {counts['patch']}")
    print(f"  → reach ternary judge  : {counts['defer']}")
    print(f"LEAK as new note (low sim): {counts['keep']}/{n}   (score ≤ τ_low — retrieval/embed)")

    # --- Stage: VERDICT (opt-in) ---
    if args.judge and judge_pairs:
        from silica.capabilities.dedup import _decide_dedup
        print("\n" + "=" * 100 + "\nJUDGE VERDICTS (only →judge pairs):")
        ok = 0
        for a, b, s in judge_pairs:
            d = _decide_dedup(CONFIG, concept=a, excerpt=note_body(paths[a])[:2000],
                              candidate_name=b, candidate_body=note_body(paths[b])[:8000], score=s)
            ok += d.verdict in ("duplicate", "contradicts")
            print(f"  {d.verdict:<12} {a!r} | {b!r}  — {d.rationale[:80]}")
        print(f"verdict recall (dup|contradicts): {ok}/{len(judge_pairs)}")

    # --- Optional: full intra-family matrix for relabeling ---
    if args.dump:
        print("\n" + "=" * 100 + "\nINTRA-FAMILY COSINE (relabel TRUE_DUPS from this):")
        for fam, members in FAMILIES.items():
            ms = [m for m in members if m in paths]
            if len(ms) < 2:
                continue
            print(f"\n[{fam}]")
            for i, a in enumerate(ms):
                for b in ms[i + 1:]:
                    s = cos(a, b)
                    flag = "DUP?" if s >= tau_low else "    "
                    print(f"  {s:>6.3f} {flag}  {a!r} | {b!r}")
    # --- Optional: full-vault retrieval — validates the perfect-retrieval
    # assumption AND measures difetto #2 (k=1). Embeds every note as a candidate,
    # then ranks each labeled duplicate partner among all 1310. ---
    if args.full_index:
        partners: dict[str, set[str]] = {}
        for x, y in TRUE_DUPS:
            partners.setdefault(x, set()).add(y)
            partners.setdefault(y, set()).add(x)

        print("\n" + "=" * 100 + "\nFULL-VAULT RETRIEVAL  (rank of the labeled twin among all notes)")
        allp = sorted(vault.rglob("*.md"))
        cand_texts, cand_name = [], []
        for p in allp:
            folder = str(p.parent.relative_to(vault))
            cand_texts.append(_note_text(p.stem, note_body(p), folder=folder))
            cand_name.append(p.stem)
        vecs = []
        for i in range(0, len(cand_texts), 64):
            vecs.extend(embedder.embed(cand_texts[i:i + 64]))
        M = np.array(vecs, dtype=np.float32)
        M /= np.linalg.norm(M, axis=1, keepdims=True) + 1e-12
        print(f"  embedded {len(cand_name)} notes")

        top1 = top5 = total = 0
        for a in order:  # resolved family members with a query vec
            ps = partners.get(a)
            if not ps:
                continue
            q = np.array(qv[a], dtype=np.float32)
            q /= np.linalg.norm(q) + 1e-12
            sims = M @ q
            ranked = [(cand_name[j], float(sims[j])) for j in np.argsort(-sims) if cand_name[j] != a]
            twin_rank = next((r for r, (nm, _) in enumerate(ranked, 1) if nm in ps), None)
            total += 1
            top1 += twin_rank == 1
            top5 += bool(twin_rank and twin_rank <= 5)
            actual = ranked[0]
            pos = f"rank {twin_rank}" if twin_rank else ">index"
            print(f"  {a!r:52} twin @ {pos:8} | actual top-1: {actual[0]!r} ({actual[1]:.3f})")
        if total:
            print(f"\n  RETRIEVAL RECALL  k=1 (pipeline today): {top1}/{total} ({top1/total:.0%})"
                  f"   k=5 (difetto #2 fix): {top5}/{total} ({top5/total:.0%})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
