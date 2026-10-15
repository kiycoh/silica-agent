# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Learner-model derived view (docs/specs/learner-model.md, learn-suite D10).

Per-note retention is the FSRS-6 retrievability of a card replayed from the
graded-quiz ledger (quiz.jsonl), note creation dates and the `AI: true`
authorship stamp — see silica/kernel/learn/schedule.py, which this module
delegates to so the digest, the calendar and the metrics attention list keep
their import. Never materialized: a second store of the same state diverges
from the ledger at the first crash, so the view is recomputed by scan on
read, exactly like quiz.stats().

Learning events are the note's creation (reader-authored notes only: writing
is encoding, and an AI-written note was never learned) and rated answers.
Nothing else — passive exposure is the illusion of competence this exists to
kill, so a chat answer citing a note is NOT evidence the reader knows it.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from silica.kernel.learn.schedule import DUE_R, WEAK_R, note_state, view  # noqa: F401  re-exported v1 names

logger = logging.getLogger(__name__)

DAY = 86400.0


def key_of(path: str) -> str:
    """The join keyspace: quiz.key (posix, no .md, casefolded)."""
    from silica.kernel.report import quiz

    return quiz.key(path)


def _created_and_ai(text: str, mtime: float) -> tuple[float, bool]:
    """A note's creation timestamp and authorship from its own frontmatter."""
    ts, ai, _kind = _note_meta(text, mtime)
    return ts, ai


