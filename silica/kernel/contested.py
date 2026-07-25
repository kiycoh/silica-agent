# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Contested-claims layer (spec-hermes-coherence §1).

A contradiction is neither a duplicate nor a new concept: it is recorded on
the existing note (frontmatter flag + warning callout) and kept visible until
a human resolves it. Pure functions over note text — no I/O, no LLM.

Also home to the claim stamp (spec-contested-bitemporal §3): the per-claim
event clock. It rides in an HTML comment rather than frontmatter because
frontmatter is per-note while a note accumulates claims from many sources on
different dates; the comment is invisible in preview, greppable, and survives
every write path byte-for-byte (no YAML round-trip).
"""
from __future__ import annotations

import re

from silica.kernel import frontmatter

CONTESTED_KEY = "contested"
CONTRADICTIONS_KEY = "contradictions"
_UNRESOLVED_TAIL = "Unresolved."

STAMP_RE = re.compile(r"<!--\s*silica:\s*(.*?)\s*-->")
# Values are dates and hex run ids. Anything outside this class is dropped so a
# value can never close the comment early or break the key=value split.
_STAMP_VALUE_RE = re.compile(r"[^\w.:+-]")


def stamp(**fields: str) -> str:
    """A claim stamp: `<!-- silica: valid_from=2023-05-08 run=b07f1268 -->`.

    Key order is the caller's, so the rendered line is deterministic. Fields
    with an empty value are dropped; all-empty yields "" so a caller can splice
    unconditionally.
    """
    parts = []
    for k, v in fields.items():
        if v is None:  # str(None) is "None", which is truthy — never emit it
            continue
        cleaned = _STAMP_VALUE_RE.sub("", str(v).strip())
        if cleaned:
            parts.append(f"{k}={cleaned}")
    return f"<!-- silica: {' '.join(parts)} -->" if parts else ""


def parse_stamp(text: str) -> dict[str, str]:
    """Fields of the first claim stamp in `text`; {} when there is none."""
    m = STAMP_RE.search(text or "")
    if not m:
        return {}
    out: dict[str, str] = {}
    for tok in m.group(1).split():
        k, _, v = tok.partition("=")
        if k and v:
            out[k] = v
    return out


def mark_contested(content: str, source_ref: str) -> str:
    """Set `contested: true` and append `source_ref` to `contradictions:`.

    Idempotent on source_ref. A note without frontmatter gains a minimal one;
    a note with unparseable YAML is returned unchanged (never destroy what we
    cannot round-trip).
    """
    data, raw, body = frontmatter.split(content)
    if data is None:
        if raw is not None:  # frontmatter present but broken YAML
            return content
        data, body = {}, content
    refs = list(data.get(CONTRADICTIONS_KEY) or [])
    if source_ref in refs:
        return content
    data[CONTESTED_KEY] = True
    data[CONTRADICTIONS_KEY] = refs + [source_ref]
    return frontmatter.dump(data, body)


def clear_contested(content: str) -> str:
    """Remove the `contested`/`contradictions` flag from a note's frontmatter.

    No-op when the note is not contested; a note with unparseable YAML is
    returned unchanged (mirror of `mark_contested`).
    """
    data, raw, body = frontmatter.split(content)
    if data is None or not data.get(CONTESTED_KEY):
        return content
    data.pop(CONTESTED_KEY, None)
    data.pop(CONTRADICTIONS_KEY, None)
    return frontmatter.dump(data, body)


def contested_refs(content: str) -> list[str]:
    """The note's `contradictions:` entries; [] when not contested."""
    data, _, _ = frontmatter.split(content)
    if not data or not data.get(CONTESTED_KEY):
        return []
    return list(data.get(CONTRADICTIONS_KEY) or [])


def contested_callout(claim: str, source_basename: str) -> str:
    """The warning callout recording a conflicting claim, with provenance."""
    quoted = "\n".join(f"> {line}".rstrip() for line in claim.strip().splitlines())
    return (
        f"> [!warning] Contradiction — from {source_basename}\n"
        f"{quoted}\n"
        f">\n"
        f"> Conflicts with this note. {_UNRESOLVED_TAIL}"
    )


# ---------------------------------------------------------------------------
# Superseded section (spec-contested-bitemporal §4)
#
# A claim that loses a contest is never deleted: it moves to the end of the
# note under `## Superseded`, keeping its own provenance and gaining a
# valid_to stamp. The section is ALWAYS the last one in the body, which is
# what append_before_superseded exists to guarantee — every EOF appender in
# the codebase routes through it.
# ---------------------------------------------------------------------------

SUPERSEDED_HEADING = "## Superseded"
_SUPERSEDED_RE = re.compile(r"^## Superseded\s*$", re.MULTILINE)
_CONTRADICTION_START_RE = re.compile(r"^> \[!warning\] Contradiction\b")


def append_before_superseded(content: str, block: str) -> str:
    """`content.rstrip() + "\\n" + block`, but above `## Superseded` if present.

    Without the section this is byte-identical to the plain EOF append it
    replaces. With it, the block lands above, so live content never ends up
    filed under the note's graveyard.
    """
    m = _SUPERSEDED_RE.search(content)
    if not m:
        return content.rstrip() + "\n" + block
    head, tail = content[: m.start()], content[m.start():]
    return head.rstrip() + "\n" + block.rstrip() + "\n\n" + tail


def _split_at_superseded(body: str) -> tuple[str, str]:
    """(live body, superseded section incl. heading). Tail is "" when absent."""
    m = _SUPERSEDED_RE.search(body)
    if not m:
        return body, ""
    return body[: m.start()], body[m.start():]


