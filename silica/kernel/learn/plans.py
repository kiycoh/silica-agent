# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Plans (learn-suite D23, D24): scope resolution and the plan note.

A plan is a `type: plan` note (legacy `type: syllabus`, recognized forever):
scope, tree, objectives (D27), optional steps with a because clause, session
lines. It stores no mastery; every reading on it is derived from the ledger
filtered by its scope, so two plans over overlapping folders never disagree
and deleting a plan loses no evidence.

`reach:` (a SOLO rung) is the legacy shape of `objectives:`; a plan without
`objectives:` reads its rung as intentions (objectives.REACH_TO_INTENTIONS)
and is never rewritten.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NamedTuple

from silica.kernel.base import note_index
from silica.kernel.base.notekey import ledger_key
from silica.kernel.base.noteshape import is_plan
from silica.kernel.learn import store
from silica.kernel.learn.pace import budget, pace_of, today_line
from silica.kernel.learn.reference import (HEADING_LINE_RE, RESTRICT_SEP, STEP_LINK_RE, assessment_text, note_title,
                                           parse_assessment, parse_steps, reference_rows, section_lines, section_links,
                                           step_label, step_marks)
from silica.kernel.learn.sources import resolve_title, steps_scope
from silica.kernel.write.gate import journaled_write

logger = logging.getLogger(__name__)

SCOPES = ("notes", "folder", "topic", "steps", "workspace")
REACH = {"unistructural": 1, "multistructural": 2, "relational": 3, "extended": 4}
REACH_LABEL = {1: "Facts", 2: "Parts", 3: "Mechanisms", 4: "New contexts"}


def find_plan(name: str) -> dict | None:
    """The plan note a REPL target names: by vault path or by title/stem."""
    for p in list_plans():
        if p["path"] == name or Path(p["path"]).stem.casefold() == name.casefold() \
                or p["name"].casefold() == name.casefold():
            return p
    return None