def _note_meta(text: str, mtime: float) -> tuple[float, bool, str]:
    """(created ts, AI-authored, frontmatter `type`) from one parse.

    Same date precedence as the timeline: explicit `date:` outranks the claim
    clock, and a note that states neither falls back to mtime — the ceiling
    AttentionCandidate already lives with. The type rides along so the
    pickers can keep plan and syllabus notes out of the study material.
    """
    from silica.kernel.write import frontmatter
    from silica.kernel.write.contested import note_clock

    data, _raw, _body = frontmatter.split(text)
    date = (data or {}).get("date") or note_clock(text)
    ts = mtime
    if date:
        try:
            ts = datetime.strptime(str(date)[:10], "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            pass  # unparseable statement: keep the mtime proxy
    # An event note states no `type`; its `event_start` is what makes it one
    # (calendar/model.py), so that key names its kind here.
    kind = "event" if (data or {}).get("event_start") is not None else (data or {}).get("type")
    return ts, bool((data or {}).get("AI")), str(kind or "").strip().lower()


# Notes about learning, never asked about; and the calendar's own notes, which
# Learn's "never practised" listed as study material until 2026-10-15.
NOT_MATERIAL = ("plan", "syllabus", "event")


def is_material(meta: dict) -> bool:
    return meta.get("kind") not in NOT_MATERIAL


_meta_memo: dict[str, tuple[str, dict[str, dict]]] = {}  # vault -> (epoch, meta)


def _notes_meta(vault: Path) -> dict[str, dict]:
    """{relative path: {"created": ts, "ai": bool}} for every readable note.

    Timeline's walk verbatim: dot-dirs, .silicaignore matches and the verbatim
    sources dir are not the reader's notes. Memoized on the vault's file-state
    epoch like timeline._all_rows: repeated digest reads between vault changes
    cost one stat walk instead of a full re-parse.
    """
    from silica.kernel.recall.paths import SOURCES_DIR, ignore_matcher, vault_epoch

    epoch = vault_epoch(str(vault))
    if epoch:
        hit = _meta_memo.get(str(vault))
        if hit is not None and hit[0] == epoch:
            return hit[1]

    ignored = ignore_matcher(vault)
    out: dict[str, dict] = {}
    for f in sorted(vault.rglob("*.md")):
        parts = f.relative_to(vault).parts
        if any(p.startswith(".") for p in parts):
            continue
        if any(ignored(p) for p in parts[:-1]):
            continue
        if parts[0] == SOURCES_DIR:
            continue
        try:
            text = f.read_text(encoding="utf-8")
            mtime = f.stat().st_mtime
        except (OSError, UnicodeDecodeError):
            continue  # one unreadable note must not blind the view
        created, ai, kind = _note_meta(text, mtime)
        # mtime rides along for the picker's freshness rule (learn-suite D13):
        # a note edited after its last rating is asked as new material.
        out["/".join(parts)] = {"created": created, "ai": ai, "mtime": mtime, "kind": kind}

    if epoch:
        _meta_memo.clear()
        _meta_memo[str(vault)] = (epoch, out)
    return out



_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _measured_stems(entries: list[dict], lang: str) -> set[str]:
    """Stems of every concept a logged question ever tested.

    Concepts are logged raw, as the asking model named them; resolution to the
    cooccur keyspace happens here, at read — so every future improvement to
    stemming re-resolves the whole history for free.
    """
    from silica.kernel.text.text import stem_word

    stems: set[str] = set()
    for e in entries:
        for name in e.get("concepts") or []:
            for tok in _TOKEN_RE.findall(str(name).lower()):
                if len(tok) > 2:
                    stems.add(stem_word(tok, lang=lang))
    return stems


def gain_fn(entries: list[dict], store=None):
    """row -> information gain: the co-occurrence mass of the note's concepts
    that no logged question ever measured (spec D5). 0 everywhere without an
    index, so the callers rank by authorship alone."""
    if store is None:
        try:
            from silica.config import CONFIG
            from silica.kernel.recall.cooccurrence import get_cooccur_store

            store = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
        except Exception:
            store = None  # no index yet: rank unexplored by authorship alone

    measured: set[str] = set()
    adj_mass: dict[str, float] = {}
    if store is not None:
        try:
            measured = _measured_stems(entries, getattr(store, "lang", "en"))
            adj_mass = {s: sum(nb.values()) for s, nb in store.adjacency().items()}
        except Exception:
            store = None

    def gain(r: dict) -> float:
        if store is None:
            return 0.0
        try:
            nodes = store.note_nodes(r["path"])
        except Exception:
            return 0.0
        return sum(adj_mass.get(s, 0.0) for s in nodes if s not in measured)

    return gain


_prereq_memo: dict[str, tuple[str, dict[str, list[str]]]] = {}  # vault -> (epoch, map)


def prerequisites_map() -> dict[str, list[str]]:
    """{dependent: [prerequisites]} (V2, RefD), store keyspace.

    Computes the store-derived variables alone rather than the whole
    co-occurrence report: the AUTOLINK delta it would drag in costs ~5 s on a
    700-note vault and the picker does not read it. Memoized on the vault
    epoch so a quiz round pays the pass once per vault change. {} when the
    graph or the index is unavailable: the queue then orders by gain alone.
    """
    try:
        from silica.config import CONFIG
        from silica.kernel.recall.paths import vault_epoch

        vault = str(CONFIG.vault_path or "")
        epoch = vault_epoch(vault) if vault else ""
        hit = _prereq_memo.get(vault)
        if epoch and hit is not None and hit[0] == epoch:
            return hit[1]

        from silica.kernel.recall.cooccurrence import get_cooccur_store
        from silica.kernel.recall.graph_export import build_graph_data, edge_graph
        from silica.kernel.report.graph_report.compute import _empty_report
        from silica.kernel.report.graph_report.cooccur_delta import _compute_cooccur_variables

        nodes, edges = build_graph_data()
        G_dir = edge_graph(nodes, edges, directed=True)
        store = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
        _prereqs, prereq_map, _burst = _compute_cooccur_variables(
            _empty_report(), G_dir.to_undirected(), G_dir, cooccur_store=store, created=None,
        )
        if epoch:
            _prereq_memo.clear()
            _prereq_memo[vault] = (epoch, prereq_map)
        return prereq_map
    except Exception as exc:
        logger.debug("learner: prerequisites unavailable (%s)", exc)
        return {}


def _topological(rows: list[dict], prereqs: dict[str, list[str]], order_key) -> list[dict]:
    """Kahn's order over the prerequisite edges among `rows`, ties by `order_key`.

    A note with an unknown or out-of-scope prerequisite is free (no edge). A
    cycle (RefD is antisymmetric per pair but chains can close) leaves the
    remaining rows in `order_key` order at the end, so the picker never stalls.
    """
    by_key = {key_of(r["path"]): r for r in rows}
    indeg: dict[str, int] = {k: 0 for k in by_key}
    dependents: dict[str, list[str]] = {k: [] for k in by_key}
    for k in by_key:
        for p in prereqs.get(k, ()):
            pk = key_of(p)
            if pk in by_key and pk != k:
                indeg[k] += 1
                dependents[pk].append(k)
    out: list[dict] = []
    ready = sorted((k for k, d in indeg.items() if d == 0), key=lambda k: order_key(by_key[k]))
    while ready:
        k = ready.pop(0)
        out.append(by_key[k])
        for d in dependents[k]:
            indeg[d] -= 1
            if indeg[d] == 0:
                ready.append(d)
        ready.sort(key=lambda k: order_key(by_key[k]))
    if len(out) < len(rows):
        done = {key_of(r["path"]) for r in out}
        out += sorted((r for r in rows if key_of(r["path"]) not in done), key=order_key)
    return out


def off_plan_dropped(rows: dict[str, dict], planned: set[str] | None) -> dict[str, dict]:
    """The rows a picker with no scope may offer (ADR-0111 point 4).

    Once a plan is active, a note never practised is study material only if
    a plan covers it: a note added to be summarised or kept for reference
    does not become a goal by being in the vault. A note already practised
    stays, because practising it was the reader's choice. `planned` is
    `plans.planned_paths()`, None when no plan is active, and then every note
    is material, as before. The caller reads it: this module is under the
    graph contract (pyproject, "graph data never reaches silica.agent"), and
    plans reaches the agent through learn.items.
    """
    if planned is None:
        return rows
    return {k: r for k, r in rows.items() if r["attempts"] or r["path"] in planned}


def review_queue(
    limit: int = 10,
    target: str = "",
    now_ts: float | None = None,
    _notes_override: dict | None = None,
    _entries_override: list | None = None,
    _store=None,
    _prereqs_override: dict[str, list[str]] | None = None,
    planned: set[str] | None = None,
) -> list[dict]:
    """The picker: what to quiz next, or (with target=) an area's full state.

    Global mode draws half from **due** (R below threshold, worst first) and
    half from **unexplored** (zero evidence, AI-unknown first, then notes whose
    concepts no question ever measured, central first). Target mode returns
    every note under the path prefix with its R and pool, unknown first — the
    calibration read /learn builds a syllabus from. `planned` narrows global
    mode as off_plan_dropped says: pass `plans.planned_paths()`.
    """
    from silica.kernel.report import quiz

    entries = _entries_override if _entries_override is not None else quiz.entries()
    notes_meta = _notes_override
    if notes_meta is None:
        from silica.config import CONFIG

        notes_meta = _notes_meta(Path(CONFIG.vault_path))
    rows = view(now_ts=now_ts, _notes_override=notes_meta, _entries_override=entries)

    def why(r: dict) -> str:
        if r["R"] is not None and r["R"] < DUE_R:
            return "due"
        if r["attempts"] == 0:
            return "unexplored"
        return "known"

    rows = {k: r for k, r in rows.items() if is_material(notes_meta.get(r["path"], {}))}
    if not target:
        rows = off_plan_dropped(rows, planned)
    for r in rows.values():
        r["why"] = why(r)

    # V2: a note is ready when every prerequisite the vault knows is itself
    # known (R at or above the due line). Unknown prerequisites (not a note in
    # this view) do not block: absence of evidence is not a blocker.
    prereqs = _prereqs_override if _prereqs_override is not None else prerequisites_map()
    prereq_paths = {key_of(k): list(v) for k, v in prereqs.items()}   # display form
    prereq_keys = {k: [key_of(p) for p in v] for k, v in prereq_paths.items()}
    for k, r in rows.items():
        mine = prereq_keys.get(k, [])
        r["prereqs"] = prereq_paths.get(k, [])
        r["ready"] = all(
            (rows[p]["R"] is not None and rows[p]["R"] >= DUE_R)
            for p in mine if p in rows and p != k
        )

    if target:
        t = target.casefold()
        scoped = [r for r in rows.values() if r["path"].casefold().startswith(t)]
        state_key = lambda r: (r["R"] is not None, r["R"] if r["R"] is not None else 0.0)
        # Prerequisite order is the syllabus order /learn asks for; within a
        # rank the old unknown-first order stands.
        return _topological(scoped, prereq_keys, state_key)

    gain = gain_fn(entries, _store)

    # Blocked notes (a prerequisite not yet known) sink below ready ones in
    # both pools: quizzing B before A is measured is the adaptive-testing
    # mistake the learner model exists to avoid (spec D5).
    due = sorted((r for r in rows.values() if r["why"] == "due"),
                 key=lambda r: (not r["ready"], r["R"]))
    unexplored = sorted(
        (r for r in rows.values() if r["why"] == "unexplored"),
        key=lambda r: (not r["ready"], not r["ai"], -gain(r), r["path"]),
    )
    n_due = min(len(due), max(1, limit // 2)) if due else 0
    picked = due[:n_due] + unexplored[: limit - n_due]
    if len(picked) < limit:  # one pool ran short: the other fills the round
        picked += due[n_due : n_due + (limit - len(picked))]
    return picked