def _extract_contradiction_callouts(body: str) -> tuple[str, list[str]]:
    """Lift every contradiction callout out of `body`.

    A callout is the run of contiguous `>`-prefixed lines opened by the
    warning marker (its internal blank lines are `>`-prefixed too, so the run
    is unbroken). A `## Note aggiuntive` header left with nothing but
    whitespace under it is dropped with its callout: the header exists only to
    attribute the block that just moved.
    """
    from silica.kernel.templates import PROVENANCE_HEADER_PREFIX

    lines = body.splitlines()
    kept: list[str] = []
    callouts: list[str] = []
    i = 0
    while i < len(lines):
        if _CONTRADICTION_START_RE.match(lines[i]):
            j = i
            while j < len(lines) and lines[j].startswith(">"):
                j += 1
            callouts.append("\n".join(lines[i:j]))
            i = j
            continue
        kept.append(lines[i])
        i += 1

    if callouts:
        pruned: list[str] = []
        for idx, line in enumerate(kept):
            if line.startswith(PROVENANCE_HEADER_PREFIX):
                rest = kept[idx + 1:]
                nxt = next((r for r in rest if r.strip()), "")
                if not nxt or nxt.startswith("## "):
                    continue  # header whose only content was the moved callout
            pruned.append(line)
        kept = pruned

    return "\n".join(kept).rstrip() + "\n", callouts


# ---------------------------------------------------------------------------
# Reliability tiers (spec-contested-bitemporal §5)
#
# Ordinal, not a posterior: the three signals available are coarse, and a
# calibrated number over three levels would be false precision. Every tier is
# derived from the note text alone, so both sides of a comparison are always
# ranked on the same information (an asymmetric lookup would turn "we know
# more about A" into "A wins", which is not the same claim).
# ---------------------------------------------------------------------------

SUPERSEDED_BY_KEY = "superseded_by"

TIER_HUMAN = 3
TIER_GROUNDED = 2
TIER_DISTILLED = 1


def reliability_tier(content: str, *, has_source_leaf: bool | None = None) -> int:
    """How much weight a claim's origin earns it: 3 human, 2 grounded, 1 distilled.

    Human means the agent never claimed authorship (`AI` absent or false, or no
    frontmatter at all: every agent write stamps the flag). Grounded means an
    agent note whose verbatim source is still reachable through its `## Sources`
    link. Distilled is everything else.

    Unparseable frontmatter ranks lowest on purpose: a parse accident must never
    win a contest. `has_source_leaf` overrides the note-side signal for a claim
    that is not a note yet (an incoming excerpt has no `## Sources` block).

    ponytail: the human tier decays. ensure_ai_flag stamps `AI: true` on a legacy
    user note the first time the agent patches it, so a human note the agent has
    touched reads as agent-authored. Upgrade path if it ever matters: a distinct
    `AI: partial` on that first patch.
    """
    data, raw, _body = frontmatter.split(content or "")
    if data is None:
        if raw is not None:  # frontmatter present but broken YAML
            return TIER_DISTILLED
        return TIER_HUMAN  # no frontmatter at all: the agent always stamps one
    if not data.get("AI"):
        return TIER_HUMAN
    if has_source_leaf is None:
        from silica.kernel.paths import SOURCES_MARKER
        has_source_leaf = SOURCES_MARKER in (content or "")
    return TIER_GROUNDED if has_source_leaf else TIER_DISTILLED


def merge_rank(content: str) -> tuple[int, int]:
    """Sort key for picking the target of a duplicate merge. Higher wins.

    Replaces the bare `len(body)` heuristic, which systematically handed the
    merge to the verbose agent note over the terse hand-written one. Length
    still breaks a tie within a tier, where there is no reliability signal to
    prefer either side.
    """
    return (reliability_tier(content), len(content))


def mark_superseded_by(content: str, winner: str) -> str:
    """Point a merged-away note at the note that absorbed it.

    The merge loser used to be left on disk with overlapping content and no
    link to the winner: two notes saying the same thing and no record that one
    replaced the other. Idempotent; unparseable YAML is returned unchanged.
    """
    data, raw, body = frontmatter.split(content)
    if data is None:
        if raw is not None:
            return content
        data, body = {}, content
    link = f"[[{winner.removesuffix('.md').rsplit('/', 1)[-1]}]]"
    if data.get(SUPERSEDED_BY_KEY) == link:
        return content
    data[SUPERSEDED_BY_KEY] = link
    return frontmatter.dump(data, body)


def resolve_contested(content: str, *, resolved_by: str, valid_to: str) -> str:
    """Resolve a note's contradictions without erasing the record.

    Every open contradiction callout moves under `## Superseded`, stamped with
    `valid_to` and its "Unresolved." tail rewritten, before the frontmatter
    flags are dropped. Callouts already filed under `## Superseded` are left
    alone, so re-running is a no-op.

    This is what `clear_contested` should have been: clearing the flag while
    leaving a body callout that still reads "Unresolved" makes the note lie
    about its own state, and drops every record of what was contested.
    """
    from silica.kernel.moc import merge_moc_section

    data, raw, body = frontmatter.split(content)
    if data is None:
        if raw is not None:  # frontmatter present but broken YAML
            return content
        data, body = {}, content
    if not data.get(CONTESTED_KEY):
        return content

    live, tail = _split_at_superseded(body)
    kept, callouts = _extract_contradiction_callouts(live)

    new_body = kept + tail
    if callouts:
        block: list[str] = []
        for callout in callouts:
            block.append(stamp(valid_to=valid_to, resolved_by=resolved_by))
            block.append(callout.replace(_UNRESOLVED_TAIL, f"Resolved {valid_to}."))
            block.append("")
        new_body = merge_moc_section(new_body, SUPERSEDED_HEADING, block)

    data.pop(CONTESTED_KEY, None)
    data.pop(CONTRADICTIONS_KEY, None)
    return frontmatter.dump(data, new_body)
