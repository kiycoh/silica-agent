# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What the reader's column says about one note: its opening sentences, where a link to it is
written, the notes it should link, its place in the graph and in a reading order."""
from __future__ import annotations

import logging
import re

from silica.kernel.text import note_parts

logger = logging.getLogger(__name__)


# --- context explorer (GET /context) -----------------------------------------
# One blocking call, all of it deterministic and LLM-free. Measured on a
# 718-note vault, warm: related 0.01s, concepts(note=) 0.06s, outline/links/
# unresolved 0.00s. The first call in a fresh process pays ~0.9s to load the
# co-occurrence store — once, in a long-lived server. So: no progressive fill,
# no client-side hybrid, one endpoint that returns the whole drawer.

# Below this a note reads faster whole than as an extract, so the snippets
# section is dropped rather than duplicating the reader.
_SNIPPET_MIN_BODY = 700
_SNIPPET_CUT = 140
_H2 = re.compile(r"^##\s+(.+?)\s*$", re.M)
# Lines that say nothing as a one-line extract: headings, lists, quotes,
# callouts, tables, fences, images.
_NOT_PROSE = re.compile(r"^\s*(?:[-*+>#|]|\d+[.)]\s|```|~~~|!\[|\[!)")
_SENTENCE_END = re.compile(r"(?<=[.!?])(?:\s|$)")
# List and quote markers, nested (`> - text`), for the readers that treat the
# item or the quote as the sentence rather than as block structure. What is
# left is then checked against _NOT_PASSAGE and not _NOT_PROSE: a stripped
# `- **term**: ...` starts with `*`, which _NOT_PROSE reads as a list marker.
LIST_OR_QUOTE = re.compile(r"^\s*(?:(?:[-*+]|\d+[.)])\s+|>\s?)+")
_NOT_PASSAGE = re.compile(r"^\s*(?:#|\||```|~~~|!\[|\[!)")
# A related note this far away (or unreachable) is a link that does not exist
# yet; distance 1 means it is already linked and belongs under Related instead.
_SUGGEST_MIN_DIST = 3


# A wikilink SPLIT INTO ITS PARTS, which is what the two readers below need and
# what `_WIKILINK` (line 173, the linkifier's) deliberately does not do: that one
# keeps `Target|alias` whole because it re-emits the link, while these two have
# to resolve the target and display the alias separately. Naming them apart is
# not style — the first version of this reused `_WIKILINK` and shadowed the
# linkifier, and every note in the app rendered without its links.
# `[[Target#anchor|alias]]`, and either half can be absent.
_WIKI_REF = re.compile(r"!?\[\[([^\]|#]*)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
# Only the PAIRED marks, and only the unambiguous ones. A lone `*` or `_` is
# left alone on purpose: stripping those turns snake_case into snakecase and a
# multiplication sign into nothing, which is a worse read than the emphasis
# marker it would have removed.
_MD_PAIR = re.compile(r"(\*\*|__|==|~~|`)(.+?)\1", re.S)


def plain(text: str) -> str:
    """Inline markup out, the words it wrapped left in.

    The extract is prose, not source. This ran nowhere while the snippets lived
    in the note drawer, one scroll under the reader that showed the same
    sentence properly rendered; they are the first thing the work panel says
    about a note now, and `==Un **Database** o Base di dati` at the top of a
    panel reads as a broken row rather than as emphasis. Block-level markup
    needs no handling here: _NOT_PROSE already refuses those lines.
    """
    out = _WIKI_REF.sub(lambda m: (m.group(2) or m.group(1) or "").strip(), text)
    out = _MD_LINK.sub(lambda m: m.group(1).strip(), out)
    # Pairs NEST (`==A **b** c==`) and sub() does not rescan what it inserted,
    # so one pass leaves the inner marks standing. Three passes, because three
    # is one more than the deepest nesting anyone writes by hand: highlight
    # around bold around code.
    for _ in range(3):
        stripped = _MD_PAIR.sub(lambda m: m.group(2), out)
        if stripped == out:
            break
        out = stripped
    return out


def fm_ref(value: str) -> tuple[str, str]:
    """A frontmatter `related:` entry as (what to resolve, what to show)."""
    v = str(value).strip()
    m = _WIKI_REF.fullmatch(v)
    if not m:
        return v, note_parts.clean_name(v)
    target = (m.group(1) or "").strip()
    return target, (m.group(2) or "").strip() or note_parts.clean_name(target)


def _lead_prose(chunk: str) -> str:
    """The first run of plain prose in a chunk, as one line."""
    run: list[str] = []
    for line in chunk.splitlines():
        if not line.strip() or _NOT_PROSE.match(line):
            if run:
                break
            continue
        run.append(line.strip())
    return " ".join(run)


def first_sentence(text: str, limit: int = _SNIPPET_CUT) -> str:
    """First sentence, trimmed to `limit` on a word boundary.
    ponytail: regex sentence split, so `e.g.` cuts early — a snippet, not a quote."""
    text = " ".join(text.split())
    if not text:
        return ""
    m = _SENTENCE_END.search(text)
    out = text[: m.start()] if m else text
    if len(out) > limit:
        out = out[:limit].rsplit(" ", 1)[0] + "…"
    return out


def key_snippets(body: str) -> list[dict]:
    """First sentence of the body plus the first sentence of each `##` section,
    at most three — a probe into the note, not a second reader."""
    if len(body) < _SNIPPET_MIN_BODY:
        return []
    parts: list[tuple[str, str]] = []
    head, last = "", 0
    for m in _H2.finditer(body):
        parts.append((head, body[last:m.start()]))
        head, last = m.group(1), m.end()
    parts.append((head, body[last:]))

    out: list[dict] = []
    for heading, chunk in parts:
        # _plain BEFORE _first_sentence, not after: the cut lands mid-sentence
        # by design, so a `==highlight==` whose closing mark falls past the
        # limit would otherwise keep its opening one and nothing to close it.
        text = first_sentence(plain(_lead_prose(chunk)))
        if text:
            out.append({"heading": plain(heading), "text": text})
        if len(out) == 3:
            break
    return out


def row(path: str) -> dict:
    return {"name": note_parts.clean_name(path), "path": path}


def bridge_passage(src: str, target: str, limit: int = 240) -> str:
    """The sentence in `src` that carries its wikilink to `target`, as prose.

    A list item or a quote counts as prose here, marker stripped: on the test
    vault 31 of 55 prose bridges lived in a `- **term**: [[link]] ...` bullet
    or a `> ...` line, which IS the sentence. Headings, tables, fences,
    images and callouts stay out. Empty when nothing carries it. The cut is
    twice a snippet's: a bridge sentence is the evidence the row exists for,
    so it may run a clause longer.
    """
    from silica.driver import get_driver
    from silica.driver.base import NoteRef

    try:
        content = get_driver().read_note(NoteRef(name=note_parts.clean_name(src), path=src)).content
    except Exception:
        logger.debug("shape: bridge source unreadable %s", src, exc_info=True)
        return ""
    want = note_parts.clean_name(target).lower()
    _props, body = note_parts.split_frontmatter(content)
    for raw in body.splitlines():
        line = LIST_OR_QUOTE.sub("", raw)
        if not line.strip() or _NOT_PASSAGE.match(line):
            continue
        for m in _WIKI_REF.finditer(line):
            if note_parts.clean_name((m.group(1) or "").strip()).lower() != want:
                continue
            for sentence in _SENTENCE_END.split(line):
                if m.group(0) in sentence:
                    out = " ".join(plain(sentence).split())
                    if len(out) > limit:
                        out = out[:limit].rsplit(" ", 1)[0] + "…"
                    return out
    return ""


def link_line(src: str, target: str, limit: int = 240) -> tuple[int, str]:
    """The line of `src` a wikilink to `target` is written on, and its words.

    Counted in the whole file, frontmatter included, because the drawer opens
    at that number. Not _bridge_passage's filter: a link in a table or under a
    heading is still written somewhere, and on Cultura (2026-11-05) any line
    holding the link found 1,248 of 1,263. (0, "") when no line names it: a
    link through an alias of the note resolves and spells another name.
    """
    from silica.driver import get_driver
    from silica.driver.base import NoteRef

    try:
        content = get_driver().read_note(NoteRef(name=note_parts.clean_name(src), path=src)).content
    except Exception:
        logger.debug("link: source unreadable %s", src, exc_info=True)
        return 0, ""
    want = note_parts.clean_name(target).lower()
    lines = content.splitlines()
    # The body before the frontmatter: a sentence says why the link is there,
    # and a `related:` entry only that it is.
    head = len(lines) - len(note_parts.split_frontmatter(content)[1].splitlines())
    for number in [*range(head, len(lines)), *range(head)]:
        raw = lines[number]
        for m in _WIKI_REF.finditer(raw):
            if note_parts.clean_name((m.group(1) or "").strip()).lower() != want:
                continue
            line = LIST_OR_QUOTE.sub("", raw)
            said = next((s for s in _SENTENCE_END.split(line) if m.group(0) in s), line)
            out = " ".join(plain(said).split()).strip("'\"")
            if len(out) > limit:
                out = out[:limit].rsplit(" ", 1)[0] + "…"
            return number + 1, out
    return 0, ""


def unresolved_links() -> list:
    from silica.driver import get_driver

    try:
        return get_driver().unresolved()
    except Exception:
        logger.debug("context: unresolved() failed", exc_info=True)
        return []


def suggested(canon: str, related: list[dict], linked: set[str], resolve) -> list[dict]:
    """How this note SHOULD be connected, in two flavours.

    - ghost: a wikilink leaving this note whose target does not exist. The note
      already claims the connection; only the file is missing.
    - note: a computed relative that scores high and sits far away (or
      unreachable) in the wikilink graph — "a missing link worth creating", per
      silica_related's own docstring. distance 1 is already linked, so it is
      Related's business, not this section's.

    Structural GAPs stay out on purpose: they are hub-to-hub by construction, so
    the section would be empty on every note that is not a hub.
    """
    out: list[dict] = [
        {"name": note_parts.clean_name(link.target), "path": "", "kind": "ghost",
         "why": "linked from here, never written"}
        for link in unresolved_links()
        if link.source.path == canon
    ]
    for r in related:
        dist = r.get("distance")
        # The recall stores key on cooccur_key (path minus .md), the wikilink
        # graph on the full path — resolve back, or `linked` never matches and
        # the click target is a path the drawer cannot open.
        rpath = resolve(r["path"]) or r["path"]
        if rpath in linked or rpath == canon or (dist is not None and dist < _SUGGEST_MIN_DIST):
            continue
        out.append({
            "name": r.get("name") or note_parts.clean_name(rpath), "path": rpath, "kind": "note",
            "why": ("unreachable" if dist is None else f"{dist} hops away")
                   + f" · score {r.get('score', 0):.2f}",
            # The same score the sentence above states in words. The explore
            # panel draws it as a meter, and parsing it back out of `why` is how
            # a display string becomes an accidental API: reword the sentence
            # and the bar goes blank.
            "score": float(r.get("score", 0) or 0),
        })
        if len(out) >= 8:
            break
    return out


def note_structure(canon: str) -> dict:
    """The seven variables for one note, named for a reader rather than a paper.

    Rows, not scalars: the panel prints what each number MEANS, so the naming
    happens here where the thresholds can be justified, not in JS where they
    would be three magic numbers in a template. A note the graph has never seen
    (written this second, index not rebuilt) returns {} and the panel omits the
    section rather than printing six zeroes.
    """
    from silica.kernel.report.structure import note_structure

    try:
        st = note_structure(canon)
    except Exception:
        logger.debug("context: structure failed for %s", canon, exc_info=True)
        return {}
    if not st or not st.get("in_graph"):
        return {}
    out = {
        "coreness": st["coreness"],
        # The scales the labels carry (ADR-0075): depth k OF the vault's
        # deepest, and how many of the vault's notes are that deep.
        "core_max": st.get("core_max", 0),
        "core_reach": st.get("core_reach", 0),
        "core_hist": st.get("core_hist", []),
        "notes": st.get("notes", 0),
        "articulation": st["articulation"],
        "strands": st["strands"],
        # Rounded to whole percent HERE: the raw pct-rank difference carries
        # four decimals of sampling noise from a betweenness taken at 400
        # pivots, and printing them would claim a precision the estimate has not
        # got.
        "surprise": round(st["surprise"] * 100),
        # The two ranks the difference is taken of, which is what the drawer
        # shows: "+25" asks the reader to know what was subtracted from what.
        "degree_pct": round(st.get("degree_rank", 0.0) * 100),
        "betweenness_pct": round(st.get("betweenness_rank", 0.0) * 100),
        # prerequisites/unlocks are NOT here: they are the +-1 rungs of the
        # ladder below, and the panel draws that instead. Two spellings of one
        # relation in one payload is the drift this file's tests exist to stop.
    }
    stranded = st.get("stranded") or []
    if st["articulation"] and stranded:
        out["component"] = st.get("component", 0)
        # Every stranded note, piece by piece, best-linked first: the drawer
        # names them, draws the pieces, and tags a neighbour that is one of
        # them `behind`. A piece is named by its head, so it ships with it.
        out["behind"] = [q for p in stranded for q in p]
        out["pieces"] = [{"size": len(p), "head": row(p[0])} for p in stranded]
        if st.get("layout"):
            out["cut"] = _cut_figure(st["layout"])
    return out


def _cut_figure(layout: dict) -> dict:
    """The cut's neighbourhood dressed for the drawer: names, and each area's
    hue in both themes, the frame's own (ADR-0075, Place)."""
    from silica.kernel.graph.graph_export import area_color

    nodes = [{**{k: v for k, v in n.items() if k != "id"}, **row(n["id"])} for n in layout["nodes"]]
    areas = sorted({n["area"] for n in nodes if n["area"] >= 0})
    return {"nodes": nodes, "edges": layout["edges"],
            "colors": {a: [area_color(a), area_color(a, on_paper=True)] for a in areas}}


