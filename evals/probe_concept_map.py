# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Does a concept map inferred from the vault show anything the note graph does not?

`docs/spec-concept-map.md` refuted the cheap version: a map built on
`extract_keyphrases` candidates gives 279 nodes, 143 edges, 63% of them isolated,
and roots like `margin`, `mit`, `colt`, `funzione`. The regola d'arco was not the
defect — the NODE SET is. YAKE emits n-gram fragments (`margin` beside `geometric
margin`), generic words and entities, and every relation computed on top inherits
that. `margin -> geometric margin` is substring nesting dressed as a taxonomy.

The untested lever is therefore the vocabulary, not the edge rule. Silica already
extracts a clean one on every write — `distiller_prompt.txt` requires 3-8
normalized keyphrases per write/patch op, grounded in the body — and then throws
it away: `orchestrator.py` forwards it to `build_contribution`, which appends it
as a sentence and stems it. Nothing persists the phrases.

This probe pays for that vocabulary on ONE Louvain community and asks whether the
inclusion hierarchy over it survives two gates. It is read-only: no store is
written, no note is touched, no `concepts.json` exists yet. If the gates fail, the
verdict closes the lane and the other ~640 notes are never extracted.

  uv run python -m evals.probe_concept_map --vault ~/Documents/Obsidian/test
  uv run python -m evals.probe_concept_map --roots-ok 12    # record the eyeball

The extraction is cached per (vault digest, community, model), so recording the
human half of gate 1 on the second invocation costs no LLM calls.
"""
from __future__ import annotations

import argparse
import itertools
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import BaseModel

# --- Gates, declared before the run, not tuned afterwards --------------------
GATE_ROOTS_OK = 0.70          # fraction of roots a human accepts as macroconcepts
GATE_NON_REDUNDANT = 0.40     # fraction of edges the note graph cannot already express
DF_MIN = 2                    # a concept in one note is a leaf: node, no edge
DF_MAX_SHARE = 0.30           # a concept in a third of the notes carries no information
NEST_OVERLAP = 0.70           # substring + this much note overlap = same concept
INCLUSION = 0.25              # A -> B when this share of note(B) sits outside note(A)
ROOTS_SHOWN = 25              # the eyeball budget

# Vacuity floors. A gate with nothing to measure must report null, never PASS:
# this vault has already produced two closed verdicts whose kill gate turned out
# to be vacuous, and a third would be a harness bug wearing a result's clothes.
# The smoke run that motivated these: 33 nodes, 2 edges, 0 of them with a note at
# both ends, and gate 2 read "2/2 = 1.00 PASS".
MIN_ROOTS_FOR_G1 = 5          # an eyeball over 2 roots measures nothing
MIN_RESOLVABLE_FOR_G2 = 10    # only edges whose ends are BOTH notes can be redundant

BODY_CAP = 8000               # chars of note body sent to the extractor


class ConceptList(BaseModel):
    concepts: list[str]


# Deliberately NOT distiller_prompt.txt's wording. That one asks what the note
# "is about", which admits entities: the vault's own run surfaced `giosuè lo
# bosco` (a lecturer) and `machine learning (9 cfu)` (a course code) as concepts.
# Asking instead for what a READER MUST UNDERSTAND excludes them by construction.
#
# It also deliberately does not ask what the note DEFINES. Silica ingests
# atomically, one note per concept, so the defined concept is the title and would
# land at df=1 — the DF_MIN filter would then delete the entire vocabulary. The
# hierarchy needs concepts that are TRAVERSED, not defined.
EXTRACT_SYSTEM = """You name the ideas a reader must understand to follow a note.

Return 3 to 8 concepts. Each one:
  - is an idea, not a thing: no people, no course names or codes, no file
    formats, no section titles, no layout artefacts
  - is grounded in the body you are given, never inferred from the topic
  - is 1 to 4 words, lowercase unless it is a proper technical name
  - is written in the language of the note

Prefer the canonical name of an idea over a phrase containing it: "maximal
margin hyperplane", not "the maximal margin hyperplane of the classifier".

