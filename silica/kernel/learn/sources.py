# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The sources a plan studies (ADR-0131) and how a reference's topics meet the
vault: collections and their role, the title, word and meaning matches, and
the step scope that puts them together.

A nucleated source leaves a spine (`section: (outline)`, one row per idea
in the source's order) and a verbatim leaf under `sources/` that names the
file it came from. A collection is the spines of one file split into
segments (a book) or of one folder of files (a series of decks, episodes,
chapters). A role is relative to a plan's reference: the same book is the
material for a reader learning from it and a reference for a course that
recommends it, so the role is read here for one reference and stored on
the plan, never on the source.
"""
from __future__ import annotations

import functools
import logging
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from silica.kernel.base import note_index
from silica.kernel.base.layout import is_source_leaf
from silica.kernel.base.syntax import WIKILINK_NAME, WIKILINK_RE, WIKILINK_TARGET_RE
from silica.kernel.learn import store
from silica.kernel.learn.reference import (HEADING_LINE_RE, RESTRICT_SEP, STEP_LINK_RE, assessment_of, line_topics,
                                           note_title, reference_rows, step_rows)

logger = logging.getLogger(__name__)

MATERIAL, REFERENCE, UNUSED = "material", "reference", "unused"
ROLES = (MATERIAL, REFERENCE, UNUSED)
ROLE_LABEL = {MATERIAL: "Material", REFERENCE: "Reference", UNUSED: "Not used"}

# What decides a role when the reader did not, read once on the Machine
# Learning course (2026-10-25, lexical, read-only): the decks followed the
# syllabus's order at Spearman 0.82 and the book's segments at 0.39; the
# median share of rows the syllabus names was 0.57 on teaching decks and 0.32
# on the book's segments, overlapping per file (q1 0.43 against q3 0.53).
# One corpus: reopen at a second corpus with two collections or 2027-01-31
# (ADR-0131, Not decided). Until then every inferred role says its rule.
ORDER_MIN_SPINES = 4     # a correlation over fewer spines is noise
ORDER_FLOOR = 0.6
ORDER_MARGIN = 0.2
FOCUS_MARGIN = 0.15

_ROW_RE = re.compile(rf"^\s*\d+[.)]\s+{WIKILINK_NAME}:?\s*(.*)$")
_PRACTICE_RE = re.compile(r"^##\s+Concepts practised\s*$", re.M)
# A cell, line or heading that introduces a reference's books: what the
# reference names there is recommended reading, not what it was taught from.
_TEXTS_RE = re.compile(r"\b(testi|libri|bibliografia|letture|textbooks?|books|readings?|bibliography|"
                       r"recommended reading|reading list)\b", re.I)
_NUM_RE = re.compile(r"\d+|\D+")


def _leaf_links(text: str) -> list[str]:
    """The verbatim leaves `text` links. By `is_source_leaf` and not by spelling:
    a leaf link is composed with the write dir (finalize), so `[[silica/sources/`
    on a confined vault, and a bare `[[sources/` found none there."""
    return [t for t in WIKILINK_TARGET_RE.findall(text) if is_source_leaf(t)]


def _read(path: str) -> str:
    try:
        return (store.vault() / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def natural_key(text: str) -> list:
    """"Lezione 2" before "Lezione 10": numbers read as numbers."""
    return [int(t) if t.isdigit() else t.casefold() for t in _NUM_RE.findall(text)]


def plain(text: str) -> str:
    """A row or a line with its links read as their words."""
    return WIKILINK_RE.sub(lambda m: m.group(4) or m.group(2), text)


def roots(text: str) -> set[str]:
    """Content roots as topic matching reads them, an English plural folded:
    "kernel tricks" is held by a deck that says "kernel trick"."""
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w for w in root_words(text)}


def held(words: set[str], within: set[str]) -> bool:
    """All the words of a topic of one or two words, two thirds of a longer
    one: "le operazioni di convoluzione" is held without "operazioni"."""
    if not words:
        return False
    need = len(words) if len(words) <= 2 else -(-len(words) * 2 // 3)
    return len(words & within) >= need


# ---------------------------------------------------------------- collections

_MEMO: dict[tuple, list[dict]] = {}


def collections(notes: dict, allowed: set[str]) -> list[dict]:
    """The collections whose spines sit in `allowed`, each
    {key, label, kind, spines, rows, leaves}: `rows` in the source's order,
    each {spine, i, note, title, text, roots}; `leaves` {leaf path: spine}.

    Grouped by the leaf's `source_file` when two spines share it (a book
    split into segments), else by the leaf's folder (a series). The hub note
    is no key: the course's book hub was merged into another note
    (2026-10-25). Computed once per notes-meta epoch and bound."""
    spines = sorted((p for p in allowed if notes.get(p, {}).get("spine")), key=natural_key)
    memo_key = (str(store.vault()), tuple(spines), tuple(notes[p].get("mtime", 0) for p in spines))
    if memo_key in _MEMO:
        return _MEMO[memo_key]
    vault = store.vault()
    found: list[tuple[str, str, str]] = []   # (spine, leaf, source_file)
    for sp in spines:
        links = _leaf_links(_read(sp))
        if not links:
            continue   # an outline the reader wrote: no source behind it
        leaf = links[0].removesuffix(".md")
        parsed = note_index.parsed(vault, leaf + ".md", body=False)
        data = (parsed[0] if parsed else None) or {}
        found.append((sp, leaf, str(data.get("source_file") or "")))
    shared = Counter(sf for _sp, _l, sf in found if sf)
    groups: dict[str, dict] = {}
    for sp, leaf, sf in found:
        if sf and shared[sf] >= 2:
            key, kind, label = "file:" + sf, "book", _file_label(sf)
        else:
            folder = leaf.rsplit("/", 1)[0] if "/" in leaf else leaf
            key, kind, label = "dir:" + folder, "series", folder.rsplit("/", 1)[-1]
        g = groups.setdefault(key, {"key": key, "label": label, "kind": kind, "spines": [], "rows": [], "leaves": {}})
        g["spines"].append(sp)
        g["leaves"][leaf] = sp
        g.setdefault("by_spine", {})[sp] = []
    for g in groups.values():
        g["spines"].sort(key=lambda p: natural_key(Path(p).stem))
        for sp in g["spines"]:
            i = 0
            for line in _read(sp).splitlines():
                m = _ROW_RE.match(line)
                if not m:
                    continue
                title, rest = m.group(1).strip(), plain(m.group(2))
                text = f"{title}: {rest}"
                row = {"spine": sp, "i": i, "note": resolve_title(title, notes), "title": title,
                       "text": text, "roots": roots(text), "key": g["key"]}
                g["rows"].append(row)
                g["by_spine"][sp].append(row)
                i += 1
    out = sorted(groups.values(), key=lambda g: natural_key(g["label"]))
    # two series named alike ("Slides" in two courses) keep their parent folder
    names = Counter(g["label"].casefold() for g in out)
    for g in out:
        if names[g["label"].casefold()] > 1 and g["kind"] == "series":
            g["label"] = g["key"][4:].rsplit("/", 2)[-2] + "/" + g["label"]
    if len(_MEMO) > 8:
        _MEMO.clear()
    _MEMO[memo_key] = out
    return out


def _file_label(source_file: str) -> str:
    from silica.sources.convert import _note_stem

    return _note_stem(Path(source_file))


def note_collections(paths, colls: list[dict]) -> dict[str, set[str]]:
    """{note: the keys of the collections it came from}: the rows it is,
    and the leaves its `## Sources` links name."""
    leaf_key = {leaf: c["key"] for c in colls for leaf in c["leaves"]}
    out: dict[str, set[str]] = {}
    for c in colls:
        for r in c["rows"]:
            if r["note"]:
                out.setdefault(r["note"], set()).add(c["key"])
    for p in paths:
        for leaf in _leaf_links(_read(p)):
            k = leaf_key.get(leaf.removesuffix(".md"))
            if k:
                out.setdefault(p, set()).add(k)
    return out


def practice_notes(allowed: set[str]) -> list[str]:
    """Notes that put concepts to work (a lab notebook's note carries
    `## Concepts practised`, silica/kernel/link/labs.py): practice by
    content, whatever folder or name."""
    return sorted((p for p in allowed if _PRACTICE_RE.search(_read(p))), key=natural_key)


_LEAF_ROOTS: dict[tuple[str, float], set[str]] = {}


def leaf_roots(leaf: str) -> set[str]:
    """A verbatim leaf's roots, read once per mtime."""
    f = store.vault() / (leaf + ".md")
    try:
        key = (str(f), f.stat().st_mtime)
    except OSError:
        return set()
    if key not in _LEAF_ROOTS:
        _LEAF_ROOTS[key] = roots(_read(leaf + ".md"))
    return _LEAF_ROOTS[key]


# ---------------------------------------------------------------- roles

def texts_lines(body: str) -> list[str]:
    """The reference's lines that list books: a table row whose first cell is
    a texts word ("TESTI CONSIGLIATI"), a "Testi: ..." line, the lines under a
    texts heading."""
    out: list[str] = []
    under = False
    for line in body.splitlines():
        s = line.strip()
        h = HEADING_LINE_RE.match(s)
        if h:
            under = bool(_TEXTS_RE.search(h.group(2)))
            continue
        if under and s:
            out.append(s)
            continue
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) >= 2 and _TEXTS_RE.search(cells[0]) and len(cells[0].split()) <= 4:
                out.append(" ".join(cells[1:]))
        elif ":" in s and _TEXTS_RE.search(s.split(":", 1)[0]) and len(s.split(":", 1)[0].split()) <= 4:
            out.append(s.split(":", 1)[1])
    return out


def named_in(label: str, text: str) -> bool:
    """A collection is named when two of its label's longer words are in the
    text: "Deep Learning (Ian Goodfellow, ...)" in "Ian Goodfellow, Yoshua
    Bengio and Aaron Courville, Deep Learning", not "Pattern Recognition And
    Machine Learning", which shares one."""
    words = {w for w in roots(label) if len(w) >= 5}
    return len(words & roots(text)) >= 2


