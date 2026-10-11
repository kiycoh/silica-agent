# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The composers behind the driver's writes, replayed on a note's text and
judged by `preserve.lost` under the `Loss` their call site declares.

Two consumers, one list: `tests/test_preserve.py` runs them on every commit
over synthetic fixtures (ADR-0093 decision 3), and the golden probe's T5 runs
them on a real vault (decision 10). No driver and no model: each composer is
the pure function chain its call site runs, fed fixed probe arguments, so a
composer that starts dropping a line or a value fails CI before it reaches a
note.

The chains mirror the sites they name; when a site's chain changes, its
composer here changes with it, or the gate measures code nobody runs.
"""
from __future__ import annotations

from collections.abc import Callable

from silica.kernel.link.autolink import autolink
from silica.kernel.link.sweep import _prune_related, _unlink_body
from silica.kernel.write import frontmatter, templates
from silica.kernel.write.contested import (
    append_before_superseded,
    mark_contested,
    mark_superseded_by,
)
from silica.kernel.write.moc import merge_moc_section
from silica.kernel.write.notetype import stamp_type
from silica.kernel.write.preserve import Loss, lost
from silica.kernel.write.provenance import attribute_lines

SRC = "Probe source.md"
HUB = "Probe hub"
PROSE = "Probe lead sentence with $x^2$ inline.\n\n## Probe facet\n\nProbe facet text."
RELATION = "- [[Probe A]] applies this (Probe spine): because"


def _floor(rel: str, prior: str, content: str) -> str:
    """_execute_overwrite's tail: the system floor, then overwrite()'s stamp."""
    return stamp_type(rel, templates.ensure_system_floor(content, prior=prior))


def _patch(rel: str, text: str, stem: str, snippet: str, relation: bool) -> str:
    new = templates.patch_snippet(heading=stem, snippet=snippet, source_basename=SRC,
                                  hub=HUB, existing_content=text, relation=relation)
    new = templates.ensure_ai_flag(new, value="partial")
    new = templates.stamp_sources(new, SRC)
    new = templates.stamp_citation(new, {"doi": "10.0000/probe"})
    return stamp_type(rel, new)


def patch_prose(rel, text, ctx):  # bulk._execute_patch
    return _patch(rel, text, ctx["stem"], PROSE, relation=False)


def patch_relation(rel, text, ctx):  # bulk._execute_patch, relation=True
    return _patch(rel, text, ctx["stem"], RELATION, relation=True)


def patch_dup_repair(rel, text, ctx):  # bulk._execute_patch, duplicate repair
    return stamp_type(rel, templates.ensure_ai_flag(templates.ensure_hub_link(text, HUB),
                                                    value="partial"))


def overwrite_keep_fm(rel, text, ctx):  # bulk._execute_overwrite, the model kept the block
    return _floor(rel, text, text.rstrip("\n") + "\n\nProbe appended paragraph.\n")


def overwrite_body_only(rel, text, ctx):  # bulk._execute_overwrite, the model dropped it
    _d, _r, body = frontmatter.split(text)
    return _floor(rel, text, body.rstrip("\n") + "\n\nProbe appended paragraph.\n")


def flag(rel, text, ctx):  # tools/notes.silica_flag_note, set
    return stamp_type(rel, mark_contested(text, "flagged: probe (by probe, 2026-10-11)"))


def superseded_by(rel, text, ctx):  # dedup merge loser, through _execute_overwrite
    return _floor(rel, text, mark_superseded_by(text, "Probe winner"))


def alias(rel, text, ctx):  # dedup absorbed alias, through _execute_overwrite
    return _floor(rel, text, frontmatter.add_alias(text, "Probe alias"))


def settle(rel, text, ctx):  # dedup settle of a landed distinct note
    data, raw, body = frontmatter.split(text)
    if data is None:
        if raw is not None:
            return text
        data, body = {}, text
    data.pop("review", None)
    body = body.rstrip("\n") + "\n\n" + templates.related_unjudged("Probe A") + "\n"
    return _floor(rel, text, frontmatter.dump(data, body))


def documents(rel, text, ctx):  # tools/notes.silica_patch_note, the documents: stamp
    return stamp_type(rel, templates.stamp_documents(text, ["src/probe.py"], "abc1234"))


def autolink_note(rel, text, ctx):  # the fs backend's autolink_note, then overwrite()
    low = text.casefold()
    cands = [t for t in ctx["titles"] if t.casefold() in low]
    new, added = autolink(text, ctx["titles"], candidates=cands, self_title=ctx["stem"])
    return stamp_type(rel, new) if added else text


def footnote_def(rel, text, ctx):  # finalize CLEANUP: the patch's footnote definition
    return stamp_type(rel, append_before_superseded(text, "\n[^Probe-source]: Probe source\n"))


def sources_block(rel, text, ctx):  # finalize _write_source_leaf, no attribution
    return stamp_type(rel, templates.append_source(text, "[^Probe-source]: [[sources/Probe source]]"))


def attribute(rel, text, ctx):  # finalize _write_source_leaf: every verbatim line gains [^label]
    _d, _r, body = frontmatter.split(text)
    return stamp_type(rel, attribute_lines(text, body, "Probe-source"))


def moc(rel, text, ctx):  # router write: hub and parent MOCs; tools/pipeline
    return stamp_type(rel, merge_moc_section(text, "## From: Probe source", ["- [[Probe A]] - probe"]))


def review_keep(rel, text, ctx):  # the GUI's keep after a soft-gate write
    return templates.drop_props(templates.upsert_props(text, {"review": "probe"}), ("review",))


def fm_roundtrip(rel, text, ctx):  # frontmatter.dump: contested, add_alias, Learn's keys
    data, _raw, body = frontmatter.split(text)
    if not isinstance(data, dict):
        return text
    return frontmatter.dump(data, body)


def sweep_body(rel, text, ctx):  # the sweep's body half, nothing resolving
    m = frontmatter.FM_RE.match(text)
    head, body = (text[:m.end()], text[m.end():]) if m else ("", text)
    new, _s, _r = _unlink_body(body, lambda t: "")
    return head + new


def sweep_full(rel, text, ctx):  # sweep.sweep_note: unlink the body, prune related:
    m = frontmatter.FM_RE.match(text)
    if not m:
        return sweep_body(rel, text, ctx)
    fm_new, _d = _prune_related(m.group(0), lambda t: "")
    body, _s, _r = _unlink_body(text[m.end():], lambda t: "")
    return fm_new + body


Composer = Callable[[str, str, dict], str]

# name -> (composer, the Loss its call site declares). Nineteen: every chain
# behind a driver write that rewrites an existing note's text.
COMPOSERS: dict[str, tuple[Composer, Loss | None]] = {
    "patch_prose": (patch_prose, None),
    "patch_relation": (patch_relation, None),
    "patch_dup_repair": (patch_dup_repair, None),
    "overwrite_keep_fm": (overwrite_keep_fm, None),
    "overwrite_body_only": (overwrite_body_only, None),
    "flag": (flag, None),
    "superseded_by": (superseded_by, None),
    "alias": (alias, None),
    "settle": (settle, None),
    "documents": (documents, None),
    "autolink": (autolink_note, None),
    "footnote_def": (footnote_def, None),
    "sources_block": (sources_block, None),
    "attribute": (attribute, Loss.CITE),
    "moc": (moc, None),
    "review_keep": (review_keep, None),
    "fm_roundtrip": (fm_roundtrip, None),
    "sweep_body": (sweep_body, None),
    "sweep_full": (sweep_full, Loss.UNLINK),
}


def preservation(rel: str, text: str, stem: str, titles: list[str]) -> dict[str, str]:
    """T5: composer name -> what it lost, for every composer that lost
    something on this note. Empty when the note is clean. A composer that
    raises lands nothing, so it loses nothing; T1 to T4 see its crash."""
    ctx = {"stem": stem, "titles": titles}
    out: dict[str, str] = {}
    for name, (fn, loss) in COMPOSERS.items():
        try:
            after = fn(rel, text, ctx)
        except Exception:  # noqa: BLE001 - a raise writes nothing (see docstring)
            continue
        if after != text and (why := lost(text, after, loss=loss)):
            out[name] = why
    return out