JSON only: {"concepts": ["...", "..."]}"""

_PARENS = re.compile(r"\(.*?\)")
_PUNCT = re.compile(r"[\[\]|#*_`]")
_WS = re.compile(r"\s+")


def norm(phrase: str) -> str:
    """Concept surface -> comparison key. Parenthetical disambiguators go: the
    vault titles notes `Predittore (Statistica)`, and the concept is the head."""
    s = _PUNCT.sub(" ", _PARENS.sub(" ", phrase.lower()))
    return _WS.sub(" ", s).strip(" .:,;-")


# ---------------------------------------------------------------------------
# Sampling: the LARGEST community, not the most coherent
# ---------------------------------------------------------------------------

def pick_community(nodes: list[dict], edges: list[dict], cid: int | None):
    """(cid, label, [node ids]) for the largest Louvain community, or `cid`.

    Largest on purpose. The map, if it ships, serves the area the vault actually
    has most of; picking the most thematically coherent community would measure
    how well the probe was aimed, not whether the hierarchy holds.
    """
    from silica.kernel.recall.graph_export import detect_communities

    comms = detect_communities(nodes, edges)   # assigns node["group"] in place
    if not comms:
        raise SystemExit("no communities: the vault has no resolved wikilinks")
    members: dict[int, list[str]] = {}
    for n in nodes:
        if n.get("type") != "ghost" and n.get("group", -1) >= 0:
            members.setdefault(n["group"], []).append(n["id"])
    if cid is None:
        cid = max(members, key=lambda c: (len(members[c]), -c))
    if cid not in members:
        raise SystemExit(f"community {cid} not found; have {sorted(members)}")
    label = next((c.label for c in comms if c.id == cid), f"Cluster {cid}")
    return cid, label, sorted(members[cid])


# ---------------------------------------------------------------------------
# Extraction (the only LLM in this probe, and the only thing worth caching)
# ---------------------------------------------------------------------------

def _extract_one(vault: Path, node_id: str, model: str) -> list[str]:
    from silica.agent.llm import call_llm
    from silica.kernel.text.sanitize import parse_json

    path = vault / node_id
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    # Isolated per note: extraction is the only thing this probe pays for, and
    # one bad response must not discard the calls already spent on the others.
    # The coverage line ("N/M notes answered") is what makes the loss visible.
    try:
        resp = call_llm(
            model=model,
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM},
                {"role": "user", "content": f"TITLE: {path.stem}\nBODY:\n{body[:BODY_CAP]}"},
            ],
            response_format=ConceptList,
            temperature=0.0,
        )
    except Exception as exc:
        print(f"   ! {node_id}: {type(exc).__name__}: {str(exc)[:120]}")
        return []
    parsed, _ = parse_json(resp.text or "", strict=False)
    raw = parsed.get("concepts") if isinstance(parsed, dict) else None
    # A note that fails to parse contributes nothing rather than an empty list
    # standing in for "this note has no concepts" — the two are different facts,
    # and only the first should be visible in the coverage line.
    return [str(c) for c in (raw or []) if isinstance(c, (str, int, float)) and str(c).strip()]


def extract(vault: Path, node_ids: list[str], model: str, jobs: int) -> dict[str, list[str]]:
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(lambda n: _extract_one(vault, n, model), node_ids))
    return {n: r for n, r in zip(node_ids, results) if r}


# ---------------------------------------------------------------------------
# The map
# ---------------------------------------------------------------------------

def build_sets(by_note: dict[str, list[str]], n_notes: int) -> dict[str, set[str]]:
    """{concept key: {note ids}} after df filtering and nested-fragment dedup."""
    sets: dict[str, set[str]] = {}
    for note, phrases in by_note.items():
        for p in {norm(x) for x in phrases}:
            if p:
                sets.setdefault(p, set()).add(note)

    kept = {k: v for k, v in sets.items()
            if DF_MIN <= len(v) <= max(DF_MIN, DF_MAX_SHARE * n_notes)}

    # `margin` beside `geometric margin` is one concept written twice. Drop the
    # shorter one when it is a substring AND lives in mostly the same notes;
    # without the note-set condition this would merge genuinely distinct ideas
    # that happen to share a word.
    drop = set()
    for a, b in itertools.permutations(sorted(kept, key=len), 2):
        if a in drop or b in drop or len(a) >= len(b):
            continue
        if a in b and len(kept[a] & kept[b]) / len(kept[a]) >= NEST_OVERLAP:
            drop.add(a)
    return {k: v for k, v in kept.items() if k not in drop}


def inclusion_edges(sets: dict[str, set[str]]) -> list[tuple[str, str]]:
    """A -> B when B lives almost entirely inside A's notes and A is broader."""
    return [
        (a, b) for a, b in itertools.permutations(sets, 2)
        if len(sets[a]) > len(sets[b])
        and len(sets[b] - sets[a]) / len(sets[b]) < INCLUSION
    ]