def spearman(xs: list[float], ys: list[float]) -> float:
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):   # ties share their mean rank
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    n = len(xs)
    if n < 2:
        return 0.0
    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else 0.0


def readings(colls: list[dict], steps: list[list[set[str]]]) -> dict[str, dict]:
    """{key: {matched, rows, focus, order, spines}} for the reference's steps,
    each a list of topics as root sets: the share of a collection's rows some
    topic holds, and how its spines' order follows the steps'."""
    out: dict[str, dict] = {}
    for c in colls:
        per_spine: dict[str, list[int]] = {}
        matched = 0
        # an outline without numbered rows is read by its source's text, one
        # unit a spine: its role is still the reference's to tell
        units = c["rows"] or [{"spine": sp, "roots": leaf_roots(leaf)} for leaf, sp in c.get("leaves", {}).items()]
        for r in units:
            hit = [i for i, topics in enumerate(steps) if any(held(t, r["roots"]) for t in topics)]
            if hit:
                matched += 1
                per_spine.setdefault(r["spine"], []).extend(hit)
        pos = {sp: n for n, sp in enumerate(c["spines"])}
        pairs = [(pos[sp], statistics.median(v)) for sp, v in per_spine.items()]
        order = spearman([a for a, _ in pairs], [b for _, b in pairs]) if len(pairs) >= ORDER_MIN_SPINES else None
        out[c["key"]] = {"matched": matched, "rows": len(units), "spines": len(pairs),
                         "focus": matched / len(units) if units else 0.0, "order": order}
    return out


def roles(colls: list[dict], body: str, title: str, steps: list[list[set[str]]],
          declared: dict[str, str] | None = None, declared_why: str = "you set it",
          read: dict[str, dict] | None = None) -> dict[str, dict]:
    """{key: {role, why}}, in ADR-0131's order: the reader's choice, the
    reference naming it, order, focus, else every matched collection is
    material and the form asks. `read` is `readings` when the caller has it."""
    declared = {k.casefold(): v for k, v in (declared or {}).items()}
    read = read if read is not None else readings(colls, steps)
    out: dict[str, dict] = {}
    texts = " ".join(texts_lines(body))
    for c in colls:
        role = declared.get(c["label"].casefold()) or declared.get(c["key"].casefold())
        if role in ROLES:
            out[c["key"]] = {"role": role, "why": declared_why, "confirm": False}
        elif texts and named_in(c["label"], texts):
            out[c["key"]] = {"role": REFERENCE, "why": "named among the reference's texts", "confirm": False}
        elif title and named_in(c["label"], title):
            out[c["key"]] = {"role": MATERIAL, "why": "named in the reference's title", "confirm": False}
    open_ = [c for c in colls if c["key"] not in out and read[c["key"]]["matched"]]
    for c in colls:
        if c["key"] not in out and not read[c["key"]]["matched"]:
            out[c["key"]] = {"role": UNUSED, "why": "no row of it meets the reference", "confirm": False}
    if not open_:
        return out
    has_material = any(v["role"] == MATERIAL for v in out.values())
    if len(open_) == 1 and not has_material and not any(v["role"] == REFERENCE for v in out.values()):
        out[open_[0]["key"]] = {"role": MATERIAL, "why": "the only source the reference meets", "confirm": False}
        return out
    confirm = False
    if not has_material:
        winner, why, confirm = _decide(open_, read)
        if winner:
            out[winner] = {"role": MATERIAL, "why": why + ("; to confirm" if confirm else ""), "confirm": confirm}
            has_material = True
    for c in open_:
        if c["key"] in out:
            continue
        if has_material:
            out[c["key"]] = {"role": REFERENCE, "why": "another collection is the material", "confirm": confirm}
        else:
            out[c["key"]] = {"role": MATERIAL, "why": "could not tell the material from a reference: choose in the form",
                             "confirm": True}
    return out


# A decision closer than twice its margin is a proposal, not a role: on 20
# random halves of the course syllabus (2026-10-25) the decks won by order 11
# times, the smallest winning margins 0.26 and 0.28, and 9 halves settled
# nothing; focus margins on the halves were 0.00 to 0.02. A role to confirm is
# shown and used but not written to the plan until the form saves it.
CONFIRM_FACTOR = 2.0


def _decide(open_: list[dict], read: dict[str, dict]) -> tuple[str | None, str, bool]:
    """The material among undecided collections by order, then by focus, or
    (None, "", True) when neither settles it; the flag says to confirm."""
    ordered = sorted(((read[c["key"]]["order"], c["key"]) for c in open_ if read[c["key"]]["order"] is not None),
                     reverse=True)
    if ordered and ordered[0][0] >= ORDER_FLOOR:
        second = ordered[1][0] if len(ordered) > 1 else -1.0
        if ordered[0][0] - second >= ORDER_MARGIN:
            other = f" against {second:.2f}" if len(ordered) > 1 else ""
            close = len(ordered) > 1 and ordered[0][0] - second < ORDER_MARGIN * CONFIRM_FACTOR
            return ordered[0][1], f"follows the reference's order ({ordered[0][0]:.2f}{other})", close
    focused = sorted(((read[c["key"]]["focus"], c["key"]) for c in open_), reverse=True)
    second = focused[1][0] if len(focused) > 1 else 0.0
    if focused[0][0] - second >= FOCUS_MARGIN:
        close = len(focused) > 1 and focused[0][0] - second < FOCUS_MARGIN * CONFIRM_FACTOR
        return focused[0][1], (f"the reference names {focused[0][0]:.0%} of its rows"
                               + (f", against {second:.0%}" if len(focused) > 1 else "")), close
    return None, "", True


def declared_of(plan_keys: dict) -> dict[str, str]:
    """{label: role} from a plan's `materials:` and `references:` keys,
    labels joined by " | " as `restrict` joins its bounds."""
    out: dict[str, str] = {}
    for key, role in (("materials", MATERIAL), ("references", REFERENCE), ("unused", UNUSED)):
        for label in str(plan_keys.get(key) or "").split(" | "):
            if label.strip():
                out[label.strip()] = role
    return out


# ---------------------------------------------------------------- rows a step takes

# Between two rows a step holds in one spine, the rows a lecture puts between
# them come too: its derivation runs in order. Lecture 13 of the course puts
# Novikoff's theorem and the optimisation problem between the online
# perceptron and the Lagrangian, and no syllabus word names either
# (2026-10-25). Two rows: a third would join unrelated halves of a lecture
# (lecture 13 opens on PCA and closes on SVMs).
FILL_GAP = 2


def section(rows: list[dict], colls: list[dict]) -> list[dict]:
    """The rows held, with up to FILL_GAP rows between two held rows of one
    spine, in the source's order: collection, spine, row."""
    by_key = {c["key"]: c for c in colls}
    held_ids = {(r["spine"], r["i"]) for r in rows}
    out: list[dict] = []
    for c in colls:
        for sp in c["spines"]:
            spine_rows = by_key[c["key"]]["by_spine"].get(sp, [])
            idx = sorted(r["i"] for r in spine_rows if (sp, r["i"]) in held_ids)
            if not idx:
                continue
            take = set(idx)
            for a, b in zip(idx, idx[1:]):
                if 1 < b - a <= FILL_GAP + 1:
                    take.update(range(a + 1, b))
            out += [r for r in spine_rows if r["i"] in take]
    return out


def half_held(words: set[str], within: set[str]) -> bool:
    """Half of a long line's distinctive words: an activity line describes,
    it does not name ("Applicazione dei modelli Kernel ... su dataset reali")."""
    got = len(words & within)
    return bool(words) and got >= 1 and got * 2 >= len(words)


def distinctive(line: str, others: list[str]) -> set[str]:
    """A line's words fewer than half of the other activity lines share: the
    three labs of a syllabus differ by "kernel", "lineari e ensemble" and
    "reti neurali superficiali e profonde", and share the rest."""
    mine = roots(line)
    if not others:
        return mine
    other_roots = [roots(o) for o in others]
    return {w for w in mine if sum(w in o for o in other_roots) * 2 < len(other_roots)}


def practice_for(words: set[str], practice: list[str]) -> list[str]:
    """The practice notes an activity line's distinctive words weigh most
    in, each word by how few practice notes hold it: "machine learning" is
    in every notebook of a course and says nothing, "kernel" is in two. A
    note joins when it weighs half of the best, and above nothing."""
    import math

    held_by = {p: practice_roots(p) & words for p in practice}
    df = Counter(w for ws in held_by.values() for w in ws)
    idf = {w: math.log((len(practice) + 1) / (df[w] + 1)) for w in df}
    score = {p: sum(idf[w] for w in ws) for p, ws in held_by.items()}
    best = max(score.values(), default=0.0)
    if best <= 0:
        return []
    return [p for p in practice if score[p] > 0 and score[p] * 2 >= best]


_PRACTICE_ROOTS: dict[tuple[str, float], set[str]] = {}


def practice_roots(path: str) -> set[str]:
    f = store.vault() / path
    try:
        key = (str(f), f.stat().st_mtime)
    except OSError:
        return set()
    if key not in _PRACTICE_ROOTS:
        _PRACTICE_ROOTS[key] = roots(Path(path).stem.replace("_", " ") + " " + _read(path))
    return _PRACTICE_ROOTS[key]


def leaf_spine(words: set[str], colls: list[dict]) -> tuple[str | None, str | None]:
    """(the spine of the first leaf whose verbatim text holds the topic, its
    collection key), material collections first as given."""
    for c in colls:
        for leaf, sp in sorted(c["leaves"].items(), key=lambda kv: natural_key(Path(kv[1]).stem)):
            if held(words, leaf_roots(leaf)):
                return sp, c["key"]
    return None, None


# ---------------------------------------------------------------- a glued line