def list_plans() -> list[dict]:
    """Every `type: plan` (or legacy syllabus) note in the vault.

    ponytail: scans every note's frontmatter on request; index it when the
    vault passes 5k notes.
    """

    vault = store.vault()
    out = []
    for path in note_index.meta(vault):
        parsed = note_index.parsed(vault, path, body=False)
        if parsed is None or not is_plan(parsed[0]):
            continue
        try:
            text = (vault / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        out.append(read_plan(path, text))
    return out


def read_plan(path: str, text: str | None = None) -> dict:
    """The note as a plan: scope kind, target, reach, deadline, steps, sessions."""
    from silica.kernel.base import frontmatter
    from silica.kernel.learn import session

    if text is None:
        text = (store.vault() / path).read_text(encoding="utf-8")
    data, _raw, body = frontmatter.split(text)
    data = data or {}
    legacy = str(data.get("type") or "").lower() == "syllabus"
    scope = str(data.get("scope") or ("steps" if legacy else "folder")).strip().lower()
    if scope not in SCOPES:
        scope = "notes"
    target = str(data.get("target") or "")
    # a syllabus pasted as text keeps its line break, which is what tells it
    # from a note's name: one line equal to a title was re-read as that note
    # on Edit plan (ADR-0119, found 2026-10-16); every other target is a name
    target = target.strip(" \t") if scope == "steps" and "\n" in target.strip(" \t") else target.strip()
    reach = str(data.get("reach") or "relational").strip().lower()
    from silica.kernel.learn import objectives as obj

    chosen = obj.normalize(data.get("objectives"))
    cards = obj.parse_section(section_lines(body, "Objectives"))
    if not chosen and "objectives" not in (data or {}):
        # a plan from before D27: the rung names the intentions; a plan
        # written with the choice left to Silica carries `objectives: []`
        # and its cards say proposed
        chosen = list(obj.REACH_TO_INTENTIONS.get(reach, ["explain"]))
    objectives = chosen or obj.normalize([c["intention"] for c in cards if c["source"] != "gap"]) or ["explain"]
    return {
        "path": path, "name": note_title(body) or Path(path).stem,
        "scope": scope, "target": target if not legacy else (target or path),
        "reach": reach, "reach_level": obj.level_of(objectives),
        "objectives": objectives, "objectives_chosen": bool(chosen), "cards": cards,
        **{k.name: k.read(data.get(k.name)) for k in PLAN_KEYS},
        "created": str(data.get("created") or data.get("date") or "")[:10] or None,
        "status": str(data.get("status") or "active"),
        "completed": str(data.get("completed") or "") or None,
        "revisit": [str(x) for x in (data.get("revisit") or [])],
        "assessment_set": "assessment" in data,
        "steps": parse_steps(body), "sessions": session.parse_lines(section_lines(body, "Sessions")),
        "tree_links": section_links(body, "Tree"),
        "legacy": legacy,
    }


def parse_matched(value) -> dict[str, str]:
    """{topic: note title} from a plan's `matched:` ("topic -> Note | ..."):
    the reader's answer to "is it one of these?" for a topic no source met
    (ADR-0131 amendment, point 5). Plain titles, no links: a link in a plan
    key would be one more note of the plan."""
    if isinstance(value, dict):
        return {str(k).strip(): str(v).strip() for k, v in value.items() if str(k).strip() and str(v).strip()}
    out = {}
    for bit in str(value or "").split(" | "):
        if " -> " in bit:
            t, n = bit.split(" -> ", 1)
            if t.strip() and n.strip():
                out[t.strip()] = n.strip().strip("[]")
    return out


def matched_text(matched: dict[str, str]) -> str:
    return " | ".join(f"{t} -> {n}" for t, n in matched.items())


def hours_of(value) -> float | None:
    try:
        h = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return h if 0 < h <= 24 else None


@dataclass
class PlanSpec:
    """What a plan is made from: the fields of Create plan and Edit plan, one
    object through the form, `compose_plan`, `write_plan` and `edit_plan`.

    None is a field the reader left alone: creation reads it off the material
    and Edit plan keeps what the plan says. `declared` the roles of the
    collections, `hours` study hours a day, `assessment` its parts ("none"
    clears them), `guide` a note whose plan table gives each step's depth
    (ADR-0131); `refused` holds line indexes from the form or texts from the
    note, `matched` the reader's answers for topics no source met."""
    name: str
    scope: str
    target: Any
    by: str | None = None
    restrict: str = ""
    objectives: Any = None
    cards: list[dict] | None = None
    refused: Any = None
    declared: dict[str, str] | None = None
    declared_why: str = "you set it"
    hours: Any = None
    assessment: Any = None
    guide: str | None = None
    matched: dict[str, str] | None = None
    reach: str = "relational"
    folder: str = "Plans"
    templates: bool = False


def resolve(spec: PlanSpec) -> dict:
    """`resolve_scope` for what a spec says."""
    return resolve_scope(spec.scope, spec.target, restrict=spec.restrict, refused=spec.refused or (),
                         declared=spec.declared, declared_why=spec.declared_why, matched=spec.matched)


@dataclass(frozen=True)
class _Made:
    """What a plan key's value is written from."""
    spec: PlanSpec
    resolved: dict
    assessment: str | None


class PlanKey(NamedTuple):
    """One optional frontmatter key of a plan: how `read_plan` reads it and what
    the writers store, "" for no key. A string, because the gate stringifies a
    list prop."""
    name: str
    read: Callable[[Any], Any]
    write: Callable[[_Made], Any]


def _text(value) -> str:
    return str(value or "")


def _role_key(made: _Made, key: str) -> str:
    return _role_keys(made.resolved.get("sources") or []).get(key, "")


def _hours_key(made: _Made) -> str:
    h = hours_of(made.spec.hours)
    return f"{h:g}" if h else ""


def _guide_key(made: _Made) -> str:
    guide = made.spec.guide
    return (guide if str(guide).startswith("[[") else f"[[{Path(guide).stem}]]") if guide else ""


# The one table of a plan's optional keys, in the order creation writes them. A
# new key is a row here, and `preserve._LOSS_KEYS[Loss.PLAN]` beside it, which
# sits below Learn and is held to this table by a test.
PLAN_KEYS = (
    PlanKey("by", lambda v: _text(v) or None, lambda m: _text(m.spec.by)),
    PlanKey("restrict", _text, lambda m: m.spec.restrict),
    # a "not this" is the reader's verdict: kept on the note so Edit plan and
    # Reword re-resolve without the false match (ADR-0081 review)
    PlanKey("refused", lambda v: [x.strip() for x in _text(v).split(" | ") if x.strip()],
            lambda m: " | ".join(_refused_lines(m.resolved))),
    # ADR-0131: the collections' roles, study hours a day, the assessment
    # read off the reference (or "none"), a guide for each step's depth
    PlanKey("materials", _text, lambda m: _role_key(m, "materials")),
    PlanKey("references", _text, lambda m: _role_key(m, "references")),
    PlanKey("unused", _text, lambda m: _role_key(m, "unused")),
    PlanKey("matched", lambda v: parse_matched(v), lambda m: matched_text(_matched_kept(m.resolved))),
    PlanKey("hours", lambda v: hours_of(v), _hours_key),
    PlanKey("assessment", lambda v: parse_assessment(v), lambda m: m.assessment),
    PlanKey("guide", _text, _guide_key),
)
# The keys every plan carries, which an edit rewrites whatever the form said.
ALWAYS_KEYS = ("scope", "target", "objectives")


# ---------------------------------------------------------------- scope resolution


def notes_of(target) -> list[str]:
    """The titles of a notes scope: a list as given, wikilinks as written
    since ADR-0081's review, else the comma string of older plans. A title
    can hold a comma ("Sensazione, percezione, psicofisica"), so a list is
    never joined on commas again."""
    if isinstance(target, list):
        return [str(t).strip() for t in target if str(t).strip()]
    text = str(target or "")
    return [w.strip() for w in STEP_LINK_RE.findall(text)] or [t.strip() for t in re.split(r"[,\n]", text) if t.strip()]


def target_text(scope: str, target) -> str:
    """A target as the frontmatter keeps it: notes as wikilinks, so a comma
    inside a title survives the round trip."""
    if scope == "notes":
        return ", ".join(f"[[{n}]]" for n in notes_of(target))
    if scope == "workspace":
        # A wikilink, so renaming the workspace note rewrites the plan too, and
        # by path: a plan named after its workspace shares its name, and
        # [[Statistics exam]] would then name the plan itself.
        from silica.kernel.workspaces import find_workspace, title_of

        ws = find_workspace(str(target))
        return f"[[{ws['path'].removesuffix('.md') if ws else title_of(str(target))}]]"
    return target if isinstance(target, str) else ", ".join(target)


def resolve_scope(kind: str, target, restrict: str = "", refused=(), declared: dict[str, str] | None = None,
                  declared_why: str = "you set it", matched: dict[str, str] | None = None) -> dict:
    """{paths, tree, gaps} for a scope (D23). `target` is the scope's argument:
    paths for notes, a prefix for folder, a term for topic, a note or a
    directive for steps. `restrict` bounds the material: a folder prefix for
    "X relative to Y", or several prefixes joined by " | " when a reference
    is bounded by the notes studied rather than one folder (ADR-0119);
    `refused` the indexes of syllabus lines the reader marked "not this",
    kept as gaps (ADR-0081 §13); `declared` the roles the reader gave the
    collections of sources, {label: role} (ADR-0131).

    ponytail: a steps plan resolves each line by title index and search
    context; a step that names a concept with no note of its own is a gap
    until new-material targets (extension 1) exist.
    """
    from silica.kernel.base.noteshape import is_material

    notes = note_index.meta(store.vault())
    # A note's path is a prefix of itself only, so a list of notes and a
    # folder bound a reference by the same test
    bounds = tuple(b.strip().casefold() for b in str(restrict or "").split(RESTRICT_SEP) if b.strip())
    # A plan is the card, never the material: the topic "memoria" listed
    # Plans/Prova Learn.md among its notes (2026-10-08) because the plan names
    # the topic. The walk already records `type` for exactly this, and an
    # event note's kind with it.
    allowed = {p for p, m in notes.items() if is_material(m)
               and (not bounds or p.casefold().startswith(bounds))}
    if kind == "notes":
        names = notes_of(target)
        paths = [p for p in (resolve_title(n, notes) for n in names) if p and p in allowed]
        return {"paths": paths, "tree": [{"node": Path(p).stem, "children": [], "leaves": [p]} for p in paths], "gaps": []}
    if kind == "folder":
        paths, seen, tree = [], set(), []
        for prefix in folder_parts(target, notes):
            got = sorted(p for p in allowed if p.casefold().startswith(prefix.casefold()) and p not in seen)
            seen.update(got)
            paths += got
            if prefix.casefold().endswith(".md"):
                tree += [{"node": Path(prefix).stem, "children": [], "leaves": got}] if got else []
            else:
                tree += _folder_tree(prefix, got)
        return {"paths": paths, "tree": tree, "gaps": []}
    if kind == "topic":
        return _topic_scope(str(target), allowed)
    if kind == "steps":
        return steps_scope(str(target), notes, allowed, set(refused or ()), declared, declared_why, matched)
    if kind == "workspace":
        return _workspace_scope(str(target), allowed)
    raise ValueError(f"unknown scope kind {kind!r}")


def folder_parts(target, notes: dict | None = None) -> list[str]:
    """A folder scope's prefixes: one folder, or folders and notes studied
    together, joined by " | " as `restrict` joins its bounds, rather than a
    scope kind of their own (ADR-0119, amendment 2026-10-17). A note part is
    turned into its path, a prefix of itself only, so it may be named by its
    title: a notes scope keeps titles, and adding a folder to one keeps them."""
    parts = [b.strip() for b in str(target or "").replace("\\", "/").split(RESTRICT_SEP) if b.strip()]
    # an empty target was every note, which a legacy plan without one relied on
    return [(resolve_title(b, notes) or b) if b.casefold().endswith(".md") else b for b in parts] or [""]


def _workspace_scope(target: str, allowed: set[str]) -> dict:
    """A workspace's members (ADR-0112 point 5), resolved live each read, so
    a note added to the workspace joins the plan's frontier without an edit.
    One tree node per folder the members sit in."""
    from silica.kernel import workspaces

    ws = workspaces.find_workspace(target)
    if ws is None:
        return {"paths": [], "tree": [], "gaps": [f"no workspace named {target}"]}
    paths = sorted(p for p in workspaces.members(ws) if p in allowed)
    groups: dict[str, list[str]] = {}
    for p in paths:
        groups.setdefault(p.rsplit("/", 1)[0] if "/" in p else "/", []).append(p)
    return {"paths": paths, "tree": [{"node": f, "children": [], "leaves": ls} for f, ls in sorted(groups.items())],
            "gaps": []}


def _folder_tree(prefix: str, paths: list[str]) -> list[dict]:
    """Subfolders as children, notes as leaves, one level below the prefix."""
    groups: dict[str, list[str]] = {}
    for p in paths:
        rest = p[len(prefix):].lstrip("/") if p.casefold().startswith(prefix.casefold()) else p
        child = rest.split("/", 1)[0] if "/" in rest else ""
        groups.setdefault(child, []).append(p)
    out = []
    for child, leaves in sorted(groups.items()):
        node = (prefix.rstrip("/") + "/" + child) if child else prefix.rstrip("/") or "/"
        out.append({"node": node, "children": [], "leaves": leaves})
    return out


def _topic_scope(term: str, allowed: set[str]) -> dict:
    """The notes carrying the term in the co-occurrence store, plus the MOC
    that names it; neighbour concepts as children."""
    try:
        from silica.config import CONFIG
        from silica.kernel.recall.cooccurrence import get_cooccur_store
        from silica.kernel.text.text import stem_word

        cooccur = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
        stem = stem_word(term.strip().lower(), lang=cooccur.lang)
        posting = cooccur.stem_postings().get(stem, {})
        neighbours = cooccur.neighbors(term, k=8)
    except Exception as exc:
        logger.debug("learn: topic scope without the store (%s)", exc)
        posting, neighbours = {}, []

    notes = note_index.meta(store.vault())
    # Carriers ranked by how much of the term they carry, the way silica_concepts lists them.
    ranked = [p for p, _c in sorted(posting.items(), key=lambda kv: (-kv[1], kv[0]))]
    paths: list[str] = []
    for p in (resolve_title(p, notes) or p for p in ranked):
        if p in allowed and p not in paths:
            paths.append(p)
    moc = resolve_title(term, notes)
    if moc and moc in allowed and moc not in paths:
        paths.insert(0, moc)
    # A topic's parts are the folders its notes sit in: the co-occurrence
    # neighbours of "memoria" were `termini`, `dati`, `principali` (2026-10-08),
    # tokens the preview called folders. They are offered as chips instead.
    by_folder: dict[str, list[str]] = {}
    for p in paths:
        by_folder.setdefault(p.rsplit("/", 1)[0] if "/" in p else "(root)", []).append(p)
    # a folder holding one carrier is not a part: "memoria" sat in 23 folders,
    # 16 of them with one note, and each part is an objective card
    tree = [{"node": f, "children": [], "leaves": leaves}
            for f, leaves in sorted(by_folder.items(), key=lambda kv: (-len(kv[1]), kv[0])) if len(leaves) > 1]
    single = [p for leaves in by_folder.values() if len(leaves) == 1 for p in leaves]
    if single:
        tree.append({"node": "elsewhere" if tree else (single[0].rsplit("/", 1)[0] if len(single) == 1 else "elsewhere"),
                     "children": [], "leaves": single})
    return {"paths": paths, "tree": tree, "gaps": [],
            "neighbours": [str(nb["concept"]) for nb in neighbours if str(nb.get("concept") or "").strip()]}


def topics_like(q: str, k: int = 4) -> list[dict]:
    """The Study typeahead's topics (ADR-0081 §12): concepts whose stem the
    query prefixes, with how many notes carry each, most carried first."""
    try:
        from silica.config import CONFIG
        from silica.kernel.recall.cooccurrence import get_cooccur_store
        from silica.kernel.text.text import stem_word

        store = get_cooccur_store(lang=CONFIG.cooccurrence_lang)
        stem = stem_word(q.strip().lower(), lang=store.lang)
        post = store.stem_postings()
        hits = sorted((s for s in post if s.startswith(stem)), key=lambda s: -len(post[s]))[:k]
        return [{"label": store.node_label(s), "count": len(post[s])} for s in hits]
    except Exception as exc:
        # no store yet (a fresh vault): folders and notes still answer
        logger.debug("learn: typeahead without topics (%s)", exc)
        return []


def scope_of(plan: dict, notes: dict | None = None) -> dict:
    """The paths a plan covers now, its frontier and its revisit queue.
    A steps plan's scope is its steps' notes plus the tree's; the frontier
    is the first unpassed step, else the notes furthest below reach.

    The vault's notes are read once and handed to every title lookup: each
    lookup used to read them itself, a vault walk per note, and a plan of 16
    syllabus steps over 60 notes took 56 s to open (2026-10-23, profiled)."""

    notes = notes if notes is not None else note_index.meta(store.vault())
    if plan["scope"] == "steps":
        paths: list[str] = []
        for s in plan["steps"]:
            for n in s["notes"]:
                p = resolve_title(n, notes)
                if p and p not in paths:
                    paths.append(p)
        for n in plan["tree_links"]:
            p = resolve_title(n, notes)
            if p and p not in paths:
                paths.append(p)
        if not paths and plan["target"]:
            paths = resolve_scope("steps", plan["target"], restrict=plan.get("restrict", ""))["paths"]
    else:
        target = plan["target"]
        if plan["scope"] == "notes":
            target = notes_of(target) or plan["tree_links"]
        paths = resolve_scope(plan["scope"], target, restrict=plan.get("restrict", ""))["paths"]
    frontier = frontier_of(plan, paths, notes=notes)
    revisit = [p for p in (resolve_title(n, notes) for n in plan["revisit"]) if p]
    return {"paths": paths, "frontier": frontier, "revisit": revisit}


def planned_paths() -> set[str] | None:
    """Every note an active plan covers, or None when no plan is active: the
    line between study material and the rest of the vault (ADR-0111 point 4)."""
    active = [p for p in list_plans() if p["status"] != "completed"]
    if not active:
        return None
    out: set[str] = set()
    for p in active:
        try:
            out.update(scope_of(p)["paths"])
        except Exception as exc:
            # A plan whose scope will not resolve covers nothing here, as it
            # covers nothing in Learn's rows (session.plan_scopes): its
            # never-practised notes wait out of Next until the plan reads.
            logger.debug("planned_paths: scope of %s not resolved (%s)", p["path"], exc)
    return out


def frontier_of(plan: dict, paths: list[str], entries: list[dict] | None = None,
                notes: dict | None = None) -> list[str]:
    """Where the plan resumes (D24): the first unpassed step's notes when it
    has steps, else the scoped notes with the fewest objectives met."""
    from silica.kernel.learn import mastery

    if plan["steps"]:
        passed = mastery.steps_passed(plan, entries, notes=notes)
        for s, ok in zip(plan["steps"], passed):
            if not ok:
                found = [p for p in (resolve_title(n, notes) for n in s["notes"]) if p]
                if found:
                    return found
        return []
    met = mastery.objectives_met(paths, plan["objectives"], entries, since=plan.get("created"))
    below = sorted(paths, key=lambda p: (sum(1 for o in plan["objectives"] if met[p][o]["met"]), p))
    return below[:2]


def assign_objectives(plan: dict, picked: list[dict], entries: list[dict] | None = None) -> list[dict]:
    """A plan session's rows carry the objective each note lacks next (D27):
    a recall row becomes that objective's kind at its level, capped at the
    plan's; a contrast, contradiction or predict row keeps its kind and
    reads as the intention that kind is evidence for."""
    from silica.kernel.learn import objectives as obj

    for it in picked:
        if it.get("kind") in ("recall",):
            o = next_objective(plan, it["path"], entries)
            it["objective"], it["kind"] = o, obj.KIND_OF[o] if o != "compare" else "explain"
            it["level"] = min(obj.LEVEL_OF[o], plan["reach_level"])
        else:
            it["objective"] = obj.of_kind(it.get("kind", "recall"))
            it["level"] = min(it.get("level", 1), plan["reach_level"])
    return picked


def next_objective(plan: dict, path: str, entries: list[dict] | None = None) -> str:
    """The plan's first objective the note has not met without help since
    the plan was written; the first objective when all are met."""
    from silica.kernel.learn import mastery

    met = mastery.objectives_met([path], plan["objectives"], entries, since=plan.get("created")).get(path, {})
    return next((o for o in plan["objectives"] if not met.get(o, {}).get("met")), plan["objectives"][0])


# ---------------------------------------------------------------- the plan note, read

def _read(path: str) -> str:
    return (store.vault() / path).read_text(encoding="utf-8")


def cycles_among(paths: list[str]) -> list[list[str]]:
    """Prerequisite cycles among the plan's notes (V2 edges), reported on
    the plan rather than silently linearized (os-taxonomy's lesson)."""
    from silica.kernel.learn import learner

    try:
        # the map is keyed by path in the note's own case and ledger_key folds it:
        # read raw, no key of it matched and no cycle was ever found
        prereqs = {ledger_key(k): v for k, v in learner.prerequisites_map().items()}
    except Exception:
        return []
    keys = {ledger_key(p): p for p in paths}
    graph = {k: [ledger_key(x) for x in prereqs.get(k, []) if ledger_key(x) in keys] for k in keys}
    out: list[list[str]] = []
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(k: str) -> None:
        state[k] = 1
        stack.append(k)
        for nb in graph.get(k, []):
            if state.get(nb) == 1:
                cyc = stack[stack.index(nb):]
                out.append([keys[c] for c in cyc])
            elif nb not in state:
                visit(nb)
        stack.pop()
        state[k] = 2

    for k in keys:
        if k not in state:
            visit(k)
    return out


def _placed_of(plan: dict, paths: list[str], objectives: list[str], entries: list[dict],
               since: str | None, step_paths: list[list[str]]) -> dict | None:
    """The steps a placement placed (ADR-0135 point 3), their notes in the
    plan, and how many of those notes have not met every objective."""
    from silica.kernel.learn import mastery, placement

    steps = placement.placed_steps(plan, entries, step_paths) if plan.get("steps") else []
    if not steps:
        return None
    inside = set(paths)
    notes = sorted({p for s in steps for p in step_paths[s]} & inside)
    held = mastery.objectives_met(notes, objectives, entries, since=since)
    still = [p for p in notes if not all(held[p][o]["met"] for o in objectives)]
    return {"steps": steps, "notes": len(notes), "open": len(still)}


def _placed_said(plan: dict, placed: dict | None, paced: bool) -> dict | None:
    if not placed:
        return None
    labels = "; ".join(step_label(plan["steps"][s]) for s in placed["steps"][:3])
    more = f" and {len(placed['steps']) - 3} more" if len(placed["steps"]) > 3 else ""
    n = len(placed["steps"])
    say = (f"{n} step{'s' if n != 1 else ''} placed by what you already know ({labels}{more}, "
           f"{placed['notes']} note{'s' if placed['notes'] != 1 else ''}): asked after the other steps"
           + (" and left out of these figures." if paced else "."))
    return {**placed, "say": say}


def _placement_offer(plan: dict, entries: list[dict], view: dict | None,
                     step_paths: list[list[str]]) -> dict | None:
    """What the plan page's offer says: how many questions a placement asks
    now, and when this plan was last placed."""
    from silica.kernel.learn import placement

    if not plan.get("steps"):
        return None
    n = len(placement.candidates(plan, view=view, steps=step_paths))
    last = max((str(e.get("ts") or "") for e in entries
                if e.get("origin") == placement.ORIGIN and e.get("plan") == plan.get("path")), default="")
    return {"questions": n, "last": last[:10] or None} if n else None


def plan_report(plan: dict, entries: list[dict] | None = None, view: dict | None = None) -> dict:
    """The tree with per-node readings, the steps with pass state, the
    sessions, the frontier, the pace and the calibration of D26: everything
    the Plans view shows, derived at read.

    ponytail: a plan's tree is rebuilt from the scope on every read; cache
    it in the note when a plan passes 500 leaves.
    """
    from silica.kernel.learn import items, mastery
    from silica.kernel.learn import learner, quiz

    entries = entries if entries is not None else quiz.entries()
    view = view if view is not None else learner.view(_entries_override=entries)
    from silica.kernel.learn import objectives as obj

    notes = note_index.meta(store.vault())  # read once: every title lookup below uses it
    scoped = scope_of(plan, notes)
    paths = scoped["paths"]
    since = plan.get("created")
    objectives = plan["objectives"]
    tree_src = _tree_of(plan, paths, notes)
    tree = []
    for node in tree_src:
        leaves = node.get("leaves", [])
        tree.append({**node, "readings": mastery.node_readings(leaves, view, entries, since, objectives)})
    passed = mastery.steps_passed(plan, entries, notes=notes) if plan["steps"] else []
    steps = [{**s, "passed": ok, "gap": is_gap(s, notes)} for s, ok in zip(plan["steps"], passed)]
    relations = _relations_in(paths, items)
    root = mastery.node_readings(paths, view, entries, since, objectives)
    below = len(paths) - root["all_met"]
    from silica.kernel.learn import placement

    step_paths = placement.step_paths(plan, notes) if plan.get("steps") else []
    placed = _placed_of(plan, paths, objectives, entries, since, step_paths)
    cal = mastery.calibration(entries, paths)
    frontier = scoped["frontier"]
    nxt = None
    first = (scoped["revisit"] or frontier or [None])[0]   # queued notes are asked first
    if first:
        o = next_objective(plan, first, entries)
        nxt = {"path": first, "title": Path(first).stem, "objective": o, "label": obj.LABEL[o]}
    return {
        "path": plan["path"], "name": plan["name"], "scope": plan["scope"], "target": plan["target"],
        "reach": plan["reach"], "reach_level": plan["reach_level"], "reach_label": REACH_LABEL[plan["reach_level"]],
        "objectives": objectives, "objectives_chosen": plan["objectives_chosen"],
        "objective_labels": [obj.LABEL[o] for o in objectives],
        "cards": obj.attach_paths(plan["cards"], tree_src), "next": nxt,
        "notes": notes_of(plan["target"]) if plan["scope"] == "notes" else [], "refused": plan.get("refused", []),
        "by": plan.get("by"), "restrict": plan.get("restrict", ""), "created": since, "status": plan["status"],
        "completed": plan.get("completed"),
        "tree": tree, "steps": steps, "sessions": plan["sessions"], "frontier": frontier,
        "revisit": scoped["revisit"], "readings": root, "below": below,
        "integration": mastery.integration(paths, entries, relations),
        "misconceptions": mastery.misconceptions(paths, entries),
        "suggestions": mastery.suggestions(entries, paths),
        "calibration": cal, "grader": mastery.grader_agreement(entries, paths),
        # a placed step's open notes are left out of the workload (ADR-0135 point 3)
        "pace": (pace := pace_of(plan, below - (placed["open"] if placed else 0), entries=entries)),
        "placed": _placed_said(plan, placed, pace is not None),
        "placement": _placement_offer(plan, entries, view, step_paths),
        "cycles": cycles_among(paths),
        "budget": budget(plan, passed=passed), "assessment": plan.get("assessment") or [], "hours": plan.get("hours"),
        "today": today_line(plan, passed),
        "assessment_set": plan.get("assessment_set", False), "guide": plan.get("guide", ""),
        "materials": plan.get("materials", ""), "references": plan.get("references", ""),
        "matched": plan.get("matched", {}),
        "unused": plan.get("unused", ""),
        "complete": completion(plan, entries, passed, steps),
        "percent": percent_done(root["all_met"], root["coverage"][1], sum(s["gap"] for s in steps)),
        "paths": paths, "gaps": [s["text"] for s in steps if s["gap"]],
    }


def _tree_of(plan: dict, paths: list[str], notes: dict | None = None) -> list[dict]:
    if plan["scope"] == "steps":
        return [{"node": STEP_LINK_RE.sub(r"\1", s["text"]).split(":", 1)[0].strip(), "children": [],
                 "leaves": [p for p in (resolve_title(n, notes) for n in s["notes"]) if p], "gap": not s["notes"]}
                for s in plan["steps"]] or [{"node": plan["name"], "children": [], "leaves": paths}]
    try:
        target = plan["target"]
        if plan["scope"] == "notes":
            target = notes_of(target) or plan["tree_links"]
        return resolve_scope(plan["scope"], target, restrict=plan.get("restrict", ""))["tree"]
    except Exception:
        return [{"node": plan["name"], "children": [], "leaves": paths}]


def _relations_in(paths: list[str], items_mod) -> list[tuple[str, str]]:
    """The typed relations whose both endpoints sit in the scope, plus the V2 edges."""
    from silica.kernel.learn import learner

    by_title = {Path(p).stem.casefold(): p for p in paths}
    out: list[tuple[str, str]] = []
    for p in paths:
        try:
            for r in items_mod.relations_of(p):
                other = r["object"] if r["subject"] == items_mod.title_of(p) else r["subject"]
                op = by_title.get(other.casefold())
                if op:
                    out.append((p, op))
        except Exception:
            continue
    try:
        prereqs = learner.prerequisites_map()
        keys = {ledger_key(p): p for p in paths}
        for k, p in keys.items():
            for pre in prereqs.get(k, []):
                pk = ledger_key(pre)
                if pk in keys:
                    out.append((p, keys[pk]))
    except Exception:
        pass
    seen: set[tuple[str, str]] = set()
    uniq = []
    for a, b in out:
        key = (min(a, b), max(a, b))
        if a != b and key not in seen:
            seen.add(key)
            uniq.append((a, b))
    return uniq


def completion(plan: dict, entries: list[dict], passed: list[bool] | None = None, steps: list[dict] | None = None) -> bool:
    """Every distinct leaf met every objective without help after plan
    creation; a steps plan: every step passed and no unresolved gap (D24).
    A plan whose objectives are still to define never completes."""
    from silica.kernel.learn import mastery

    if plan["steps"]:
        passed = passed if passed is not None else mastery.steps_passed(plan, entries)
        gaps = any(s["gap"] for s in steps) if steps is not None else any(not s["notes"] for s in plan["steps"])
        return bool(passed) and all(passed) and not gaps
    paths = scope_of(plan)["paths"]
    if not paths:
        return False
    met = mastery.objectives_met(paths, plan["objectives"], entries, since=plan.get("created"))
    return all(met[p][o]["met"] for p in paths for o in plan["objectives"])


def is_gap(step: dict, notes: dict | None = None) -> bool:
    """A step with no note, or none of whose notes is in the vault any more:
    no answer can pass it. Named once because the plan page and the plan's
    row each count these, and a row that skipped the renamed-away notes read
    a different percent from the page it opens (ADR-0141)."""
    return not step["notes"] or all(resolve_title(n, notes) is None for n in step["notes"])


def percent_done(met: int, notes: int, gaps: int) -> int:
    """The completion bar's figure (ADR-0141): the notes that met every
    objective without help, over the plan's notes plus one unit for each
    gap step, which no answer can meet. Floored, so 224 of 225 reads 99:
    100 shows only when every unit is met, which on a plan Silica wrote is
    when `completion` holds, since its Tree lists the steps' own notes and
    the scope is their union. A Tree link added by hand outside every step
    counts here and not in `completion`, which reads the steps."""
    units = notes + gaps
    return met * 100 // units if units else 0


# ---------------------------------------------------------------- the plan note, written

def append_session(plan_path: str, line: str) -> str:
    """The session's summary line under `## Sessions`, once: a retry with
    the same session id appends nothing."""
    from silica.kernel.learn import session

    sid = session.line_id(line)

    def mutate(text: str) -> str:
        if sid and session.has_line(text, sid):
            return text
        lines = text.rstrip("\n").split("\n")
        idx = next((i for i, ln in enumerate(lines) if (h := HEADING_LINE_RE.match(ln)) and h.group(2).casefold().startswith("sessions")), None)
        if idx is None:
            return "\n".join(lines) + "\n\n## Sessions\n- " + line + "\n"
        end = len(lines)
        for j in range(idx + 1, len(lines)):
            if HEADING_LINE_RE.match(lines[j]):
                end = j
                break
        block = lines[:end]
        while block and not block[-1].strip():
            block.pop()
        return "\n".join(block + ["- " + line] + ([""] if end < len(lines) else []) + lines[end:]) + "\n"

    return journaled_write(plan_path, mutate, source="learn")


def _set_keys(text: str, updates: dict) -> str:
    from silica.kernel.base import frontmatter

    data, raw, body = frontmatter.split(text)
    if data is None:
        if raw is not None:
            return text  # broken YAML: never round-trip what cannot be parsed
        data, body = {}, text
    data = dict(data)
    for k, v in updates.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    return frontmatter.dump(data, body)


def set_revisit(plan_path: str, paths: list[str]) -> str:
    """`revisit:` in the plan's frontmatter, consumed by the next scoped
    session; the only way a calibration suggestion persists (D26)."""
    current = read_plan(plan_path)["revisit"]
    merged = list(dict.fromkeys(current + [p for p in paths if p]))
    return journaled_write(plan_path, lambda t: _set_keys(t, {"revisit": merged or None}),
                           source="learn", free=("revisit",))


def clear_revisit(plan_path: str, consumed: list[str]) -> str:
    current = read_plan(plan_path)["revisit"]
    left = [p for p in current if p not in set(consumed)]
    return journaled_write(plan_path, lambda t: _set_keys(t, {"revisit": left or None}),
                           source="learn", free=("revisit",))


def close_plan(plan_path: str, reopen: bool = False) -> dict:
    """`status: completed` and `completed:` only on evidence; `reopen`
    clears both. Closing the view completes nothing."""
    from datetime import date

    plan = read_plan(plan_path)
    if reopen:
        journaled_write(plan_path, lambda t: _set_keys(t, {"status": None, "completed": None}),
                        source="learn", free=("status", "completed"))
        return {"status": "active", "completed": None}
    from silica.kernel.learn import quiz

    entries = quiz.entries()
    if not completion(plan, entries):
        return {"status": plan["status"], "completed": plan.get("completed"),
                "say": "Not every leaf has demonstrated the target yet; the plan stays open."}
    today = date.today().isoformat()
    journaled_write(plan_path, lambda t: _set_keys(t, {"status": "completed", "completed": today}),
                    source="learn", free=("status", "completed"))
    return {"status": "completed", "completed": today}


def write_plan(spec: PlanSpec) -> dict:
    """The note compose_plan composes, through the write gate
    (silica_write_note), which refuses a path that exists."""
    from silica.tools import TOOLS

    d = compose_plan(spec)
    out = TOOLS["silica_write_note"].fn(path=d["path"], body=d["body"], title=spec.name, props=d["props"])
    if isinstance(out, dict) and out.get("error"):
        raise RuntimeError(out["error"])
    resolved = d["resolved"]
    return {"path": (out.get("path") if isinstance(out, dict) else None) or d["path"],
            "gaps": resolved["gaps"], "tree": resolved["tree"], "paths": resolved["paths"], "cards": d["cards"]}


def compose_plan(spec: PlanSpec) -> dict:
    """{path, body, props, resolved, cards}: the plan note, not written. The
    chat's unsaved plan is this note read back (learn.offer.plan_draft), so
    its steps and calendar are the ones Memorize shows once it is saved.
    `templates` words the objective cards without the model, as the form's
    Save does.

    Scope, objectives (the chosen intentions in frontmatter, the cards under
    `## Objectives`), tree, steps with a because clause and gaps, an empty
    Sessions section. Nothing about mastery goes in; every reading derives
    from the ledger. `reach` is accepted for the REPL's legacy call and
    mapped to intentions when `objectives` is None."""
    from datetime import date

    from silica.kernel.learn import objectives as obj

    if spec.scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    reach = spec.reach if spec.reach in REACH else "relational"
    resolved = resolve(spec)
    assessment = _with_parts(resolved, spec)
    if spec.scope == "steps":
        _depths(resolved["tree"], guide=spec.guide or "", restrict=spec.restrict)
    chosen = obj.normalize(spec.objectives) if spec.objectives is not None else list(obj.REACH_TO_INTENTIONS[reach])
    cards = spec.cards
    if cards is None:
        cards = obj.propose(resolved["tree"], chosen, model=not spec.templates)
    cards = [c for c in cards if isinstance(c, dict) and c.get("source") in obj.SOURCES]
    lines = _plan_body(spec.name, spec.scope, spec.target, resolved, chosen, cards) + ["", "## Sessions", ""]
    # the gate stringifies a list prop (`['explain']`), so the intentions go
    # as a comma string; `proposed by Silica` keeps the key present with
    # nothing chosen, which read_plan reads as "the cards say"
    props = {"type": "plan", "scope": spec.scope, "objectives": ", ".join(chosen) or "proposed by Silica",
             "created": date.today().isoformat(), "target": target_text(spec.scope, spec.target)}
    made = _Made(spec, resolved, assessment)
    props.update({k.name: v for k in PLAN_KEYS if (v := k.write(made))})
    return {"path": f"{spec.folder}/{spec.name}.md", "body": "\n".join(lines), "props": props, "resolved": resolved,
            "cards": cards}


def _with_parts(resolved: dict, spec: PlanSpec) -> str | None:
    """The assessment's parts on `resolved`, for the note's section, and the
    `assessment:` value. Only a steps plan has them."""
    parts, value = _parts_of(resolved, spec.assessment) if spec.scope == "steps" else ([], None)
    resolved["parts"] = parts
    return value


def _refused_lines(resolved: dict) -> list[str]:
    return [n.get("text", n["node"]) for n in resolved["tree"] if n.get("how") == "refused"]


def _plan_body(name: str, scope: str, target, resolved: dict, chosen: list[str], cards: list[dict]) -> list[str]:
    """The plan note's title, goal, Objectives, Tree and Steps; Sessions is
    the caller's, because an edit keeps the lines already there."""
    from silica.kernel.learn import objectives as obj

    lines = [f"# {name}", "", _goal_sentence(scope, target, chosen or [c["intention"] for c in cards if c.get("intention")])]
    said = " ".join(x for x in (_sources_sentence(resolved.get("sources") or []), resolved.get("order_note") or "") if x)
    if said:
        # one paragraph with the goal, so Edit plan rewrites it (_reader_head)
        lines.append(said)
    lines += ["", "## Objectives"]
    lines += [obj.format_line(c) for c in cards] or ["- to define · the whole scope · gap: no objective yet"]
    lines += ["", "## Tree"]
    for node in resolved["tree"]:
        label = node["node"]
        if node.get("gap"):
            lines.append(f"- {label}: not in the vault")
            continue
        # A step that is its one note shows as the link; any other label stays
        # text over its notes. Linking the label made "Utilizzo delle librerie
        # numpy, ..." a link to a note that does not exist (2026-10-23).
        found = node.get("leaves") or []
        one = scope == "steps" and len(found) == 1 and Path(found[0]).stem.casefold() == label.casefold()
        lines.append(f"- [[{Path(found[0]).stem}]]" if one else f"- {label}")
        leaves = [] if one else [f"[[{Path(p).stem}]]" for p in found]
        if leaves:
            lines.append("  - " + " · ".join(leaves))
    if scope == "steps":
        lines += ["", "## Steps"]
        for node in resolved["tree"]:
            missing = ", ".join(node.get("missing") or [])
            if node.get("gap"):
                lines.append(f"- [ ] {node['node']}: " + (node["how"] if node.get("how") == "no practice note found"
                                                         else "not in the vault") + (f" ({missing})" if missing else "")
                             + step_marks(node))
            elif ("missing" in node or str(node.get("how", "")).endswith(("topics", "rows"))
                  or "practice" in node or node.get("how") == "by position"):
                # a syllabus block keeps its name; its topics are the notes,
                # and what no note covers is said where the step is read; a
                # note met by meaning is marked (≈), one only a reference
                # gives (ref), the step's weight and depth close the line
                links = ", ".join(f"[[{Path(p).stem}]]" + (" (≈)" if p in node.get("approx", ()) else "")
                                  + (" (ref)" if p in node.get("ref", ()) else "") for p in node["leaves"])
                lines.append(f"- [ ] {node['node']}: {links}" + (f" · not in the vault: {missing}" if missing else "")
                             + step_marks(node))
            else:
                text = node.get("text", node["node"])
                links = " ".join(f"[[{Path(p).stem}]]" for p in node["leaves"]) if "[[" not in text else STEP_LINK_RE.sub(lambda m: f"[[{m.group(1)}]]", text.split(":", 1)[0])
                because = text.split(":", 1)[1].strip() if ":" in text else ""
                lines.append((f"- [ ] {links}: {because}" if because else f"- [ ] {links}") + step_marks(node))
        parts = resolved.get("parts") or []
        if parts:
            lines += ["", "## Assessment"] + [f"- [ ] {_PART_LINE[p['part']](p.get('days'))}" for p in parts]
    return lines


# The assessment's parts as the plan note says them (ADR-0131 point 9).
_PART_LINE = {
    "project": lambda d: (f"Project: the reference gives it at least {d} days before the date, and the calendar keeps "
                          f"those days for it" if d else "Project"),
    "oral": lambda d: "Oral exam: rehearse aloud the day before the date",
    "written": lambda d: "Written exam",
    "presentation": lambda d: "Presentation: rehearse it aloud the day before the date",
    "exercises": lambda d: "Exercises",
}


def _sources_sentence(sources: list[dict]) -> str:
    """"Sources: X, the material (its rule); Y, a reference (its rule).":
    each role the plan studies by, and which rule of ADR-0131 decided it."""
    from silica.kernel.learn import sources as so

    bits = [f"{s['label']}, {'the material' if s['role'] == so.MATERIAL else 'a reference'} ({s.get('why', '')})"
            for s in sources if s.get("role") in (so.MATERIAL, so.REFERENCE)]
    return ("Sources: " + "; ".join(bits) + ".") if bits else ""


def _matched_kept(resolved: dict) -> dict[str, str]:
    """The reader's answers that still apply: a topic the sources now meet
    by themselves needs none."""
    return {t: n for node in resolved.get("tree") or [] for t, n in (node.get("matched") or {}).items()}


def _role_keys(sources: list[dict]) -> dict[str, str]:
    """The plan keys that store the roles: labels joined by " | " (the write
    gate stringifies a list prop)."""
    from silica.kernel.learn import sources as so

    out = {}
    # a role to confirm is used for the steps but written only once the form
    # saves it: a plan made without the form keeps no guess as a choice
    for key, role in (("materials", so.MATERIAL), ("references", so.REFERENCE), ("unused", so.UNUSED)):
        labels = [s["label"] for s in sources if s.get("role") == role and not s.get("confirm")]
        if labels:
            out[key] = RESTRICT_SEP.join(labels)
    return out


def _parts_of(resolved: dict, assessment) -> tuple[list[dict], str | None]:
    """(the assessment's parts, the `assessment:` value): None reads them off
    the reference, "none" clears them, a list or a "project 7 | oral" string
    is the reader's."""
    if assessment is None:
        parts = resolved.get("assessment") or []
        return parts, (assessment_text(parts) if parts else None)
    if isinstance(assessment, str):
        parts = parse_assessment(assessment)
        return parts, assessment_text(parts)
    parts = [{"part": str(p.get("part")), "days": p.get("days")} for p in assessment
             if isinstance(p, dict) and str(p.get("part")) in _PART_LINE]
    return parts, assessment_text(parts)


_GUIDE_HEAD_RE = re.compile(r"^\|.*\b(profondit\w*|depth|difficolt\w*|difficulty|diff\.)", re.I | re.M)
_GUIDES_MEMO: dict[tuple, list[str]] = {}


def guide_candidates(restrict: str = "") -> list[str]:
    """The notes in the bound a plan could take its steps' depth from: a
    table whose header names a depth or a difficulty (ADR-0131 point 11),
    as a study guide writes one. Read once per notes epoch and bound."""
    from silica.kernel.base.noteshape import is_material

    vault = store.vault()
    notes = note_index.meta(vault)
    bounds = tuple(b.strip().casefold() for b in str(restrict or "").split(RESTRICT_SEP) if b.strip())
    paths = sorted(p for p, m in notes.items() if is_material(m) and (not bounds or p.casefold().startswith(bounds)))
    key = (str(vault), bounds, len(paths), max((notes[p].get("mtime", 0) for p in paths), default=0))
    if key not in _GUIDES_MEMO:
        found = []
        for p in paths:
            parsed = note_index.parsed(vault, p)
            body = parsed[1] if parsed else ""
            if "|" in body and _GUIDE_HEAD_RE.search(body) and any(r.get("depth") for r in reference_rows(body)):
                found.append(p)
        if len(_GUIDES_MEMO) > 8:
            _GUIDES_MEMO.clear()
        _GUIDES_MEMO[key] = found
    return _GUIDES_MEMO[key]


def _depths(tree: list[dict], guide: str = "", restrict: str = "") -> None:
    """A depth per step, written on its line (ADR-0131 point 10): the guide
    row sharing most of its notes, else the step's own verbs, else how much
    later steps rest on it (prerequisite edges from their notes into its
    notes), the top third deep and the bottom third read. Each step's lead,
    the note later steps rest on most, opens its objective."""
    from silica.kernel.learn import learner

    nodes = [n for n in tree if not n.get("gap") and n.get("leaves") and "practice" not in n]
    if guide:
        try:
            g = resolve_scope("steps", guide, restrict=restrict)["tree"]
        except Exception as exc:  # noqa: BLE001
            # a guide that does not resolve leaves the depth to the other rules
            logger.info("plan depth: guide %r unreadable (%s)", guide, exc)
            g = []
        for n in nodes:
            best = max(g, key=lambda gn: len(set(gn.get("leaves") or []) & set(n["leaves"])), default=None)
            if best and best.get("depth") and set(best.get("leaves") or []) & set(n["leaves"]):
                n["depth"] = best["depth"]
    for n in nodes:
        verb = next((d for d, rx in _VERB_DEPTH if rx.search(n.get("text", ""))), None)
        if verb and not n.get("depth"):
            n["depth"] = verb
    try:
        prereqs = {ledger_key(k): [ledger_key(x) for x in v] for k, v in learner.prerequisites_map().items()}
    except Exception as exc:  # noqa: BLE001
        # no graph or no index: depth stays what the guide and the verbs said
        logger.info("plan depth: no prerequisite map (%s)", exc)
        prereqs = {}
    step_of: dict[str, int] = {}
    for idx, n in enumerate(nodes):
        for p in n["leaves"]:
            step_of.setdefault(ledger_key(p), idx)
    rests_on: dict[str, int] = {}
    for dep, pres in prereqs.items():
        d = step_of.get(dep)
        if d is None:
            continue
        for pre in pres:
            s_ = step_of.get(pre)
            if s_ is not None and s_ < d:
                rests_on[pre] = rests_on.get(pre, 0) + 1
    score = [sum(rests_on.get(ledger_key(p), 0) for p in n["leaves"]) for n in nodes]
    for n in nodes:
        lead = max(n["leaves"], key=lambda p: rests_on.get(ledger_key(p), 0))
        if rests_on.get(ledger_key(lead), 0):
            n["lead"] = lead
    open_ = [i for i, n in enumerate(nodes) if not n.get("depth")]
    # Too few edges say nothing: on the course two of thirteen steps had a
    # prerequisite edge from a later step (2026-10-25), and ranking the rest
    # by zero made them all "read". No depth beats a wrong one.
    if len(nodes) < 3 or sum(1 for x in score if x) * 2 < len(nodes):
        return
    ranked = sorted(open_, key=lambda i: -score[i])
    third = max(1, len(nodes) // 3)
    cut_hi = sorted(score, reverse=True)[third - 1]
    cut_lo = sorted(score)[third - 1]
    for i in ranked:
        nodes[i]["depth"] = "deep" if score[i] >= cut_hi and score[i] > 0 else "read" if score[i] <= cut_lo else "well"


_VERB_DEPTH = (("deep", re.compile(r"\b(dimostr\w*|deriv\w*|prove|proofs?|derive\w*)\b", re.I)),
               ("read", re.compile(r"\b(cenni|overview|panoramica)\b", re.I)))


def edit_plan(plan_path: str, spec: PlanSpec) -> dict:
    """Edit plan (ADR-0081 §17): the same fields as creation, one revertible
    overwrite of the note. Only what creation generates is rewritten (the
    title, the goal line, Objectives, Tree, Steps); the reader's own text,
    their sections, Sessions and every other key (created, status, revisit)
    stay, so the ledger's reading of the plan does not move. The file keeps
    its path when the name changes, because a rename is a move and the
    sessions' receipts link the path. `refused` None keeps the plan's own
    refusals (Reword sends none); a list replaces them. The same holds for
    ADR-0131's `declared`, `hours`, `assessment` and `guide`: None keeps
    what the plan says."""
    from silica.kernel.learn import objectives as obj
    from silica.kernel.base import frontmatter

    if spec.scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    if not (store.vault() / plan_path).is_file():
        raise ValueError(f"no plan at {plan_path!r}")
    old = read_plan(plan_path)
    from silica.kernel.learn import sources as so

    kept = spec.declared is None
    spec = replace(
        spec, refused=old["refused"] if spec.refused is None else spec.refused,
        declared=so.declared_of(old) if kept else spec.declared,
        declared_why="as the plan says" if kept else spec.declared_why,
        matched=old.get("matched") if spec.matched is None else spec.matched,
        assessment=assessment_text(old["assessment"]) if spec.assessment is None and old.get("assessment_set") else spec.assessment,
        hours=old.get("hours") if spec.hours is None else spec.hours,
        guide=old.get("guide") if spec.guide is None else spec.guide)
    resolved = resolve(spec)
    assessment = _with_parts(resolved, spec)
    if spec.scope == "steps":
        _depths(resolved["tree"], guide=STEP_LINK_RE.sub(r"\1", spec.guide or ""), restrict=spec.restrict)
    chosen = obj.normalize(spec.objectives)
    cards = spec.cards
    if cards is None:
        cards = obj.propose(resolved["tree"], chosen)
    cards = [c for c in cards if isinstance(c, dict) and c.get("source") in obj.SOURCES]
    generated = _split_sections(_plan_body(spec.name, spec.scope, spec.target, resolved, chosen, cards))
    made = _Made(spec, resolved, assessment)

    def mutate(text: str) -> str:
        data, raw, body = frontmatter.split(text)
        if data is None and raw is not None:
            return text  # broken YAML: never round-trip what cannot be parsed
        mine = _split_sections(body.splitlines())
        out = generated[0][1] + _trim(_reader_head(mine[0][1]))
        done = set()
        for title, lines in mine[1:]:
            key = _GENERATED.get(title.casefold())
            if key is None:
                out += [f"## {title}", *lines]
                continue
            new = next((g for g in generated[1:] if g[0] == key), None)
            if new:
                out += [f"## {key}", *new[1], ""]
                done.add(key)
        # a section creation now writes that the note lacked (Steps after a
        # scope change) goes before Sessions, where creation puts it
        missing = [g for g in generated[1:] if g[0] not in done]
        at = next((i for i, ln in enumerate(out) if ln.strip().casefold() == "## sessions"), len(out))
        for title, lines in reversed(missing):
            out[at:at] = [f"## {title}", *lines, ""]
        keys = dict(data or {})
        keys.update(scope=spec.scope, target=target_text(spec.scope, spec.target), objectives=", ".join(chosen) or "proposed by Silica")
        # `matched` joined the table's middle on creation and an edit has always
        # added it last: a key's place in a note's frontmatter stays where it was
        for k in sorted(PLAN_KEYS, key=lambda k: k.name == "matched"):
            v = k.write(made)
            if v:
                keys[k.name] = str(v)
            else:
                keys.pop(k.name, None)
        return frontmatter.dump(keys, re.sub(r"\n{3,}", "\n\n", "\n".join(out)).rstrip("\n") + "\n")

    from silica.kernel.write.preserve import Loss

    # PLAN relaxes the preamble, the generated sections and the plan keys;
    # the reader's own sections must survive the re-scope.
    run = journaled_write(plan_path, mutate, source="learn", loss=Loss.PLAN)
    return {"path": plan_path, "run": run, "gaps": resolved["gaps"], "tree": resolved["tree"],
            "paths": resolved["paths"], "cards": cards}


# the sections creation writes, by their heading; anything else is the reader's
_GENERATED = {"objectives": "Objectives", "tree": "Tree", "steps": "Steps", "assessment": "Assessment"}


def _split_sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """[("", preamble), (heading, lines), ...] on level-2 headings."""
    out: list[tuple[str, list[str]]] = [("", [])]
    for ln in lines:
        m = HEADING_LINE_RE.match(ln)
        if m and len(m.group(1)) == 2:
            out.append((m.group(2), []))
        else:
            out[-1][1].append(ln)
    return out


def _reader_head(lines: list[str]) -> list[str]:
    """The preamble without what creation wrote there: the title and the
    first paragraph when it is the goal ("Learn ..."), which before this
    review could run over several lines for a pasted syllabus."""
    lines = [ln for ln in lines if not ((h := HEADING_LINE_RE.match(ln)) and len(h.group(1)) == 1)]
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].startswith("Learn "):
        while i < len(lines) and lines[i].strip():
            i += 1
    return lines[i:]


def _trim(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines = lines[1:]
    return lines + [""] if lines else []


def _goal_sentence(scope: str, target, objectives: list[str]) -> str:
    from silica.kernel.learn import objectives as obj

    what = {"notes": "these notes", "folder": f"everything under {', '.join(str(target).split(RESTRICT_SEP))}", "topic": f"the notes on {target}",
            "workspace": f"the notes of the workspace {target}",
            "steps": f"the steps of {target if isinstance(target, str) and len(target) < 60 and chr(10) not in target else 'the program'}"}[scope]
    names = [obj.LABEL[o].lower() for o in obj.normalize(objectives)] or ["objectives to define"]
    return f"Learn {what}: " + (", ".join(names[:-1]) + (", and " if len(names) > 1 else "") + names[-1]) + "."


# ---------------------------------------------------------------- the sentence, read (ADR-0081 §10, §11)
# Code reads what code can check, the model only the rest (ADR-0065's rule):
# the date and the intentions' verbs here, the material by one model call
# whose every field is checked against the vault before it reaches a field.