# ---------------------------------------------------------------------------
# Gate 2: can the note graph already say this?
# ---------------------------------------------------------------------------

def redundancy(edges, nodes, wiki) -> dict:
    """Share of concept edges the wikilink graph cannot already express.

    NOT "are note(A) and note(B) linked": the inclusion rule puts B's notes
    INSIDE A's by construction, so that test is degenerate — it asks whether a
    set is linked to itself. The discriminating question uses the fact that
    Silica ingests atomically, so a concept is often a note: when both endpoints
    resolve to a note title, the note graph CAN express the relation, and the
    concept edge is redundant exactly when those two notes are already
    wikilinked. An endpoint with no note is non-redundant by construction — the
    note graph has no vertex for it.
    """
    title_of = {norm(n["label"]): n["id"] for n in nodes if n.get("type") != "ghost"}
    total = both_resolve = non_redundant = 0
    examples: list[dict] = []
    for a, b in edges:
        total += 1
        na, nb = title_of.get(a), title_of.get(b)
        if na is None or nb is None:
            non_redundant += 1
            continue
        both_resolve += 1
        linked = nb in wiki.get(na, ()) or na in wiki.get(nb, ())
        if not linked:
            non_redundant += 1
            if len(examples) < 8:
                examples.append({"from": a, "to": b, "notes": [na, nb]})
    return {
        "edges": total,
        "both_endpoints_are_notes": both_resolve,
        "non_redundant": non_redundant,
        "share": (non_redundant / total) if total else 0.0,
        "examples_the_note_graph_misses": examples,
    }


# ---------------------------------------------------------------------------