# Function words a glued line hides ("Introduzionealcorso": introduzione, al,
# corso): Italian and English, short, never in a title on their own.
_SHORT = {"a", "e", "i", "o", "al", "di", "da", "in", "il", "la", "le", "lo", "gli", "del", "dei", "della", "delle",
          "dal", "con", "per", "su", "tra", "fra", "ed", "of", "and", "the", "to", "an", "on", "for", "with"}
_VOCAB: dict[tuple, set[str]] = {}


def vocabulary(notes: dict, allowed: set[str], colls: list[dict]) -> set[str]:
    """The words of the bound: note titles, outline rows and the leaves'
    verbatim text, where "richiami" lives when no title says it."""
    key = (str(store.vault()), len(allowed), tuple(c["key"] for c in colls))
    if key in _VOCAB:
        return _VOCAB[key]
    words: set[str] = set(_SHORT)
    for p in allowed:
        words.update(w.casefold() for w in re.findall(r"[^\W\d_]{2,}", Path(p).stem))
    for c in colls:
        for r in c["rows"]:
            words.update(w.casefold() for w in re.findall(r"[^\W\d_]{2,}", r["text"]))
        for leaf in c["leaves"]:
            words.update(w.casefold() for w in re.findall(r"[^\W\d_]{2,}", _read(leaf + ".md")))
    if len(_VOCAB) > 4:
        _VOCAB.clear()
    _VOCAB[key] = words
    return words


def split_glued(token: str, vocab: set[str]) -> str | None:
    """The fewest vocabulary words that spell the token, or None: a PDF's
    text layer dropped the spaces of a whole syllabus line
    ("richiamidialgebralineare", 2026-10-25)."""
    low = token.casefold()
    n = len(low)
    best: list[list[str] | None] = [None] * (n + 1)
    best[0] = []
    for i in range(1, n + 1):
        for j in range(max(0, i - 24), i):
            prev = best[j]
            piece = low[j:i]
            if prev is None or piece not in vocab or (len(piece) == 1 and piece not in _SHORT):
                continue
            cur = best[i]
            if cur is None or len(prev) + 1 < len(cur):
                best[i] = prev + [piece]
    parts = best[n]
    return " ".join(parts) if parts and len(parts) > 1 else None


# ---------------------------------------------------------------- steps aligned to rows

# How much a held topic counts toward a row's step (ADR-0131 point 3): a word
# alone ("ottimizzazione", "kernel", "percettrone") is held by rows all over a
# course, so it counts half, and so does the step's label; a title vector
# counts between. Read on the course (2026-10-25): without the order, the
# first syllabus block took the SVM's slack rows by "ottimizzazione" and the
# kernel block the convolution rows by "kernel".
W_PHRASE, W_WORD, W_LABEL, W_MEANING = 1.0, 0.5, 0.5, 0.75
SPINE_SUPPORT = 1.0      # a phrase, or two words, of a block in one spine


def align(steps: list[dict], coll: dict) -> dict[int, list[int]]:
    """{step: indexes of `coll`'s rows} by a monotone alignment: the rows,
    in the source's order, each go to one step or none, the steps' order
    never going back, the held weight summed at its largest. A reference
    whose material follows it (the course: 0.84) gives each block the rows of
    its own lectures, and a row a later block's word happens to hold stays
    with the block that teaches it. `steps` are {i, scores: {row: weight}}."""
    order = [s["i"] for s in steps]
    n = len(order)
    if not n:
        return {}
    neg = float("-inf")
    # f[k]: the best total over the rows so far with the last row given to a
    # step at or before k; non-decreasing in k, so f[n - 1] is the answer
    f = [0.0] * n
    back: list[list[tuple[bool, int]]] = []
    for r in range(len(coll["rows"])):
        best, arg = neg, -1
        new_f, choice = [0.0] * n, [(False, 0)] * n
        for k in range(n):
            w = steps[k]["scores"].get(r, 0.0)
            if w > 0 and f[k] + w > best:
                best, arg = f[k] + w, k
            if best > f[k]:
                new_f[k], choice[k] = best, (True, arg)
            else:
                new_f[k], choice[k] = f[k], (False, k)
        f = new_f
        back.append(choice)
    k = n - 1
    out: dict[int, list[int]] = {}
    for r in range(len(coll["rows"]) - 1, -1, -1):
        took, prev = back[r][k]
        if took:
            out.setdefault(order[prev], []).append(r)
        k = prev
    return {i: sorted(v) for i, v in out.items()}


def best_steps(steps: list[dict], coll: dict, names: dict[int, str]) -> dict[int, list[int]]:
    """{step: indexes of `coll`'s rows} with no order assumed: each row goes
    to the step that holds it with most weight, then to the step its spine
    supports most, then by the step's name, so the steps come out the same
    whatever order the reference lists them in.

    For a reference the material does not follow. Measured on a shuffled
    course syllabus (2026-10-25): aligned in the shuffled order, its kernel
    block took 89 notes and its mathematics 2, and nothing said so; this way
    the total stays within 5% of the ordered plan and 15 of the 17 gate rows
    under kernels and SVMs stay there. Three other ways did worse (12, 12
    and 13 of 17: whole spines, runs within a spine, the material's own
    order). The ordered alignment is no ground truth either: where words
    point elsewhere (Novikoff's row holds the feedforward block's
    "percettrone"), only the order had kept a row with its lecture, which is
    why the plan says it matched by words and asks to check."""
    rows = coll["rows"]
    support: dict[int, Counter] = {}
    for st in steps:
        sup = support.setdefault(st["i"], Counter())
        for n, w in st["scores"].items():
            sup[rows[n]["spine"]] += w
    out: dict[int, list[int]] = {}
    for n in range(len(rows)):
        cands = [(st["scores"][n], support[st["i"]][rows[n]["spine"]], names.get(st["i"], ""), st["i"])
                 for st in steps if st["scores"].get(n)]
        if cands:
            out.setdefault(max(cands)[3], []).append(n)
    return out


# ---------------------------------------------------------------- topics against the vault

def resolve_title(name: str, notes: dict | None = None) -> str | None:
    """A wikilink target to a vault path, by exact path, then by stem. A
    merge loser resolves to the note it points at: a dedup pass made
    "Retropropagazione" a loser of "Backpropagation", and a syllabus that
    names retropropagazione called it not in the vault (2026-10-23)."""

    notes = notes if notes is not None else note_index.meta(store.vault())
    hit = _by_name(name, notes)
    for _hop in range(3):   # a chain of merges, never a loop
        winner = hit and notes[hit].get("winner")
        nxt = _by_name(winner, notes) if winner else None
        if not nxt or nxt == hit:
            break
        hit = nxt
    return hit


# A file's own extension, the only suffix a name loses: Path.stem cut
# "scheda.trasparenza - Reti di Calcolatori e Internet" to "scheda" (2026-10-29).
FILE_EXT_RE = re.compile(r"\.(md|pdf|docx?|pptx?|xlsx?|odt|epub|html?|txt)$", re.I)


def _by_name(name: str, notes: dict) -> str | None:
    name = name.strip()
    cand = name if name.endswith(".md") else name + ".md"
    if cand in notes:
        return cand
    return _stem_index(notes).get(FILE_EXT_RE.sub("", name.rsplit("/", 1)[-1]).casefold())


# {casefolded stem: the first path holding it, in the notes' own order}, for
# the notes dict last asked about: the stem was looked up by walking every
# note, 4,860 walks of 2,082 notes in one course preview, 16 s of a 30 s
# profile (2026-10-25). The dict is held, not its id, so a new dict never
# reads an old index; a changed length rebuilds it.
_STEMS: tuple = (None, -1, {})


def _stem_index(notes: dict) -> dict[str, str]:
    global _STEMS
    held, size, index = _STEMS
    if held is not notes or size != len(notes):
        index = {}
        for p in notes:
            index.setdefault(Path(p).stem.casefold(), p)
        _STEMS = (notes, len(notes), index)
    return index