def note_ladder(canon: str, *, near: int = 2, per_rung: int = 3) -> dict:
    """Where this note sits in a reading order, not just who touches it.

    The two flat lists this replaced could not say the one thing the DAG knows:
    that a prerequisite has a prerequisite. `ladder` walks three hops each way,
    and the drawer draws the near rungs at `near`; everything past that is a
    count and a hand-off to Flow, which is the one place in the app
    that draws a whole ladder. Same reason `per_rung` exists: a 310px column
    that prints all 21 dependents of a hub buries every section under it.

    Empty dict, never a rung of one: a note with no direction around it has no
    reading order, and a section holding only the note you are already on is a
    heading with nothing under it.
    """
    from silica.kernel.report.structure import ladder as build_ladder

    try:
        lad = build_ladder(canon)
    except Exception:
        logger.debug("context: ladder failed for %s", canon, exc_info=True)
        return {}
    if not lad["nodes"]:
        return {}
    by_depth: dict[int, list[str]] = {}
    for n in lad["nodes"]:
        by_depth.setdefault(n["depth"], []).append(n["path"])
    rungs, further = [], 0
    for d in sorted(by_depth):
        paths = sorted(by_depth[d], key=note_parts.clean_name)
        # Past the near rungs the drawer says how many rather than which: the
        # names are Flow's job and it has the width for them.
        if abs(d) > near:
            further += len(paths)
            continue
        rungs.append({
            "depth": d,
            "notes": [row(q) for q in paths[:per_rung]],
            "hidden": max(0, len(paths) - per_rung),
        })
    return {"rungs": rungs, "further": further, "root": lad["root"],
            "cycles": lad["cycles"], "truncated": lad["truncated"]}
