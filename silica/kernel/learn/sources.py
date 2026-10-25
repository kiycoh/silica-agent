# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The sources a plan studies (ADR-0131): collections and their role.

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

import re
import statistics
from collections import Counter
from pathlib import Path

MATERIAL, REFERENCE, UNUSED = "material", "reference", "unused"
ROLES = (MATERIAL, REFERENCE, UNUSED)

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

_ROW_RE = re.compile(r"^\s*\d+[.)]\s+\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]:?\s*(.*)$")
_LEAF_RE = re.compile(r"\[\[(sources/[^\]|#]+)")
_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]")
_PRACTICE_RE = re.compile(r"^##\s+Concepts practised\s*$", re.M)
# A cell, line or heading that introduces a reference's books: what the
# reference names there is recommended reading, not what it was taught from.
_TEXTS_RE = re.compile(r"\b(testi|libri|bibliografia|letture|textbooks?|books|readings?|bibliography|"
                       r"recommended reading|reading list)\b", re.I)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_NUM_RE = re.compile(r"\d+|\D+")


def _vault() -> Path:
    from silica.config import CONFIG

    return Path(CONFIG.vault_path)


def _read(path: str) -> str:
    try:
        return (_vault() / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def natural_key(text: str) -> list:
    """"Lezione 2" before "Lezione 10": numbers read as numbers."""
    return [int(t) if t.isdigit() else t.casefold() for t in _NUM_RE.findall(text)]


def plain(text: str) -> str:
    """A row or a line with its links read as their words."""
    return _LINK_RE.sub(lambda m: m.group(2) or m.group(1), text)


def roots(text: str) -> set[str]:
    """Content roots as topic matching reads them, an English plural folded:
    "kernel tricks" is held by a deck that says "kernel trick"."""
    from silica.kernel.learn.plans import _root_words

    return {w[:-1] if len(w) > 4 and w.endswith("s") else w for w in _root_words(text)}


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
    from silica.kernel.report.learner import parsed_note

    spines = sorted((p for p in allowed if notes.get(p, {}).get("spine")), key=natural_key)
    memo_key = (str(_vault()), tuple(spines), tuple(notes[p].get("mtime", 0) for p in spines))
    if memo_key in _MEMO:
        return _MEMO[memo_key]
    vault = _vault()
    found: list[tuple[str, str, str]] = []   # (spine, leaf, source_file)
    for sp in spines:
        m = _LEAF_RE.search(_read(sp))
        if not m:
            continue   # an outline the reader wrote: no source behind it
        leaf = m.group(1).removesuffix(".md")
        parsed = parsed_note(vault, leaf + ".md")
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
    from silica.kernel.learn.plans import resolve_title

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
        for leaf in _LEAF_RE.findall(_read(p)):
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
    f = _vault() / (leaf + ".md")
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
        h = _HEADING_RE.match(s)
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
          declared: dict[str, str] | None = None, declared_why: str = "you set it") -> dict[str, dict]:
    """{key: {role, why}}, in ADR-0131's order: the reader's choice, the
    reference naming it, order, focus, else every matched collection is
    material and the form asks."""
    declared = {k.casefold(): v for k, v in (declared or {}).items()}
    read = readings(colls, steps)
    out: dict[str, dict] = {}
    texts = " ".join(texts_lines(body))
    for c in colls:
        role = declared.get(c["label"].casefold()) or declared.get(c["key"].casefold())
        if role in ROLES:
            out[c["key"]] = {"role": role, "why": declared_why}
        elif texts and named_in(c["label"], texts):
            out[c["key"]] = {"role": REFERENCE, "why": "named among the reference's texts"}
        elif title and named_in(c["label"], title):
            out[c["key"]] = {"role": MATERIAL, "why": "named in the reference's title"}
    open_ = [c for c in colls if c["key"] not in out and read[c["key"]]["matched"]]
    for c in colls:
        if c["key"] not in out and not read[c["key"]]["matched"]:
            out[c["key"]] = {"role": UNUSED, "why": "no row of it meets the reference"}
    if not open_:
        return out
    has_material = any(v["role"] == MATERIAL for v in out.values())
    if len(open_) == 1 and not has_material and not any(v["role"] == REFERENCE for v in out.values()):
        out[open_[0]["key"]] = {"role": MATERIAL, "why": "the only source the reference meets"}
        return out
    if not has_material:
        winner, why = _decide(open_, read)
        if winner:
            out[winner] = {"role": MATERIAL, "why": why}
            has_material = True
    for c in open_:
        if c["key"] in out:
            continue
        if has_material:
            out[c["key"]] = {"role": REFERENCE, "why": "another collection is the material"}
        else:
            out[c["key"]] = {"role": MATERIAL, "why": "could not tell the material from a reference: choose in the form"}
    return out


def _decide(open_: list[dict], read: dict[str, dict]) -> tuple[str | None, str]:
    """The material among undecided collections by order, then by focus, or
    (None, "") when neither settles it."""
    ordered = sorted(((read[c["key"]]["order"], c["key"]) for c in open_ if read[c["key"]]["order"] is not None),
                     reverse=True)
    if ordered and ordered[0][0] >= ORDER_FLOOR:
        second = ordered[1][0] if len(ordered) > 1 else -1.0
        if ordered[0][0] - second >= ORDER_MARGIN:
            other = f" against {second:.2f}" if len(ordered) > 1 else ""
            return ordered[0][1], f"follows the reference's order ({ordered[0][0]:.2f}{other})"
    focused = sorted(((read[c["key"]]["focus"], c["key"]) for c in open_), reverse=True)
    second = focused[1][0] if len(focused) > 1 else 0.0
    if focused[0][0] - second >= FOCUS_MARGIN:
        return focused[0][1], (f"the reference names {focused[0][0]:.0%} of its rows"
                               + (f", against {second:.0%}" if len(focused) > 1 else ""))
    return None, ""


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
    f = _vault() / path
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
    key = (str(_vault()), len(allowed), tuple(c["key"] for c in colls))
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
            if best[i] is None or len(prev) + 1 < len(best[i]):
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