def steps_scope(target: str, notes: dict, allowed: set[str], refused: set | None = None,
                 declared: dict[str, str] | None = None, declared_why: str = "you set it",
                 matched: dict[str, str] | None = None) -> dict:
    """Each step line resolved to the notes that cover it: wikilinks first,
    then the title index, then one body search; a step with no note is kept
    as a gap (the plan's most useful line). Each line says how it matched
    (`how`) and whether that is doubtful, because "Teoria della mente"
    resolved to two probability notes on one shared word and nothing said so
    (ADR-0081, Create plan fault 4); a refused line is a gap by the reader.

    With nucleated sources in the bound, the sources come first (ADR-0131):
    a step is the outline rows of the material that hold its topics, the
    title matching behind them, a note that comes only from a reference kept
    for a topic no material row holds, and a topic not in the vault only
    when no source's text holds it either."""
    from silica.kernel.base import frontmatter

    # a line break says pasted text: "Iperpiano" alone names the note to read
    # the syllabus from, "Iperpiano\n" is one line to match (ADR-0119)
    path = None if "\n" in target else resolve_title(target, notes)
    body = title = ""
    if path:
        _d, _r, body = frontmatter.split((store.vault() / path).read_text(encoding="utf-8"))
        title = note_title(body) or Path(path).stem
        rows = step_rows(body) or reference_rows(body)
    else:
        rows = [{"text": ln.strip("-* ").strip()} for ln in re.split(r"[\n;]", target) if ln.strip()]
    colls = collections(notes, allowed)
    rows = _unglued(rows, notes, allowed, colls)
    lines = [r["text"] for r in rows]
    topics_of = [line_topics(t) for t in lines]
    labels = [STEP_LINK_RE.sub(r"\1", t).split(":", 1)[0].strip() for t in lines]
    steps_roots = [[roots(x) for x in (tp or [lab])] for tp, lab in zip(topics_of, labels)]
    read = readings(colls, steps_roots) if colls else {}
    role = roles(colls, body, title, steps_roots, declared, declared_why, read=read) if colls else {}
    ctx = _source_context(colls, role, lines, topics_of, labels, allowed, read)
    tree, gaps = [], []
    for i, text in enumerate(lines):
        how, doubt, missing, approx, hits = "", False, [], [], {}
        found = [p for p in (resolve_title(w, notes) for w in STEP_LINK_RE.findall(text)) if p]
        topics = topics_of[i]
        if topics:
            # every topic, linked or not: the autolinker links two of eight
            # topics in a syllabus row, and "by link" read only those two
            hits = topic_hits(topics, notes, allowed)
            more = list(dict.fromkeys(p for p, _how in hits.values() if p))
            missing = [t for t, (p, _how) in hits.items() if not p]
            # a note met by meaning only is the one the reader should check
            approx = [p for p, how_ in hits.values() if p and how_ == "meaning"]
            found = list(dict.fromkeys(more + found))  # the syllabus's order first
            how = f"{len(topics) - len(missing)} of {len(topics)} topics"
            doubt = len(missing) * 2 > len(topics)
        elif found:
            how = "by link"
        else:
            plain = labels[i]
            hit = resolve_title(plain, notes)
            if hit:
                found, how = [hit], "by title"
            else:
                found, how, doubt = _search_notes(plain, notes, explain=True)
        found = [p for p in found if p in allowed]
        node = {"node": labels[i], "children": [], "text": text}
        if rows[i].get("hours") is not None:
            node.update(hours=rows[i]["hours"], unit=rows[i].get("unit") or "")
        if rows[i].get("depth"):
            node["depth"] = rows[i]["depth"]
        if ctx["material"]:
            found, missing, approx, how, doubt = _with_sources(i, text, labels[i], topics, hits, found, how, doubt,
                                                               approx, node, ctx)
        # the reader's answer for a topic no source met ("is it one of these?")
        resolved = {t: resolve_title(matched[t], notes) for t in missing if matched and t in matched}
        said = {t: p for t, p in resolved.items() if p}
        if said:
            missing = [t for t in missing if t not in said]
            found = list(dict.fromkeys(list(found) + list(said.values())))
            node["matched"] = {t: Path(p).stem for t, p in said.items()}
            node["matched_paths"] = list(said.values())
        if missing:
            node["missing"] = missing
        if approx:
            node["approx"] = approx
        if ctx["material"] and ctx["by_meaning"].get(i):
            node["by_meaning"] = ctx["by_meaning"][i][:3]
        # by index from the form, by its text from the plan note (`refused:`)
        if refused and (i in refused or text in refused):
            node.update(leaves=[], gap=True, refused=len(found), how="refused")
        elif not found:
            node.update(leaves=[], gap=True, how=node.get("how", ""))
        else:
            node.update(leaves=found, gap=False, how=how, doubt=doubt)
        tree.append(node)
    if ctx["material"]:
        _fill_activity(tree, ctx, notes, refused or set())
        _one_note_per_step(tree, set(ctx["practice"]), ctx)
        _gap_candidates(tree, notes)
        if ctx["ordered"]:
            # position means something only when the material follows the reference
            _place_by_position(tree, ctx, refused or set())
    paths: list[str] = []
    for node in tree:
        if node["gap"]:
            gaps.append(node["text"])
        else:
            paths += [p for p in node["leaves"] if p not in paths]
    return {"paths": paths, "tree": tree, "gaps": gaps, "steps": lines,
            "sources": [{"key": c["key"], "label": c["label"], "kind": c["kind"], "spines": len(c["spines"]),
                         **role.get(c["key"], {})} for c in colls],
            "assessment": assessment_of(body) if body else [], "order_note": _order_note(ctx)}


def _order_note(ctx: dict) -> str:
    """What the plan says when its steps were matched without the order
    (ADR-0131 amendment, evening): the one failure that was silent."""
    if not ctx.get("material") or ctx.get("ordered"):
        return ""
    o = ctx.get("order")
    if o is None:
        return ("Too few of the material's sources meet the reference to follow its order: steps matched by "
                "their words; check them.")
    return f"The material does not follow the reference's order ({o:.2f}): steps matched by their words; check them."


def _source_context(colls: list[dict], role: dict, lines: list[str], topics_of: list[list[str]],
                    labels: list[str], allowed: set[str], read: dict[str, dict] | None = None) -> dict:
    """What every step line of one reference shares: the collections by
    role, the practice notes, the activity lines, and each step's rows of
    the material, aligned to the reference's order (ADR-0131 point 3)."""
    material = [c for c in colls if role.get(c["key"], {}).get("role") == MATERIAL]
    others = [c for c in colls if c not in material and role.get(c["key"], {}).get("role") != UNUSED]
    ctx: dict[str, Any] = {"material": material, "others": others, "practice": [], "activity": set(), "lines": lines,
           "origin": {}, "colls": colls, "rows": {}, "covered": {}, "meaning": {}, "ordered": False,
           "labels": labels, "by_meaning": {}}
    if not material:
        return ctx
    ctx["practice"] = practice_notes(allowed)
    ctx["activity"] = {i for i, t in enumerate(lines)
                       if not topics_of[i] and len(content_words(STEP_LINK_RE.sub(r"\1", t))) > GAP_MAX_WORDS}
    asked: dict[int, list[tuple[str, float]]] = {}
    for i, label in enumerate(labels):
        if i in ctx["activity"]:
            continue
        q = topics_of[i] or [label]
        asked[i] = [(t, W_PHRASE if len(roots(t)) > 1 else W_WORD) for t in q]
        if topics_of[i] and label and label not in q:
            asked[i].append((label, W_LABEL))
    row_notes = {r["note"] for c in material for r in c["rows"] if r["note"]}
    dense = _dense_rows([t for t in dict.fromkeys(t for v in asked.values() for t, _w in v)
                         if len(root_words(t)) > 1], row_notes)
    topic_roots = {t: roots(t) for v in asked.values() for t, _w in v}

    def holds(t: str, r: dict, floor: float) -> str:
        """"words", "meaning" or "" for one topic and one row. At the plan
        floor a meaning match must pass the sibling check; under it, inside
        a step's own rows, a sibling is the lecture's next idea, which is
        what the step wants: lecture 13's "Iperpiano a margine massimale"
        and "Forma duale della lagrangiana" for "iperpiano di separazione
        ottimale e sua formulazione lagrangiana" (2026-10-25)."""
        if held(topic_roots[t], r["roots"]):
            return "words"
        score, checked = dense.get(t, {}).get(r["note"], (0.0, False)) if r["note"] else (0.0, False)
        if floor >= PLAN_TITLE_FLOOR:
            return "meaning" if checked and score >= floor else ""
        return "meaning" if score >= floor else ""

    main = max(material, key=lambda c: len(c["rows"]))
    steps = []
    for i, ts in asked.items():
        scores: dict[int, float] = {}
        for n, r in enumerate(main["rows"]):
            w = 0.0
            for t, wt in ts:
                k = holds(t, r, PLAN_TITLE_FLOOR)
                w += wt if k == "words" else W_MEANING if k else 0.0
            if w:
                scores[n] = w
        # A lecture teaches a block when it holds a phrase of it or two of its
        # words: lecture 15's confusion matrix held "validazione" alone, and
        # the feedforward block opened on it, taking the metrics (2026-10-25).
        support: dict[str, float] = {}
        for n, w in scores.items():
            support[main["rows"][n]["spine"]] = support.get(main["rows"][n]["spine"], 0.0) + w
        steps.append({"i": i, "scores": {n: w for n, w in scores.items()
                                         if support[main["rows"][n]["spine"]] >= SPINE_SUPPORT}})
    # The order is a structure only when the material follows the reference
    # (ADR-0131 amendment, evening): a reference listed otherwise, its rows go
    # each to the step that holds them best, and a step's own rows are those of
    # the spines it took rows in rather than a stretch of the course.
    order = (read or {}).get(main["key"], {}).get("order")
    ctx["order"] = order
    ctx["ordered"] = order is not None and order >= ORDER_FLOOR
    names = dict(enumerate(labels))
    aligned = align(steps, main) if ctx["ordered"] else best_steps(steps, main, names)
    given = {n for v in aligned.values() for n in v}
    dense, ctx["generic"] = _without_generic(dense, asked, aligned, len(main["rows"]))
    picked_by: dict[int, list[dict]] = {}
    how: dict[tuple, str] = {}
    stray: set[tuple] = set()      # rows met outside a step's own lectures
    for i, ts in asked.items():
        mine = set(aligned.get(i, []))
        picked: list[dict] = []
        if mine:
            if ctx["ordered"]:
                local: range | list[int] = range(min(mine), max(mine) + 1)
            else:
                spines = {main["rows"][n]["spine"] for n in mine}
                local = [n for n, r in enumerate(main["rows"]) if r["spine"] in spines]
            for n in local:
                r = main["rows"][n]
                kinds = [holds(t, r, ROW_NEAR) for t, _w in ts]
                if n in mine or (n not in given and any(kinds)):
                    picked.append(r)
                    how[(r["spine"], r["i"])] = "words" if "words" in kinds else "meaning"
        for t, wt in ts:
            if wt == W_LABEL or any(holds(t, r, ROW_NEAR) for r in picked):
                continue
            # a phrase the aligned rows miss is looked for anywhere in the
            # material, and an unaligned step's every topic is; a lone word
            # held out of the step's rows is a word, not its lecture
            if mine and len(topic_roots[t]) < 2:
                continue
            for c in material:
                for n, r in enumerate(c["rows"]):
                    if c is main and n in given:
                        continue
                    k = holds(t, r, PLAN_TITLE_FLOOR)
                    if k:
                        picked.append(r)
                        how.setdefault((r["spine"], r["i"]), k)
                        stray.add((r["spine"], r["i"]))
        picked_by[i] = picked
    # who keeps a row two steps took: the first step in order, or by name when
    # there is no order, so a shuffled reference gives the same steps
    first = sorted(picked_by) if ctx["ordered"] else sorted(picked_by, key=lambda i: (names.get(i, ""), i))
    _whole_lectures(picked_by, material, stray, first)
    ctx["owned"] = {(r["spine"], r["i"]) for v in picked_by.values() for r in v}
    for i, ts in asked.items():
        sec = section(picked_by[i], material)
        ctx["rows"][i] = sec
        ctx["covered"][i] = {t for t, wt in ts if wt != W_LABEL and any(holds(t, r, ROW_NEAR) for r in sec)}
        ctx["meaning"][i] = {r["note"] for r in sec if r["note"] and how.get((r["spine"], r["i"])) == "meaning"}
        # a topic only a title vector met, and the note that met it best: the
        # coverage a reader should check, said by topic (amendment, point 5)
        pairs = []
        for t, wt in ts:
            if wt == W_LABEL or t not in ctx["covered"][i]:
                continue
            if any(holds(t, r, ROW_NEAR) == "words" for r in sec):
                continue
            near = [(dense.get(t, {}).get(r["note"], (0.0, False))[0], r["note"]) for r in sec if r["note"]]
            if near:
                pairs.append((t, Path(max(near)[1]).stem))
        ctx["by_meaning"][i] = pairs
    return ctx