def run(vault: Path, *, cid: int | None, model: str, jobs: int,
        roots_ok: int | None, cache: Path) -> dict:
    import networkx as nx

    from evals._shared import provenance
    from evals.golden.runner import vault_digest
    from silica.kernel.recall.graph_export import build_graph_data, edge_graph

    nodes, edges_g = build_graph_data()
    cid, label, members = pick_community(nodes, edges_g, cid)
    digest, n_vault = vault_digest(vault)
    print(f"1. COMMUNITY {cid} · {label} · {len(members)} notes "
          f"(vault {n_vault} notes, {digest[:19]})")

    key = {"digest": digest, "community": cid, "model": model}
    cached = json.loads(cache.read_text(encoding="utf-8")) if cache.is_file() else {}
    if cached.get("key") == key:
        by_note = cached["by_note"]
        print(f"   extraction: cache hit ({len(by_note)} notes) → no LLM calls")
    else:
        print(f"   extracting concepts: {len(members)} LLM calls, {jobs} at a time…")
        by_note = extract(vault, members, model, jobs)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"key": key, "by_note": by_note}, indent=2) + "\n",
                         encoding="utf-8")
        print(f"   written → {cache}")

    raw = sum(len(v) for v in by_note.values())
    print(f"   {len(by_note)}/{len(members)} notes answered, {raw} concept instances")

    sets = build_sets(by_note, len(members))
    edges = inclusion_edges(sets)
    G = nx.DiGraph()
    G.add_nodes_from(sets)
    G.add_edges_from(edges)
    isolated = [n for n in G if G.degree(n) == 0]
    cycles = list(itertools.islice(nx.simple_cycles(G), 3))
    roots = sorted((n for n in G if G.in_degree(n) == 0 and G.out_degree(n) > 0),
                   key=lambda n: (-G.out_degree(n), n))

    print(f"\n2. MAP  nodes {len(sets)}  edges {len(edges)}  "
          f"isolated {len(isolated)} ({100 * len(isolated) / max(len(sets), 1):.0f}%)  "
          f"cycles {len(cycles)}")
    print("   (reported, not a gate. YAKE baseline from the spec: 63% isolated)")

    W = edge_graph(nodes, edges_g)
    wiki = {n: set(W.neighbors(n)) for n in W}
    red = redundancy(edges, nodes, wiki)

    print(f"\n3. GATE 1 · roots recognizable (human). {len(roots)} roots, "
          f"showing {min(len(roots), ROOTS_SHOWN)}:")
    for i, r in enumerate(roots[:ROOTS_SHOWN], 1):
        print(f"   {i:>2}. {r}  (df={len(sets[r])}, unlocks {G.out_degree(r)})")
    shown = min(len(roots), ROOTS_SHOWN)
    g1: bool | None
    if len(roots) < MIN_ROOTS_FOR_G1:
        g1, g1_why = None, f"vacuous: {len(roots)} roots < {MIN_ROOTS_FOR_G1}"
        print(f"\n   → NOT EVALUABLE ({g1_why}). The hierarchy is too thin to have "
              f"a top to judge.")
    elif roots_ok is None:
        g1, g1_why = None, "awaiting the human count"
        print("\n   → count the ones you accept as macroconcepts of THIS vault, "
              "then rerun with --roots-ok N (cache makes it free).")
    else:
        g1 = roots_ok / shown >= GATE_ROOTS_OK
        g1_why = f"{roots_ok}/{shown}"
        print(f"\n   → {g1_why} = {roots_ok / shown:.2f} "
              f"(gate {GATE_ROOTS_OK:.2f}) {'PASS' if g1 else 'FAIL'}")

    g2: bool | None
    print(f"\n4. GATE 2 · non-redundant edges. {red['non_redundant']}/{red['edges']} = "
          f"{red['share']:.2f} (gate {GATE_NON_REDUNDANT:.2f})")
    print(f"   {red['both_endpoints_are_notes']} of those edges have a note at BOTH "
          f"ends — only they can be redundant, so only they carry the gate")
    if red["both_endpoints_are_notes"] < MIN_RESOLVABLE_FOR_G2:
        g2, g2_why = None, (f"vacuous: {red['both_endpoints_are_notes']} resolvable "
                            f"edges < {MIN_RESOLVABLE_FOR_G2}")
        print(f"   → NOT EVALUABLE ({g2_why}). Nothing here COULD have been "
              f"redundant, so passing would mean nothing.")
    else:
        g2, g2_why = red["share"] >= GATE_NON_REDUNDANT, "evaluated"
        print(f"   → {'PASS' if g2 else 'FAIL'}")
    for e in red["examples_the_note_graph_misses"][:5]:
        print(f"     {e['from']} → {e['to']}")

    if g1 is None or g2 is None:
        verdict = "INCONCLUSIVE"
        tail = ("  → the sample could not answer; a KILL needs a measured gate, "
                "not an empty one")
    elif g1 and g2:
        verdict, tail = "PASS", ""
    else:
        verdict, tail = "KILL", "  → lane closes, do not backfill the vault"
    print(f"\n   VERDICT: {verdict}{tail}")

    return {
        "provenance": provenance(vault),
        "vault": {"path": str(vault), "digest": digest, "notes": n_vault},
        "community": {"id": cid, "label": label, "notes": len(members)},
        "config": {"model": model, "df_min": DF_MIN, "df_max_share": DF_MAX_SHARE,
                   "nest_overlap": NEST_OVERLAP, "inclusion": INCLUSION,
                   "gate_roots_ok": GATE_ROOTS_OK,
                   "gate_non_redundant": GATE_NON_REDUNDANT},
        "extraction": {"notes_answered": len(by_note), "concept_instances": raw},
        "map": {"nodes": len(sets), "edges": len(edges),
                "isolated": len(isolated), "acyclic": not cycles,
                "roots": [{"concept": r, "df": len(sets[r]), "unlocks": G.out_degree(r)}
                          for r in roots]},
        "gates": {
            "G1_roots_recognizable": {"roots": len(roots), "shown": shown,
                                      "accepted": roots_ok, "pass": g1, "why": g1_why},
            "G2_non_redundant": {**red, "pass": g2, "why": g2_why},
        },
        "verdict": verdict,
    }


def main(argv=None) -> int:
    from evals.golden.runner import resolve_vault
    from silica.config import CONFIG

    ap = argparse.ArgumentParser(prog="python -m evals.probe_concept_map")
    ap.add_argument("--vault")
    ap.add_argument("--community", type=int, default=None,
                    help="Louvain id (default: the largest)")
    ap.add_argument("--model", default=None, help="extractor model (default CONFIG.model)")
    ap.add_argument("--jobs", type=int, default=4, help="concurrent LLM calls")
    ap.add_argument("--roots-ok", type=int, default=None,
                    help="how many shown roots you accept as macroconcepts (gate 1)")
    ap.add_argument("--cache", default="bench/concept_map_extraction.json")
    ap.add_argument("--json", default="bench/concept_map.json")
    args = ap.parse_args(argv)

    vault = resolve_vault(args.vault)
    CONFIG.vault_path = str(vault)
    try:
        res = run(vault, cid=args.community, model=args.model or CONFIG.model,
                  jobs=args.jobs, roots_ok=args.roots_ok, cache=Path(args.cache))
    finally:
        import silica.driver
        silica.driver._driver = None
    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")
    print(f"\nwritten → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