GENERIC_MIN_STEPS = 3


GENERIC_SPAN = 0.5


def _without_generic(dense: dict, asked: dict, aligned: dict[int, list[int]], n_rows: int) -> tuple[dict, set[str]]:
    """The title-vector matches less the course's generic notes, and those
    notes (ADR-0131 amendment, point 7). A note is generic when topics of
    GENERIC_MIN_STEPS steps or more that share no word with its title meet it
    by meaning, and those steps' rows lie further apart in the material than
    GENERIC_SPAN of it: on the course (2026-10-25) Apprendimento artificiale
    was met by "introduzione al corso" and "computazione numerica" among
    others, from the first lecture to the last; Rete di percettroni was met
    from early and late blocks too, but by topics naming the perceptron. Words
    are never filtered, and the note stays where its lecture puts it: what
    goes is the claim that it teaches those topics. One corpus: reopen with
    the role thresholds."""
    import statistics

    anchor = {i: statistics.median(rows) for i, rows in aligned.items() if rows}
    steps_of: dict[str, set[int]] = {}
    for i, ts in asked.items():
        for t, _w in ts:
            words = set(root_words(t))
            for p in dense.get(t, {}):
                # a topic sharing a word with the title is the concept coming
                # back (the perceptron in three blocks), not a hub's likeness
                if not (words & _title_words(Path(p).stem)):
                    steps_of.setdefault(p, set()).add(i)
    generic = set()
    for p, steps in steps_of.items():
        at = [anchor[i] for i in steps if i in anchor]
        if len(steps) >= GENERIC_MIN_STEPS and len(at) >= 2 and (max(at) - min(at)) > GENERIC_SPAN * n_rows:
            generic.add(p)
    if not generic:
        return dense, generic
    return {t: {p: v for p, v in d.items() if p not in generic} for t, d in dense.items()}, generic


def _whole_lectures(picked_by: dict[int, list[dict]], material: list[dict], stray: set[tuple] | frozenset[tuple] = frozenset(),
                    first: list[int] | None = None) -> None:
    """A lecture the reference teaches is studied whole (ADR-0131 point 3):
    a row no step took, in a spine where steps took others, goes to the step
    of the nearest taken row of that spine, the earlier on a tie. Lecture
    15's SVDD rows follow its one-class row and no syllabus word names them;
    without this they were in no step, so not in the plan (2026-10-25). A
    spine no step touched stays out: the reference does not ask for it, and
    a row a step met outside its own lectures (`stray`) brings that row
    only: one title-vector row of lecture 1 had given the whole lecture to
    the exploratory-analysis block (2026-10-25)."""
    owner: dict[tuple, int] = {}
    for i in (first if first is not None else sorted(picked_by)):
        for r in picked_by[i]:
            if (r["spine"], r["i"]) not in stray:
                owner.setdefault((r["spine"], r["i"]), i)
    for c in material:
        for sp, rows in c["by_spine"].items():
            taken = [(r["i"], owner[(sp, r["i"])]) for r in rows if (sp, r["i"]) in owner]
            if not taken:
                continue
            for r in rows:
                if (sp, r["i"]) in owner or (sp, r["i"]) in stray:
                    continue
                _d, i = min(taken, key=lambda t: (abs(t[0] - r["i"]), t[0]))
                picked_by[i].append(r)


def _origin(path: str, ctx: dict) -> set[str]:
    if path not in ctx["origin"]:
        ctx["origin"][path] = note_collections([path], ctx["colls"]).get(path, set())
    return ctx["origin"][path]


def _with_sources(i: int, text: str, label: str, topics: list[str], hits: dict, found: list[str], how: str,
                  doubt: bool, approx: list[str], node: dict, ctx: dict):
    """(notes, missing, approx, how, doubt) of one step line read against
    the material's outline rows first (ADR-0131 points 3, 4 and 6)."""
    material = ctx["material"]
    mat_keys = {c["key"] for c in material}

    def ref_only(p: str) -> bool:
        # from sources, none of them the material: the reader's own notes
        # come from no collection and always stay
        ks = _origin(p, ctx)
        return bool(ks) and not (ks & mat_keys)

    if i in ctx["activity"]:
        # filled after every other step (_fill_activity): practice joins the
        # step whose theory it practises; a search hit on one shared word
        # ("supervisionata" for LVQ, 2026-10-25) never stands beside it, a
        # link the line itself makes does
        node["keep"] = [p for p in found if not doubt and not ref_only(p)]
        return node["keep"], [], [], how, doubt
    q = topics or [label]
    sec = ctx["rows"].get(i, [])
    covered = ctx["covered"].get(i, set())
    row_notes = [r["note"] for r in sec if r["note"]]
    approx = list(dict.fromkeys([p for p in approx if p in found] + [p for p in row_notes if p in ctx["meaning"].get(i, ())]))
    title_of_topic = {t: p for t, (p, _h) in hits.items() if p}
    kept, refs = [], []
    for p in found:
        topic = next((t for t, tp in title_of_topic.items() if tp == p), None)
        if ref_only(p):
            # the material teaches it: the book's note gives way to the rows
            if (topic is not None and topic in covered) or (topic is None and row_notes):
                continue
            refs.append(p)
        elif doubt and how.endswith("words") and row_notes:
            continue
        kept.append(p)
    missing, spines_extra = [], []
    for t in q if topics else []:
        if t in covered or t in title_of_topic:
            continue
        sp, key = leaf_spine(roots(t), material + ctx["others"])
        if sp:
            spines_extra.append(sp)
            if key not in mat_keys:
                refs.append(sp)
        else:
            missing.append(t)
    notes = list(dict.fromkeys(row_notes + kept + spines_extra))
    node["ref"] = [p for p in dict.fromkeys(refs) if p in notes]
    node["spines"] = sorted({r["spine"] for r in sec})
    node["rows"] = len(sec)
    if topics:
        how = f"{len(q) - len(missing)} of {len(q)} topics" + (f" · {len(sec)} rows" if sec else "")
        doubt = len(missing) * 2 > len(q)
    elif sec:
        how, doubt = f"{len(sec)} rows", False
    return notes, missing, approx, how, doubt


def _fill_activity(tree: list[dict], ctx: dict, notes: dict, refused: set) -> None:
    """An activity line's notes (ADR-0131 point 6), once the theory steps
    hold theirs: the practice notes its distinctive words weigh most in
    (`sources.practice_for`), and the rows of the material's spines no
    theory step took (the exercise decks) that hold half of them."""
    act = sorted(ctx["activity"])
    free = [r for c in ctx["material"] for r in c["rows"] if (r["spine"], r["i"]) not in ctx.get("owned", set())]
    for i in act:
        node = tree[i]
        if node.get("how") == "refused" or i in refused or node["text"] in refused:
            continue
        words = distinctive(node["text"], [tree[j]["text"] for j in act if j != i])
        labs = practice_for(words, ctx["practice"])
        sec = section([r for r in free if half_held(words, r["roots"])], ctx["material"])
        leaves = list(dict.fromkeys(labs + [r["note"] for r in sec if r["note"]] + node.pop("keep", [])))
        node.update(practice=labs, spines=sorted({r["spine"] for r in sec}), rows=len(sec))
        if leaves:
            node.update(leaves=leaves, gap=False, how=f"{len(labs)} practice · {len(sec)} rows", doubt=False)
        else:
            node.update(leaves=[], gap=True, how="no practice note found")


CANDIDATES = 4


def _gap_candidates(tree: list[dict], notes: dict) -> None:
    """For each topic a step names and no source met, the step's notes
    nearest it by title vector (by shared words without an embedder): the
    choices of "is it one of these?" (ADR-0131 amendment, point 5). Lecture
    19 says "Schemi auto-associativi" where the syllabus says "schemi
    codificatore/decodificatore", and nothing met it."""
    asks = [(n, t) for n in tree if not n.get("gap") for t in n.get("missing") or []]
    if not asks:
        return
    store: Any
    try:
        from collections import Counter

        from silica.model.providers import get_embedder_or_none
        from silica.config import CONFIG
        from silica.kernel.recall.embed import _note_title_text, get_store

        store = get_store()
        embedder = get_embedder_or_none(CONFIG, "plan topics", level="info") if len(store) else None
    except Exception as exc:  # noqa: BLE001
        # no index: the choices fall back to shared words
        logger.info("plan gaps: no title vectors (%s)", exc)
        store = embedder = None
    for node, t in asks:
        leaves = [p for p in node["leaves"] if p.endswith(".md")]
        ranked: list[str] = []
        if embedder is not None and leaves:
            try:
                keys = {p.removesuffix(".md"): p for p in leaves}
                folder = Counter(p.rsplit("/", 1)[0] for p in keys if "/" in p).most_common(1)
                vec = _embed([_note_title_text(t, folder=folder[0][0] if folder else "")], embedder)[0]
                outside = {p for p in store.paths() if p not in keys}
                ranked = [keys[h["path"]] for h in store.title_cosine_top_k(vec, k=CANDIDATES, exclude=outside)
                          if h["path"] in keys]
            except Exception as exc:  # noqa: BLE001
                # a failed lookup leaves the shared-words choices below
                logger.info("plan gaps: candidates by words for %r (%s)", t, exc)
        if not ranked:
            words = set(root_words(t))
            ranked = sorted(leaves, key=lambda p: -len(words & set(root_words(Path(p).stem))))[:CANDIDATES]
        node.setdefault("candidates", {})[t] = [Path(p).stem for p in ranked[:CANDIDATES]]


def _one_note_per_step(tree: list[dict], practice: set[str], ctx: dict | None = None) -> None:
    """A note met by two steps stays in the first (ADR-0131 point 5); the
    later step still counts its topic as matched. A practice note practises
    several steps and is never moved, and a step whose every note sits in
    earlier steps keeps them rather than turning into a gap. With no order
    (a reference the material does not follow) "first" is the step whose own
    rows hold the note, then the step's name, so the order of the list does
    not decide."""
    seen: set[str] = set()
    walk = list(range(len(tree)))
    if ctx is not None and not ctx.get("ordered", True):
        rows_of = {i: {r["note"] for r in ctx["rows"].get(i, []) if r["note"]} for i in walk}
        owner: dict[str, int] = {}
        for i in sorted(walk, key=lambda i: (tree[i]["node"], i)):
            for p in tree[i].get("leaves") or []:
                if p in practice:
                    continue
                held = p in rows_of[i]
                if p not in owner or (held and p not in rows_of[owner[p]]):
                    owner[p] = i
        for i, node in enumerate(tree):
            if node.get("gap"):
                continue
            rest = [p for p in node["leaves"] if p in practice or owner.get(p) == i]
            if rest:
                node["leaves"] = rest
        return
    for node in (tree[i] for i in walk):
        if node.get("gap"):
            continue
        # the reader's answer for a topic stays with the topic, like practice
        mine = set(node.get("matched_paths") or ())
        rest = [p for p in node["leaves"] if p in practice or p in mine or p not in seen]
        if rest:
            node["leaves"] = rest
        seen.update(node["leaves"])


def _place_by_position(tree: list[dict], ctx: dict, refused: set) -> None:
    """A step still empty in a sequenced reference takes the material's
    spines between the step before it and the step after it that no step
    holds (ADR-0131 point 7): the first programme line, glued past reading,
    takes the lectures before the first matched block."""
    if len(tree) < 4:
        return
    coll = max(ctx["material"], key=lambda c: len(c["rows"]))
    pos = {sp: n for n, sp in enumerate(coll["spines"])}
    at = [_median([pos[s] for s in n.get("spines", []) if s in pos]) for n in tree]
    claimed = {p for n in tree if not n.get("gap") for p in n["leaves"]}
    for i, node in enumerate(tree):
        if not node.get("gap") or node.get("how") == "refused" or i in refused or node["text"] in refused:
            continue
        before = [a for a in at[:i] if a is not None]
        after = [a for a in at[i + 1:] if a is not None]
        lo, hi = (before[-1] if before else -1), (after[0] if after else len(coll["spines"]))
        take = []
        for sp in coll["spines"]:
            if lo < pos[sp] < hi:
                rows = [r for r in coll["by_spine"].get(sp, []) if r["note"]]
                free = [r["note"] for r in rows if r["note"] not in claimed]
                if rows and len(free) * 2 >= len(rows):
                    take += free
        if take:
            node.update(leaves=list(dict.fromkeys(take)), gap=False, how="by position", doubt=False)
            node.pop("missing", None)
            claimed.update(take)


def _median(values: list[int]) -> float | None:
    import statistics

    return statistics.median(values) if values else None


def _unglued(rows: list[dict], notes: dict, allowed: set[str], colls: list[dict]) -> list[dict]:
    """Rows whose glued tokens are split into the vault's words (ADR-0131
    point 7). The vocabulary is read only when a token is long enough to be
    a glued line: a root of GAP_GLUED letters no word of the vault spells."""
    vocab = None
    out = []
    for r in rows:
        text = r["text"]
        for tok in set(re.findall(r"[^\W\d_]+", text)):
            if len(_root(tok.casefold())) < GAP_GLUED:
                continue
            if vocab is None:
                vocab = vocabulary(notes, allowed, colls)
            if tok.casefold() in vocab:
                continue
            split = split_glued(tok, vocab)
            if split:
                text = re.sub(rf"\b{re.escape(tok)}\b", split, text)
        out.append({**r, "text": text} if text != r["text"] else r)
    return out


# Words a title opens on that say what kind of note it is, not what it is
# about: "Definizione di kernel" is the note on kernels, and "Kernel CCA" is
# not (2026-10-23).
# A syllabus line says the same of a topic: "analisi discriminante lineare",
# "casi di convoluzione efficiente", "tipi di apprendimento".
_GENERIC_HEADS = {"definizion", "introduzion", "concett", "definition", "introduction", "concept",
                  "analis", "cas", "tip", "propriet", "operazion"}


def _title_words(stem: str) -> set[str]:
    """A title's root words as topic matching reads them, less the words that
    only name the kind of note. Acronyms stay uncounted: counted, "API" made
    "Keras come API" miss the one-word topic "keras" (2026-10-23)."""
    return set(root_words(stem)) - _GENERIC_HEADS


def topic_hits(topics: list[str], notes: dict, allowed: set[str]) -> dict[str, tuple[str | None, str]]:
    """{topic: (note or None, how)} for the topics of one syllabus block. How:
    "title", "words", the fallback that named it (`_named_otherwise`), or
    "meaning" for a title vector, the one a reader should check.

    By title, else the note whose name holds most of the topic's content
    words, more than half of them, then the one adding fewest words of its
    own: "regressione lineare multipla" is "Regressione lineare", not
    "Regressione lineare bayesiana", which the shorter path had won from a
    subfolder (2026-10-23). A shared "analisi" is not a match, and half of two
    words was one: "noise injection" met "Noise-contrastive estimation"."""
    # A merge loser's name still says what it is about; the match lands on
    # its winner ("Set di Validazione" for "validazione", 2026-10-23).
    losers = {p for p, m in notes.items() if m.get("winner") and resolve_title(p, notes) in allowed}
    stems = {p: _title_words(Path(p).stem) for p in sorted(allowed | losers)}
    block = {w for t in topics for w in root_words(t)}
    hits: dict[str, tuple[str | None, str]] = {}
    for t in topics:
        hit = resolve_title(t, notes)
        how = "title"
        if not hit or hit not in allowed:
            hit, how = None, "words"
            # each word once: "lineare e non lineare" counted "lineare" twice
            words = list(dict.fromkeys(w for w in root_words(t) if w not in _GENERIC_HEADS)) or root_words(t)
            # A one-word topic names a thing only by a title holding that word
            # and nothing the block does not list: "ottimizzazione" met
            # "Ottimizzazione del codice" and opened the plan on it, and
            # "architettura" met "Architettura RBFN", while "overfitting" and
            # "underfitting", listed together, are "Underfitting e overfitting"
            # (2026-10-23).
            pool = stems if len(words) > 1 else {p: s for p, s in stems.items() if s and set(words) <= s <= block}
            # then the fewest words of its own, then a note that defines the
            # thing ("Definizione di kernel" over "Kernel CCA"), then the shorter
            ranked = sorted(((sum(1 for w in words if w in s), -len(s - set(words)),
                              bool(set(root_words(Path(p).stem)) & _GENERIC_HEADS), -len(Path(p).stem), p)
                             for p, s in pool.items()), reverse=True)
            got, best = (ranked[0][0], ranked[0][4]) if ranked else (0, None)
            best = _translating(words, [r[4] for r in ranked if r[0] == got][:8], stems) if best else best
            # the joined form of "ada-boost" is a spelling of its words, not one more
            base = len({_root(w) for w in content_words(t)} - _GENERIC_HEADS)
            # A topic of one or two words is not a note that adds two of its
            # own: "algoritmi di apprendimento" is no "Algoritmi con tassi di
            # apprendimento adattivi" (2026-10-23).
            narrower = best and base <= 2 and len(stems[best] - set(words)) >= 2
            if best and got and got * 2 > (base or len(words)) and not narrower:
                hit = resolve_title(best, notes) if best in losers else best
            if not hit:
                hit, how = _named_otherwise(t, notes, allowed)
        hits[t] = (hit, how)
    # Past the words, the meaning: a textbook translated into Italian named
    # its notes "Iniezione di rumore negli input" and "Architettura
    # encoder-decoder" for a syllabus that says "noise injection" and
    # "architetture codificatore-decodificatore", and the plan called both not
    # in the vault (2026-10-23). Topics of two words or more only: a lone
    # "architettura" met "Diversità architetturale" at 0.80, as near as the
    # right answers were.
    near = _by_title_vector([t for t, (p, _h) in hits.items() if not p and len(root_words(t)) > 1], allowed)
    for t, p in _translated(near).items():
        hits[t] = (p, "meaning")
    return hits


def _translating(words: list[str], tied: list[str], stems: dict[str, set[str]]) -> str:
    """Among titles holding as many of the topic's words, the first that
    holds, in its own words, a translation of the ones it misses: "Definizione
    di reti convolutive" missed "retropropagazione", which "Backpropagation
    nelle reti convolutive" translates (2026-10-23). The first title when none
    does, or with no embedder to tell."""
    best = tied[0]
    missed = " ".join(w for w in words if w not in stems[best])
    others = [p for p in tied[1:] if stems[p] - set(words)]
    if not missed or not others:
        return best
    try:
        import numpy as np

        from silica.model.providers import get_embedder_or_none
        from silica.config import CONFIG

        embedder = get_embedder_or_none(CONFIG, "plan topics", level="info")
        if embedder is None:
            return best
        vecs = embedder.embed([missed] + [" ".join(sorted(stems[p] - set(words))) for p in others])
        x = np.asarray(vecs[0], float)
        for p, v in zip(others, vecs[1:]):
            y = np.asarray(v, float)
            if float(x @ y / (np.linalg.norm(x) * np.linalg.norm(y) or 1.0)) >= TRANSLATED_PARTS:
                return p
    except Exception as exc:  # noqa: BLE001
        # No embedder: the title with fewest words of its own stands, as before.
        logger.info("plan topics: translation check skipped (%s)", exc)
    return best


# A title vector near a topic because of one shared word is a sibling, not the
# thing: "percettrone multistrato" met "Algoritmo del percettrone" and
# "apprendimento hebbiano" met "Apprendimento artificiale" (0.81 and 0.83,
# 2026-10-23). What tells a translation from a sibling is the words the two do
# not share: measured, translated parts scored 0.61 to 0.94 (autocodificatori
# and autoencoder, data and dataset) and sibling parts 0.26 to 0.38 (multistrato
# and algoritmo, hebbiano and artificiale).
TRANSLATED_PARTS = 0.5


def _translated(near: dict[str, str]) -> dict[str, str]:
    """The title-vector matches that survive the sibling check: none shared
    word, or the parts each side does not share read as one another."""
    ok = _translated_pairs(list(near.items()))
    return {t: p for t, p in near.items() if (t, p) in ok}


def _translated_pairs(pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """The (topic, note) pairs that survive the sibling check, several notes
    a topic allowed (a step's outline rows, ADR-0131)."""
    todo: list[tuple[tuple[str, str], str, str]] = []
    out: set[tuple[str, str]] = set()
    for t, p in pairs:
        tw, hw = set(root_words(t)) - _GENERIC_HEADS, _title_words(Path(p).stem)
        only_t, only_h = tw - hw, hw - tw
        if not (tw & hw) or not only_t or not only_h:
            out.add((t, p))      # a pure translation, or one side covering the other
            continue
        todo.append(((t, p), " ".join(sorted(only_t)), " ".join(sorted(only_h))))
    if not todo:
        return out
    try:
        import numpy as np

        from silica.model.providers import get_embedder_or_none
        from silica.config import CONFIG

        embedder = get_embedder_or_none(CONFIG, "plan topics", level="info")
        if embedder is None:
            return out
        vecs = _embed([a for _t, a, _b in todo] + [b for _t, _a, b in todo], embedder)
        n = len(todo)
        for i, (pair, _a, _b) in enumerate(todo):
            x, y = np.asarray(vecs[i], float), np.asarray(vecs[n + i], float)
            if float(x @ y / (np.linalg.norm(x) * np.linalg.norm(y) or 1.0)) >= TRANSLATED_PARTS:
                out.add(pair)
    except Exception as exc:  # noqa: BLE001
        # No embedder for the check: the doubtful matches are dropped, never
        # linked unchecked.
        logger.info("plan topics: sibling check skipped (%s)", exc)
    return out


# A plan's link is read as "this note covers the topic", with no judge after
# it, so the dedup title band (0.709 on jina-v5-small) is too low a bar: on 12
# topics of a course syllabus it let in "Output strutturati" for "funzioni
# kernel per dati strutturati" (0.729) and a general schema for "schemi
# codificatore/decodificatore" (0.712), while the 8 right notes scored 0.760
# to 0.883 (2026-10-23). An embedder whose band is higher keeps its own.
PLAN_TITLE_FLOOR = 0.75


def _by_title_vector(topics: list[str], allowed: set[str]) -> dict[str, str]:
    """{topic: note} for each topic whose name sits within the title band
    (CONFIG.sim_title_threshold, at least PLAN_TITLE_FLOOR) of an allowed
    note's title vector. The topic is embedded as the index embeds a title,
    under the folder most of the notes share, so the band compares like with
    like. {} without an embedder or an index."""
    if not topics or not allowed:
        return {}
    try:
        from collections import Counter

        from silica.model.providers import get_embedder_or_none
        from silica.config import CONFIG
        from silica.kernel.recall.embed import _note_title_text, get_store

        store = get_store()
        embedder = get_embedder_or_none(CONFIG, "plan topics", level="info") if len(store) else None
        if embedder is None:
            return {}
        keys = {p.removesuffix(".md"): p for p in allowed}
        outside = {p for p in store.paths() if p not in keys}
        folder = Counter(p.rsplit("/", 1)[0] for p in keys if "/" in p).most_common(1)
        texts = [_note_title_text(t, folder=folder[0][0] if folder else "") for t in topics]
        out: dict[str, str] = {}
        for t, vec in zip(topics, _embed(texts, embedder)):
            hit = store.title_cosine_top_k(vec, k=1, exclude=outside)
            if hit and hit[0]["path"] in keys and hit[0]["score"] >= max(CONFIG.sim_title_threshold, PLAN_TITLE_FLOOR):
                out[t] = keys[hit[0]["path"]]
    except Exception as exc:  # noqa: BLE001
        # No index or no embedder: the topics stay not in the vault, which is
        # what their words said.
        logger.info("plan topics: no title-vector match (%s)", exc)
        return {}
    return out


# Texts embedded during this process, per vault index directory, by model and
# text: the plan form re-previews on every change, and the same ninety topics
# were embedded again each time; the model is in the key so a changed
# embedder never reuses them, and the vault is, so one vault's topics never
# land in another's index (ADR-0014: per-vault state follows the vault).
_VEC_MEMO: dict[str, dict[tuple[str, str], list[float]]] = {}


VEC_MAX = 4000


def _embed(texts: list[str], embedder) -> list[list[float]]:
    from silica.kernel.base.home import index_dir

    model = str(getattr(embedder, "model", "") or type(embedder).__name__)
    memo = _VEC_MEMO.get(str(index_dir()))
    if memo is None:
        memo = _VEC_MEMO[str(index_dir())] = _vectors_on_disk()
    todo = [t for t in dict.fromkeys(texts) if (model, t) not in memo]
    if todo:
        for k in list(memo)[:max(0, len(memo) + len(todo) - VEC_MAX)]:
            del memo[k]           # the oldest first, so the file stays bounded and keeps the rest
        memo.update(((model, t), v) for t, v in zip(todo, embedder.embed(todo)))
        _vectors_on_disk(memo)
    return [memo[(model, t)] for t in texts]


def _vectors_on_disk(value: dict | None = None) -> dict:
    """Read (no value) or write the plan's topic vectors in the vault's index
    directory, keyed "model<TAB>text": after a restart the first preview of
    the course embedded its 120 topics again, 4.5 of its 13 s, and on hosted
    each is a paid call (ADR-0131 amendment, point 3). {} when absent or
    unreadable."""
    import orjson

    from silica.kernel.base.durable import atomic_write_bytes
    from silica.kernel.base.home import index_dir

    path = index_dir() / "plan_vectors.json"
    try:
        if value is not None:
            atomic_write_bytes(path, orjson.dumps({f"{m}\t{t}": v for (m, t), v in value.items()},
                                                  option=orjson.OPT_SERIALIZE_NUMPY))
            return value
        data = orjson.loads(path.read_bytes())
        return {tuple(k.split("\t", 1)): v for k, v in data.items() if "\t" in k}
    except (OSError, ValueError, TypeError):
        # A derived cache: unreadable means embed again, unwritable means the
        # next process embeds again, never a wrong vector (the model is in the key)
        return {}


# A row's idea note met by a topic's title vector (ADR-0131 point 3). Read on
# the course (2026-10-25) with the sibling check on: the plan floor (0.75)
# keeps the right rows ("formulazione lagrangiana" 0.99, "reti ricorrenti
# bidirezionali" to Reti bidirezionali 0.94, "versione duale dell'algoritmo
# del percettrone" to Forma duale del percettrone 0.91), and a course's
# generic hubs reach it too (Apprendimento artificiale, 0.68 to 0.84). So a
# row met by meaning alone helps align a step only at the floor, and joins a
# step under it, down to ROW_NEAR, only inside the rows its words already
# gave that step: "autocodificatori di riduzione" meets Autoencoders at
# 0.747, "Macchine a vettori di supporto" Punti di supporto at 0.734, while
# the wrong "Problemi di ottimizzazione" for "iperpiano di separazione
# ottimale" sits at 0.717.
ROW_TOP_K = 5


ROW_NEAR = 0.72


def _dense_rows(topics: list[str], row_notes: set[str]) -> dict[str, dict[str, tuple[float, bool]]]:
    """{topic: {idea note of a material row: (score, passed the sibling
    check)}} at ROW_NEAR or more; {} without an embedder."""
    if not topics or not row_notes:
        return {}
    try:
        from collections import Counter

        from silica.model.providers import get_embedder_or_none
        from silica.config import CONFIG
        from silica.kernel.recall.embed import _note_title_text, get_store

        store = get_store()
        embedder = get_embedder_or_none(CONFIG, "plan topics", level="info") if len(store) else None
        if embedder is None:
            return {}
        keys = {p.removesuffix(".md"): p for p in row_notes}
        outside = {p for p in store.paths() if p not in keys}
        folder = Counter(p.rsplit("/", 1)[0] for p in keys if "/" in p).most_common(1)
        texts = [_note_title_text(t, folder=folder[0][0] if folder else "") for t in topics]
        scored = []
        for t, vec in zip(topics, _embed(texts, embedder)):
            for hit in store.title_cosine_top_k(vec, k=ROW_TOP_K, exclude=outside):
                if hit["path"] in keys and hit["score"] >= ROW_NEAR:
                    scored.append((t, keys[hit["path"]], float(hit["score"])))
        ok = _translated_pairs([(t, p) for t, p, _s in scored])
    except Exception as exc:  # noqa: BLE001
        # No index or no embedder: the rows meet the topics by their words only.
        logger.info("plan rows: no title-vector match (%s)", exc)
        return {}
    out: dict[str, dict[str, tuple[float, bool]]] = {}
    for t, p, sc in scored:
        out.setdefault(t, {})[p] = (sc, (t, p) in ok)
    return out


# A topic's letters in a title: "learning vector quantization" is the note
# "LVQ come estensione supervisionata" (2026-10-23).
_ACRONYM_SPAN = (2, 4)


_SEGMENT_SLUG_RE = re.compile(r"-\d+(?:-\d+)*-([a-z][a-z0-9-]*)$")


# The same spine as convert names it since 2026-10-26: "<source>, <section>".
_SEGMENT_TITLE_RE = re.compile(r", ([^,]+)$")


def _named_otherwise(topic: str, notes: dict, allowed: set[str]) -> tuple[str | None, str]:
    """(the note a topic names in a way its title's words do not show, which
    way), or (None, "").

    In this order, each a thing the notes already carry, measured on a course
    syllabus whose plan called these not in the vault (2026-10-23):
    - an alias, as merges record them on the winner ("Classificazione
      one-class" on One-class classification);
    - a gloss in brackets: "stride" is "Convoluzione con passo (stride)";
    - the initials of two to four of its words as a title's word, the title
      opening on it first: "reti self organizing map" is "SOM e topologia";
    - a textbook segment's spine whose section title is the topic: "dropout"
      is the spine of 7.12, "Goodfellow-20-7-12-dropout" (or, named since
      2026-10-26, "Goodfellow, Dropout"), whose ideas are the dropout notes.
    """
    key = " ".join(root_words(topic))
    low = topic.strip().casefold()
    if not key:
        return None, ""
    for p in sorted(allowed, key=len):
        if any(" ".join(root_words(a)) == key for a in notes.get(p, {}).get("aliases", ())):
            return p, "alias"
    for p in sorted(allowed, key=len):
        if f"({low})" in Path(p).stem.casefold():
            return p, "gloss"
    # by length, not a stopword list: "self" is an English stopword and the S of SOM
    words = [w for w in re.findall(r"[^\W\d_]+", low) if len(w) > 2]
    initials = {"".join(w[0] for w in words[i:i + n])
                for n in range(_ACRONYM_SPAN[0], _ACRONYM_SPAN[1] + 1) for i in range(len(words) - n + 1)}
    acr = []
    for p in allowed:
        toks = re.findall(r"[a-z]+", Path(p).stem.casefold())
        if any(len(t) >= 3 and t in initials for t in toks):
            acr.append((0 if toks[0] in initials else 1, len(p), p))
    if acr:
        return min(acr)[2], "initials"
    for p in sorted(allowed, key=len):
        stem = Path(p).stem.casefold()
        m = notes.get(p, {}).get("spine") and (_SEGMENT_SLUG_RE.search(stem) or _SEGMENT_TITLE_RE.search(stem))
        if m and " ".join(root_words(m.group(1).replace("-", " "))) == key:
            return p, "segment"
    return None, ""


@functools.lru_cache(maxsize=50_000)
def _root_words_cached(text: str) -> tuple[str, ...]:
    joined = [re.sub(r"[-\s]", "", m).casefold() for m in re.findall(r"\b\w+-\w+\b", text)]
    return tuple(_root(w) for w in content_words(text) + joined)


def root_words(text: str) -> list[str]:
    """Content words without their last vowel, so an Italian plural meets its
    singular: "reti neurali" and "Rete neurale" share both words."""
    # "ada-boost" in a syllabus is the note "AdaBoost": the joined form too.
    # Cached: a course preview asked for the roots of the same 1,100 titles
    # 72,000 times, 2.5 s of a cold preview (2026-10-25)
    return list(_root_words_cached(text))


def _root(word: str) -> str:
    return word[:-1] if len(word) >= 4 and word[-1] in "aeiouàèéìòù" else word


def content_words(text: str) -> list[str]:
    from silica.kernel.text.language import study_stopwords

    stop = study_stopwords()
    return [w for w in re.findall(r"[^\W\d_]{4,}", text.casefold()) if w not in stop]


def _search_notes(text: str, notes: dict, k: int = 2, explain: bool = False):
    """One body search per step line, title-ish: the notes whose stem the
    step's longest words hit, else the search-context leg. With `explain`,
    (paths, how, doubtful): how is `k of n words` counted on content words,
    so a match on "della" alone reads 0 of 2, or `by search`.

    ponytail: matching itself still takes any 4-letter word; raising the bar
    (a share of the words, a floor on the search score) waits for a real
    syllabus to measure it on (ADR-0081, not decided)."""
    words = [w for w in re.findall(r"[^\W\d_]{4,}", text.casefold())]
    if not words:
        return ([], "", False) if explain else []
    scored = []
    for p in notes:
        stem = Path(p).stem.casefold()
        hits = sum(1 for w in words if w in stem)
        if hits:
            scored.append((-hits, p))
    if scored:
        best = [p for _h, p in sorted(scored)[:k]]
        if not explain:
            return best
        content = content_words(text)
        top = Path(best[0]).stem.casefold()
        got = sum(1 for w in content if w in top)
        n = max(1, len(content))
        return best, f"{got} of {n} word{'s' if n > 1 else ''}", got * 2 <= n
    try:
        from silica.driver import DRIVER

        rows = DRIVER.search_context(" ".join(words[:4]))
        seen: list[str] = []
        for r in rows or []:
            rp = getattr(getattr(r, "ref", None), "path", None) or (r.get("path") if isinstance(r, dict) else None)
            if rp and rp in notes and rp not in seen:
                seen.append(rp)
        return (seen[:k], "by search", True) if explain else seen[:k]
    except Exception:
        # no index (a fresh vault, a test): the line stays a gap, which is
        # what a step with no note is
        return ([], "", False) if explain else []


# The syllabus topics the material never mentions: what a course asks and its
# sources leave to the book or the guide. Read off the material's own text,
# because asked which topics the slides miss, the local 9B confused "in the
# vault" with "in the slides" and listed half the programme (2026-10-23). The
# material is the plan's material collections (ADR-0131 point 12), however
# its decks are named: a lesson word at the head of a title was the test
# until then.


GAP_MAX_WORDS = 6      # a longer line is an activity ("Applicazione dei modelli ... su dataset reali"), not a topic


GAP_GLUED = 16         # a root this long is a conversion's glued line ("Introduzionealcorso", 17), unreadable; "regolarizzazione" roots to 15


_GAPS_CACHE: dict[tuple, tuple[list[str], int] | None] = {}


def _material_of(plan: dict, notes: dict) -> tuple[list[dict], dict, str, list[dict]]:
    """(the plan's material collections, every collection's role, the
    reference's body, its rows), the roles as the plan states them or as
    ADR-0131 reads them."""
    from silica.kernel.base.noteshape import is_material
    from silica.kernel.base import frontmatter

    ref = resolve_title(plan.get("target") or "", notes) if plan.get("scope") == "steps" else None
    if not ref:
        return [], {}, "", []
    _d, _r, body = frontmatter.split((store.vault() / ref).read_text(encoding="utf-8"))
    bounds = tuple(b.strip().casefold() for b in str(plan.get("restrict") or "").split(RESTRICT_SEP) if b.strip())
    allowed = {p for p, m in notes.items() if is_material(m) and (not bounds or p.casefold().startswith(bounds))}
    colls = collections(notes, allowed)
    rows = _unglued(step_rows(body) or reference_rows(body), notes, allowed, colls)
    lines = [r["text"] for r in rows]
    labels = [STEP_LINK_RE.sub(r"\1", t).split(":", 1)[0].strip() for t in lines]
    steps = [[roots(x) for x in (line_topics(t) or [lab])] for t, lab in zip(lines, labels)]
    role = roles(colls, body, note_title(body) or Path(ref).stem, steps, declared_of(plan)) if colls else {}
    return [c for c in colls if role[c["key"]]["role"] == MATERIAL], role, body, rows


def lecture_gaps(plan: dict, notes: dict | None = None) -> tuple[list[str], int] | None:
    """(the topics of the plan's syllabus whose words no deck of its material
    holds, the decks read), or None when the plan has no syllabus or no
    material. A topic is held when one deck holds two thirds of its words
    (all of them for one or two, `held`), read as roots so a plural meets its
    singular; activity lines and lines still glued are not judged."""
    # 5.6 s on the course's 32 decks, most of it the title vectors: once per
    # plan note as written, since its syllabus and its folder are the input
    try:
        key = (plan.get("path"), (store.vault() / plan["path"]).stat().st_mtime_ns)
    except (KeyError, TypeError, OSError):
        key = None
    if key and key in _GAPS_CACHE:
        return _GAPS_CACHE[key]
    notes = notes if notes is not None else note_index.meta(store.vault())
    material, _role, _body, rows = _material_of(plan, notes)
    decks = [leaf_roots(leaf) for c in material for leaf in c["leaves"]]
    decks = [d for d in decks if d]
    if not decks:
        return None
    lines = [r["text"] for r in rows]

    def in_a_deck(words: set[str]) -> bool:
        # a title with no content word has nothing a deck could miss
        return not words or any(held(words, d) for d in decks)

    misses = []
    for line in lines:
        for topic in line_topics(line) or [STEP_LINK_RE.sub(r"\1", line).split(":", 1)[-1].strip(" .")]:
            words = roots(topic)
            if not words or len(words) > GAP_MAX_WORDS or any(len(w) >= GAP_GLUED for w in words):
                continue
            if not in_a_deck(words):
                misses.append(topic)
    # The syllabus is in Italian and the slides often keep the English term
    # ("autocodificatori" against "autoencoder", "sequence-to-sequence"
    # against "seq2seq"): a topic the plan matched to a note is held when a
    # deck holds that note's title, the match having crossed the language.
    restrict = (plan.get("restrict") or "").split(RESTRICT_SEP)[0].strip().rstrip("/")
    allowed = {p for p in notes if not restrict or p.startswith(restrict + "/")}
    hits = topic_hits(list(dict.fromkeys(misses)), notes, allowed) if misses else {}
    gaps = [t for t in misses if not ((hit := hits.get(t, (None, ""))[0]) and in_a_deck(roots(Path(hit).stem)))]
    out = (list(dict.fromkeys(gaps)), len(decks))
    if key:
        _GAPS_CACHE[key] = out
    return out
