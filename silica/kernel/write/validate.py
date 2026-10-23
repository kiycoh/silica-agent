# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

import os
import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from pydantic import BaseModel
from silica.driver import DRIVER
from silica.kernel.write.ops import Op, OpType
from silica.kernel.write.templates import slugify
from silica.kernel.vault_manifest import within
from silica.onboarding.adopt import SAFE_WRITE_DIR
from silica.kernel.link.ast import extract_links

logger = logging.getLogger(__name__)


def same_file_key(path: str) -> str:
    """Identity key for "these two refs are the same file on disk".

    `os.path.abspath` alone is case- and composition-sensitive, but macOS and
    Windows are not: `Percettrone.md` and `percettrone.md` are ONE file there,
    so two ops surviving the dedup commit in sequence and the second silently
    overwrites the first — both reporting ok. NFC folds the other half of the
    same failure (macOS stores decomposed filenames).

    ponytail: over-merges on a case-sensitive filesystem where two notes really
    do differ only by case. That is a vault the rest of the pipeline already
    treats as one note (title_key casefolds, the linter matches case-insensitively),
    so collapsing is the consistent answer, not a regression.
    """
    return unicodedata.normalize("NFC", os.path.abspath(path)).casefold()


# Precision gate: a write op whose snippet is shorter than this is deferred
# instead of written — execute_write would otherwise fill the note with a
# "(to be expanded)" placeholder (real incident: run 5d0a3350, 2026-07-04, the
# distiller returned whole chunks with snippet="" despite full inbox excerpts).
# Rejection routes through the existing defer + steer path, so the distiller
# gets re-prompted with the reason.
#
# Raised 100→400 (audit 2026-07-23): a 130-220 char body is a meta-summary of
# the source section, not the section — the run's note-body length was bimodal
# (thin ≤220 vs rich ≥1300), and 400 routes the thin band to expand.
# Lowered 400→275 (2026-07-27) to match the value actually run against real
# vaults: 400 also deferred short-but-complete notes, which cost a round trip.
# ponytail: length is a proxy for "content, not a description of it" — a true
# shape check needs semantic judgment, not a regex; left as the worker-model
# lever. Build it only with an eval that catches what the floor misses (a
# description of content that still clears the char floor).
MIN_WRITE_SNIPPET_CHARS = 275


# Extractive bodies are verbatim-selected spans, grounded by construction; a
# short durable fact is real content, not the prose-placeholder the default
# floor guards against.
EXTRACTIVE_MIN_SNIPPET_CHARS = 40

# Profiles whose bodies are SELECTED verbatim, not authored: extractivity is
# enforced and the lower floor applies. "promotion" copies lines from an
# episodic chain render; same contract, different lens.
EXTRACTIVE_PROFILES = frozenset({"extractive", "promotion"})


def _content_only(snippet: str, *, claim_first: bool) -> str:
    """The part of a body that carries facts: headings are structure, and on
    the outline lane the leading blockquote is the idea's one-line claim,
    already carried by the spine note. Counted as body, a claim of 120-219
    chars cleared the hard gate (==0) and five fact-free notes landed
    (run 7ad24860, 2026-09-06)."""
    text = snippet.strip()
    if claim_first and text.startswith(">"):
        head, _sep, rest = text.partition("\n\n")
        if all(ln.startswith(">") for ln in head.splitlines()):
            text = rest
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#")).strip()


def min_write_snippet_chars(profile: str | None = None) -> int:
    """Effective write-body floor for the run's profile, read at call time.
    Single source so the expand recovery uses the SAME floor the gate enforces
    — they used to diverge (expand cleared bodies at 100 while the gate could
    want more, so expanded notes stayed thin, audit finding 1).

    Two independent levers, one per body kind: SILICA_MIN_WRITE_SNIPPET_CHARS
    pins the AUTHORING floor (default profile), SILICA_EXTRACTIVE_MIN_SNIPPET_CHARS
    the verbatim-selection one. Measured live: a single shared env pin, set for
    authoring, reached a promotion run and rejected its verbatim body at
    '0 < 275'."""
    if profile in EXTRACTIVE_PROFILES:
        return int(os.getenv("SILICA_EXTRACTIVE_MIN_SNIPPET_CHARS",
                             str(EXTRACTIVE_MIN_SNIPPET_CHARS)))
    return int(os.getenv("SILICA_MIN_WRITE_SNIPPET_CHARS",
                         str(MIN_WRITE_SNIPPET_CHARS)))


# --- meta-description shape gate (audit 2026-07-23, finding 1) ---------------
# A small worker model writes bodies that ANNOUNCE the source section instead of
# delivering it: "Task: classificazione supervisionata... Include definizione
# formale e esempio di applicazione pratica." Length alone can't catch a 400+
# char announcement, so this detects announcement *shape*: content-noun
# announcements ("include definizione", "provides an overview"), deixis to the
# source artifact ("questa sezione", "la lezione"), or patch-style openers
# ("Estende la sezione su..."). Rejection routes through defer/steer/expand,
# which re-prompts with this reason — a false positive costs one retry, not the
# note. ponytail: lexical heuristic, IT+EN only; a semantic judge is the upgrade
# path if new marker families appear.

# Announcement verb followed (same clause) by an abstract content-noun.
_META_ANNOUNCE_RE = re.compile(
    r"\b(?:include|contiene|presenta|descrive|copre|tratta|riassume|illustra|fornisce"
    r"|includes?|contains?|presents?|describes?|covers?|summarizes?|outlines?|provides?)\b"
    r"[^.\n]{0,60}?"
    r"\b(?:definizion\w*|esemp\w*|panoramic\w*|descrizion\w*|spiegazion\w*|introduzion\w*"
    r"|dettagl\w*|informazion\w*|overview|definitions?|examples?|details?"
    r"|explanations?|introductions?)\b",
    re.IGNORECASE,
)
# Deixis to the source artifact itself (demonstratives, or the unambiguous
# section words — "il documento" is left out: too common as subject matter).
_META_DEICTIC_RE = re.compile(
    r"\b(?:questa|questo)\s+(?:sezione|nota|documento|capitolo)\b"
    r"|\b(?:la\s+sezione|il\s+capitolo|la\s+lezione)\b"
    r"|\bthis\s+(?:section|note|document|chapter)\b|\bthe\s+section\b",
    re.IGNORECASE,
)
_META_VERB_RE = re.compile(
    r"\b(?:include|contiene|presenta|descrive|copre|tratta|riassume|illustra|spiega"
    r"|covers?|describes?|contains?|includes?|presents?|summarizes?|outlines?|explains?)\b",
    re.IGNORECASE,
)
# Patch-style opener on a WRITE body: extends/adds something instead of being it.
_META_OPENER_RE = re.compile(
    r"^(?:estende|aggiunge|aggiunto|integra|amplia|extends?|adds?|added|updates?)\b",
    re.IGNORECASE,
)


def _has_content_evidence(body: str) -> bool:
    """Signals the body delivers material rather than announcing it: code,
    math, a real list, or multi-paragraph length."""
    if "```" in body or "$" in body:
        return True
    if sum(1 for l in body.splitlines() if l.lstrip().startswith(("-", "*", "+"))) >= 3:
        return True
    paragraphs = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    return len(paragraphs) >= 3 or len(body) >= 800


def meta_description_reason(body: str) -> str | None:
    """Reason string when a write body reads as a meta-description of its
    source section instead of the section's content; None when it looks fine."""
    b = (body or "").strip()
    if not b or _has_content_evidence(b):
        return None
    markers = []
    if _META_ANNOUNCE_RE.search(b):
        markers.append("content announcement")
    if _META_OPENER_RE.match(b):
        markers.append("patch-style opener")
    if not markers and _META_DEICTIC_RE.search(b) and _META_VERB_RE.search(b):
        markers.append("source-artifact deixis")
    if not markers:
        return None
    return (
        f"body reads as a meta-description of the source ({', '.join(markers)}) "
        f"— write the section's actual content, not a summary of what it contains"
    )


class Rejection(BaseModel):
    op: Op
    reason: str


def _flag(op: Op, reason: str) -> None:
    """Soft verdict: the op keeps going, `review` says why a reader should look.

    Two soft gates can fire on one op; both stay readable in the one key
    (`"; "`-joined) so the note names everything that was suspicious about it.
    """
    op.review = f"{op.review}; {reason}" if op.review else reason


@dataclass
class PayloadIndex:
    """What the chunk's payloads say, keyed the way the ops name things.

    `(source_basename, heading)` is the join between an op and its concept:
    an op names its source by basename because the distiller sees one file
    at a time, and the concept name is the heading it was asked to write
    under. `expected_collision_paths` holds None for a concept the miner
    matched to nothing, so "unknown concept" and "no collision" stay
    distinguishable to the coercion pass.
    """
    valid_concepts: dict[str, set[str]] = field(default_factory=dict)
    expected_collision_paths: dict[tuple[str, str], str | None] = field(default_factory=dict)
    concept_excerpts: dict[tuple[str, str], str] = field(default_factory=dict)
    collision_excerpts: dict[tuple[str, str], str] = field(default_factory=dict)
    source_files: dict[str, str] = field(default_factory=dict)
    inbox_folders: set[str] = field(default_factory=set)


def _index_payloads(payloads: list) -> PayloadIndex:
    """Read the payloads once, before any op is judged.

    A batch without `inbox_file` and a concept without `name` are skipped,
    not rejected: the payload is the miner's output, not the model's, and a
    hole in it is nothing an op can be blamed for. `inbox_folders` are made
    absolute against the process cwd, as every boundary check downstream is
    (characterized 2026-09-19).
    """
    index = PayloadIndex()
    for payload_data in payloads or []:
        for batch in payload_data.get("batches", []):
            inbox_file = batch.get("inbox_file")
            if not inbox_file:
                continue
            source_basename = os.path.basename(inbox_file)
            index.inbox_folders.add(os.path.dirname(os.path.abspath(inbox_file)))
            index.source_files[source_basename] = inbox_file
            concepts = index.valid_concepts.setdefault(source_basename, set())
            for c in batch.get("concepts", []):
                name = c.get("name")
                if not name:
                    continue
                concepts.add(name)
                key = (source_basename, name)
                index.concept_excerpts[key] = c.get("inbox_excerpt", "") or ""
                collision = c.get("vault_collision")
                if collision and isinstance(collision, dict) and collision.get("path"):
                    index.expected_collision_paths[key] = collision["path"]
                    index.collision_excerpts[key] = collision.get("excerpt", "") or ""
                else:
                    index.expected_collision_paths[key] = None
    return index


def _normalize_paths(ops: list[Op], *, target_dir: str, write_root: str, repaired) -> None:
    """Repair every op's path in place, before any phase reads one.

    Three repairs in a fixed order, each one's input being the previous
    one's output: the title-derived path, the write-boundary rebase, the
    filename slug. `repaired(pattern)` counts each repair for the caller's
    normalized_out; the reasons for each repair stay on the branch that
    makes it.
    """
    # Sanitize filesystem-illegal characters (e.g. ':') from path filenames.
    # When a write op carries a `title` field, rebuild the path from title so
    # the note is filed under the clean concept name rather than the heading.
    for op in ops:
        if op.op == OpType.write and op.title and op.path and target_dir:
            clean_title = slugify(op.title)
            if clean_title:
                new_path = f"{target_dir.rstrip('/')}/{clean_title}.md"
                if new_path != op.path:
                    logger.debug("validate: title-derived path '%s' → '%s'", op.path, new_path)
                    repaired("title_path")
                    op.path = new_path

        # A NEW note's location is Silica's own filing decision, so a write
        # aimed outside the boundary is rebased into it (structure preserved)
        # rather than rejected — target_dir and the model both predate the
        # vault's declaration. Ops that touch an EXISTING note outside a MIRROR
        # are never rebased: that would silently hit a different file. They get
        # rejected in the main loop below.
        if write_root and op.op == OpType.write and op.path and not within(op.path, write_root):
            rebased = write_root + "/" + op.path.replace("\\", "/").strip("/")
            logger.debug("validate: write_dir rebase '%s' → '%s'", op.path, rebased)
            repaired("write_dir_rebase")
            op.path = rebased

        if op.path:
            folder, filename = os.path.split(op.path)
            name, ext = os.path.splitext(filename)
            # lstrip("."): a leading dot hides the note from every vault walker
            # (the calendar scan, the timeline and ignore_matcher all skip a
            # dot-prefixed part), so a model-supplied ".secret.md" would be
            # written, reported as a success, and never read back again.
            sanitized = (slugify(name).lstrip(".") or "note") + ext
            if sanitized != filename:
                new_path = (os.path.join(folder, sanitized) if folder else sanitized).replace("\\", "/")
                logger.debug("validate: sanitized path '%s' → '%s'", op.path, new_path)
                repaired("path_slugify")
                op.path = new_path


def _dedup_paths(ops: list[Op]) -> None:
    """One op per note: the richest body wins, the others become skips.

    Runs before any coercion so the winner's type is decided on what the
    model actually sent. Same-file identity is same_file_key (case and
    Unicode composition folded), because the vault's filesystem may treat
    the two spellings as one file.
    """
    # 1. Global deduplication (executed before coercion to ensure correct richest op type determination)
    path_groups: dict[str, list[Op]] = {}
    for op in ops:
        path = op.touched_ref()
        if path:
            norm_path = same_file_key(path)
            if norm_path not in path_groups:
                path_groups[norm_path] = []
            path_groups[norm_path].append(op)

    for norm_path, group in path_groups.items():
        if len(group) > 1:
            richest_op = max(group, key=lambda o: len(o.snippet or o.content or ""))
            has_write = any(o.op in (OpType.write, OpType.overwrite) for o in group)
            for op in group:
                if op is not richest_op:
                    op.op = OpType.skip
                    op.reason = f"Duplicate operation to the same path '{op.path}'"
            if has_write:
                # If there's an overwrite in the group, richest_op becomes overwrite.
                # Read after the skips above, so only the richest op's own type can
                # still say overwrite: a richer patch beside an overwrite becomes a
                # write, not an overwrite (characterized 2026-09-19, coercion-order).
                if any(o.op == OpType.overwrite for o in group):
                    richest_op.op = OpType.overwrite
                    # overwrite reads op.content; a coerced write/patch carries its body
                    # in snippet — copy it so _execute_overwrite doesn't crash on content=None (A18)
                    richest_op.content = richest_op.content or richest_op.snippet
                else:
                    richest_op.op = OpType.write
                    # write reads op.snippet; a coerced overwrite carries its body in content (A18)
                    richest_op.snippet = richest_op.snippet or richest_op.content or ""


class TitleGate:
    """The target folder's existing note titles, keyed by title_key, read once.

    Built lazily on the first question and never again in the same validate
    call. Empty on any driver failure: the gate abstains, it never blocks the
    pipeline.
    """

    def __init__(self, target_dir: str) -> None:
        self._target_dir = target_dir
        self._cache: tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]] | None = None
        self._names: list[str] | None = None

    def titles(self, subtree: bool = False) -> dict[str, tuple[str, str]]:
        """key -> (title, path) of the target folder; `subtree=True` adds its
        subfolders (folder entries win on a shared key). Key-EQUAL coercion
        reads the subtree: the user files by subtopic, and the exact-folder view
        wrote a second "Regressione lineare" beside the one in
        Apprendimento supervisionato/ (2026-09-05). The fuzzy near band keeps
        the exact folder, where a lookalike is a variant and not a neighbour."""
        if self._cache is not None:
            return self._cache[subtree]
        from silica.kernel.text.title import title_key
        out: dict[str, tuple[str, str]] = {}
        deep: dict[str, tuple[str, str]] = {}
        names: dict[str, str] = {}
        try:
            norm_dir = (self._target_dir or "").replace("\\", "/").strip("/")
            for ref in DRIVER.list_files(norm_dir):
                ref_dir = os.path.dirname((ref.path or "").replace("\\", "/")).strip("/")
                key = title_key(ref.name)
                if key and ref_dir != norm_dir:
                    deep.setdefault(key, (ref.name, ref.path))
                if ref_dir != norm_dir:
                    continue
                names[ref.name] = ref.path
                if key:
                    out[key] = (ref.name, ref.path)
        except Exception as e:
            logger.debug("validate: title gate enumeration failed (abstaining): %s", e)
        # Frontmatter aliases join the index after the filenames: the alias the
        # judge leaves on a merge winner is the only trace of the absorbed
        # spelling once the loser stub is deleted (2026-09-03, "Percettrone"
        # would have come back as a new note). Same precedence as
        # autolink.build_alias_map: a real title outranks any alias on its key.
        # The ws backend serves no alias index, so there the gate sees names only.
        try:
            for title, aliases in DRIVER.alias_index():
                path = names.get(title)
                if not path:
                    continue
                for alias in aliases or []:
                    key = title_key(str(alias))
                    if key and key not in out:
                        out[key] = (str(alias), path)
        except Exception as e:
            logger.debug("validate: alias index unavailable (gate sees filenames only): %s", e)
        self._cache = (out, {**deep, **out})
        return self._cache[subtree]

    def names(self) -> list[str]:
        """The folder's titles and aliases as a list, for the fuzzy near band."""
        if self._names is None:
            self._names = [t for (t, _p) in self.titles().values()]
        return self._names


def _path_exists_memo():
    """One vault read per distinct path per validate call.

    The same path is asked about by the coercion pass, the main loop and
    the link pass; the answer must not change between them even if a
    sibling op lands meanwhile, so the memo is per call and never shared.
    Keyed on os.path.abspath, i.e. against the process cwd, like every
    other path comparison here (characterized 2026-09-19).
    """
    _existence_cache: dict[str, bool] = {}
    def path_exists(p: str) -> bool:
        norm = os.path.abspath(p)
        if norm not in _existence_cache:
            try:
                DRIVER.read_note(p)
                _existence_cache[norm] = True
            except RuntimeError:
                _existence_cache[norm] = False
        return _existence_cache[norm]
    return path_exists


def _coerce_types(ops: list[Op], *, index: PayloadIndex, has_payloads: bool,
                  title_gate: TitleGate, path_exists, hub: str | None) -> None:
    """Decide each op's real type against the vault, and stamp the hub.

    A write onto a note that exists, by path or by key-equal title, is a
    patch (and follows a superseded loser to its winner); a patch onto a
    note that does not exist is a write, unless the payload named a
    different collision target for it, in which case the main loop will
    reject the mismatch with the reason. Skips are left alone.
    """
    for op in ops:
        if op.op == OpType.skip:
            continue
        if op.op in (OpType.write, OpType.patch, OpType.overwrite) and hub:
            op.hub = hub

        if op.op == OpType.write and op.path and path_exists(op.path):
            op.op = OpType.patch
            # The existing note may be a merge loser: its content already
            # lives in the winner, so the append goes there (run f30ace50,
            # 2026-09-03: three lessons piled onto a superseded "Percettrone").
            from silica.kernel.write.contested import follow_superseded
            op.path = follow_superseded(op.path)
        elif op.op == OpType.write and op.path:
            # C3 gate, band 1: a title key-equal to an existing note in the
            # target folder is the SAME note under a cosmetic variant
            # («Machine Learning (9 CFU)») — mechanical coercion to patch,
            # extending the exact-path coercion above.
            from silica.kernel.text.title import title_key
            stem = os.path.splitext(os.path.basename(op.path))[0]
            from silica.kernel.text.title import numbers_differ, same_surface
            match = title_gate.titles(subtree=True).get(title_key(stem))
            if match and numbers_differ(stem, match[0]):
                match = None  # "Lezione 12" is not "Lezione 11": numbers are identity
            if match is not None and not same_surface(stem, match[0]):
                # ADR-0079: an equal key coerces only when the names read the
                # same. "Tipi di dati (SQL)" onto "Tipi di dati (Python)" was a
                # patch here; the owner keeps 8 of 15 such pairs apart and the
                # names cannot tell which, so the write lands flagged and the
                # judge settles it exactly as for band 2 below.
                from silica.kernel.write.contested import follow_superseded
                cand_path = follow_superseded(match[1] if match[1].endswith(".md") else f"{match[1]}.md")
                _flag(op, f"near_title candidate='{os.path.splitext(os.path.basename(cand_path))[0]}' "
                          f"path='{cand_path}'")
                match = None
            if match is not None:
                logger.info(
                    "validate: title '%s' key-equal to existing '%s' — coercing write→patch",
                    stem, match[0],
                )
                op.op = OpType.patch
                from silica.kernel.write.contested import follow_superseded
                op.path = follow_superseded(match[1] if match[1].endswith(".md") else f"{match[1]}.md")
        elif op.op == OpType.patch and op.path and not path_exists(op.path):
            if has_payloads:
                expected_path = index.expected_collision_paths.get((op.source_basename, op.heading))
                if not expected_path or os.path.abspath(op.path) == os.path.abspath(expected_path):
                    op.op = OpType.write
            else:
                op.op = OpType.write


def _dedup_coerced_paths(ops: list[Op]) -> None:
    """One op per note again, now by post-coercion path.

    The band-1 title-key coercion (_coerce_types) can retarget two writes with
    different paths onto the SAME existing note; _dedup_paths ran before it and
    cannot see it. Re-dedup by post-coercion path so two ops never target one
    note (also removes the duplicate touched_ref that would let write.py's
    failed-op lookup defer one op twice and drop the other — audit A21/A4).
    """
    coerced_groups: dict[str, list[Op]] = {}
    for op in ops:
        if op.op == OpType.skip:
            continue
        ref = op.touched_ref()
        if ref:
            coerced_groups.setdefault(same_file_key(ref), []).append(op)
    for _ref_path, group in coerced_groups.items():
        if len(group) > 1:
            keep = max(group, key=lambda o: len(o.snippet or o.content or ""))
            for op in group:
                if op is not keep:
                    op.op = OpType.skip
                    op.reason = f"Duplicate operation to the same path '{op.path}' after coercion"


class Grounding:
    """The source-side judgement of a body, against what the distiller saw.

    Two verdicts share one source text (the concept's inbox excerpt plus the
    colliding vault excerpt): the warn-only span check, which fills
    ungrounded_out and grounding_out and never rejects, and the extractive
    rejection, on only under the extractive profiles or
    SILICA_EXTRACTIVE_ENFORCE=1. The profile is resolved once per call: the
    run's own when it passed one, else the vault's.
    """

    def __init__(self, index: PayloadIndex, *, profile: str | None,
                 ungrounded_out: list | None, grounding_out: list | None) -> None:
        from silica.kernel.prep_delegation import active_distill_profile

        self._index = index
        self._ungrounded_out = ungrounded_out
        self._grounding_out = grounding_out
        self._source_text_cache: dict[str, str] = {}
        self.profile = profile or active_distill_profile()
        self.enforce_extractive = (os.getenv("SILICA_EXTRACTIVE_ENFORCE") == "1"
                                   or self.profile in EXTRACTIVE_PROFILES)

    def full_source_text(self, basename: str) -> str:
        """The whole source document, "" when unreadable. A finished source is
        archived at CLEANUP and RETRY/steer validates after the move — so look
        there too, at the path `archive_path_for` would have produced. The flat
        `<archive>/<basename>` stays last in line: sources archived before
        2026-08-23 kept no folder structure and still have to be readable."""
        if basename not in self._source_text_cache:
            from silica.kernel.vault_manifest import active_done_dir, archive_path_for

            text = ""
            inbox_path = self._index.source_files.get(basename)
            candidates = [
                inbox_path,
                archive_path_for(inbox_path) if inbox_path else "",
                f"{active_done_dir()}/{basename}",
            ]
            for cand in candidates:
                if not cand:
                    continue
                try:
                    text = DRIVER.read_note(cand).content
                    break
                except Exception:
                    continue
            self._source_text_cache[basename] = text
        return self._source_text_cache[basename]

    def check(self, op: Op) -> None:
        """Warn-only verbatim gate (never rejects): math/code spans in the body
        that can't be located in the source excerpt are fabrication candidates."""
        # Ground against everything the distiller legitimately saw for this
        # concept: inbox excerpt + colliding vault note excerpt — a patch
        # restating a vault formula for coherence is not fabrication.
        excerpt = self._index.concept_excerpts.get((op.source_basename, op.heading), "")
        collision = self._index.collision_excerpts.get((op.source_basename, op.heading), "")
        source_text = f"{excerpt}\n{collision}" if collision else excerpt
        body = op.snippet or op.content or ""
        if not source_text.strip() or not body:
            return
        from silica.kernel.write.provenance import ungrounded_spans
        spans = ungrounded_spans(body, source_text)
        if spans:
            # Second look against the whole document: the distiller (and the
            # steer model, which sees the entire bundle) legitimately stitches
            # from neighboring windows, and a span verbatim elsewhere in the
            # source is not fabrication (IDK.md, 2026-08-05: 3/3 flagged spans
            # were verbatim in the file, outside the concept's window). Only
            # ever clears spans — a class the excerpt didn't gate stays ungated.
            full = self.full_source_text(op.source_basename)
            if full:
                still = set(ungrounded_spans(body, f"{source_text}\n{full}"))
                spans = [s for s in spans if s in still]
        if spans:
            logger.warning(
                "validate: '%s' — %d verbatim span(s) not grounded in source excerpt: %s",
                op.path, len(spans), " | ".join(s[:60] for s in spans),
            )
            if self._ungrounded_out is not None:
                self._ungrounded_out.append({
                    "path": op.path,
                    "heading": op.heading,
                    "source_basename": op.source_basename,
                    "spans": spans,
                })
        if self._grounding_out is not None:
            from silica.kernel.write.provenance import grounding_counts
            # Denominator against the excerpt (what the gate judged); the
            # numerator is what survived the whole-document second look.
            checked, _first_look = grounding_counts(body, source_text)
            self._grounding_out.append({
                "path": op.path,
                "heading": op.heading,
                "source_basename": op.source_basename,
                "spans": checked,
                "ungrounded": len(spans),
                "profile": self.profile,
            })

    def extractive_reject(self, op: Op) -> str | None:
        """Under the extractive distill profile a write/patch body must be
        SELECTED verbatim from the source, not rewritten. Returns a rejection
        reason when it isn't — routing the op through the normal defer/steer
        retry so a persistent violator becomes a declared hole, never silent
        loss. On when the extractive profile is active (the invariant is the
        profile's contract, one lever) or under SILICA_EXTRACTIVE_ENFORCE=1;
        otherwise off — the default distiller paraphrases legitimately, so
        this must never fire outside such a run."""
        if not self.enforce_extractive:
            return None
        excerpt = self._index.concept_excerpts.get((op.source_basename, op.heading), "")
        collision = self._index.collision_excerpts.get((op.source_basename, op.heading), "")
        source_text = f"{excerpt}\n{collision}" if collision else excerpt
        body = op.snippet or op.content or ""
        if not source_text.strip() or not body.strip():
            return None
        from silica.kernel.write.provenance import nonextractive_lines
        bad = nonextractive_lines(body, source_text)
        if bad:
            return ("extractive: %d body line(s) not verbatim from source: %s"
                    % (len(bad), " | ".join(s[:60] for s in bad[:3])))
        return None


def _record_dangling_links(ops: list[Op], *, future_ref_whitelist: list[str] | None,
                           path_exists, cleared_links_out: list | None) -> None:
    """Name the wikilinks of the batch that resolve nowhere, and keep every op.

    Surface wikilinks introduced by write/patch/overwrite
    ops that cannot be resolved in the current vault, within this batch, or via the
    future_ref_whitelist.  Unlike parents (see _resolve_parent), an unresolved inline
    link does NOT reject the op — it is kept verbatim as a dangling forward-reference
    and recorded in cleared_links_out, symmetric with cleared_parents.  This mirrors
    Obsidian semantics (dangling links are first-class) and prevents a self-referential
    source from losing whole notes to the rejection-rate gate.
    Any op that leaves a note at op.path after this batch (write/patch/overwrite) is a
    valid in-batch link target — not just freshly-written notes.
    """
    batch_created_names: set[str] = {
        os.path.splitext(os.path.basename(op.path))[0].lower()
        for op in ops
        if op.op in (OpType.write, OpType.patch, OpType.overwrite) and op.path
    }

    _link_resolve_cache: dict[str, bool] = {}
    whitelist_lower = {w.lower() for w in (future_ref_whitelist or [])}

    def _link_resolves(target: str) -> bool:
        stem = target.removesuffix(".md")
        key = stem.lower()
        if key in _link_resolve_cache:
            return _link_resolve_cache[key]
        if key in batch_created_names:
            _link_resolve_cache[key] = True
            return True
        if key in whitelist_lower:
            _link_resolve_cache[key] = True
            return True
        if "/" in stem:
            result = path_exists(stem + ".md") or path_exists(stem)
        else:
            matches = DRIVER.search_names(stem)
            result = any(r.name.lower() == key for r in matches)
        _link_resolve_cache[key] = result
        return result

    for op in ops:
        if op.op not in (OpType.write, OpType.patch, OpType.overwrite):
            continue
        text = op.snippet or op.content or ""
        if not text:
            continue
        links = extract_links(text)
        broken = [lnk for lnk in links if not _link_resolves(lnk)]
        if broken:
            logger.debug(
                "validate: %d unresolved wikilink(s) kept as forward-ref in '%s': %r",
                len(broken), op.path or op.heading or "?", broken,
            )
            if cleared_links_out is not None:
                for lnk in broken:
                    cleared_links_out.append({
                        "cleared_link": lnk,
                        "note_heading": op.heading or "",
                        "note_path": op.path or "",
                    })


def _record_ambiguous_links(ops: list[Op], ambiguous_out: list | None) -> None:
    """Name the wikilinks of the batch that a shared name sends to another folder.

    A bare `[[Name]]` with several notes of that name and none beside the note
    resolves to the shallowest of them. In the vaults Silica wrote, measured
    2026-10-15, every such link checked was wrong or arbitrary: 7 of 7 Zocor
    links opened another drug's section, 8 of 8 hub links a source leaf
    instead of the lecture outline (ADR-0114). Which note the model meant is
    not in the op, so the link is kept as written and only reported: a
    rewrite would guess, and this exists to stop guessing.
    """
    if ambiguous_out is None:
        return
    from silica.driver.base import namesake_order

    batch: dict[str, set[str]] = {}
    for op in ops:
        if op.op in (OpType.write, OpType.patch, OpType.overwrite) and op.path:
            batch.setdefault(os.path.splitext(os.path.basename(op.path))[0].lower(), set()).add(
                op.path if op.path.endswith(".md") else op.path + ".md")
    seen: dict[str, list[str]] = {}
    for op in ops:
        if op.op not in (OpType.write, OpType.patch, OpType.overwrite) or not op.path:
            continue
        here = os.path.dirname(op.path.replace("\\", "/"))
        for lnk in dict.fromkeys(extract_links(op.snippet or op.content or "")):
            stem = lnk.split("|", 1)[0].split("#", 1)[0].strip().removesuffix(".md")
            if not stem or "/" in stem:
                continue
            key = stem.lower()
            if key not in seen:
                try:
                    named = set(DRIVER.notes_named(stem))
                except Exception:
                    named = set()  # a backend without the index: nothing to compare, nothing reported
                seen[key] = sorted(named | batch.get(key, set()), key=namesake_order)
            cands = seen[key]
            if len(cands) < 2 or any(os.path.dirname(c) == here for c in cands):
                continue
            ambiguous_out.append({"link": stem, "note_path": op.path, "resolves_to": cands[0],
                                  "candidates": cands})


def _missing_hub_ops(ops: list[Op], *, target_dir: str, path_exists) -> list[Op]:
    """A write for every hub the batch names and the vault does not have.

    The hub is asked about by its bare name, the way a wikilink would be,
    and created in the target folder unless the batch already writes it,
    by heading or by path. The stub is attributed to the batch's first
    source so provenance never reads "auto_generated" when a real source
    is known.
    """
    hubs_to_check = set()
    for op in ops:
        op_type = op.op
        # An overwrite rewrites a note that exists, and the bookkeeping ones
        # (a merge's alias, its superseded_by pointer) carry only the folder's
        # name as their hub: an empty "Teoria della probabilità" landed in the
        # owner's Matematica folder after an alias on Variabile casuale (2026-10-23).
        if op_type in (OpType.write, OpType.patch):
            hub = op.hub
            if hub:
                clean_hub = hub.strip("[]")
                if clean_hub:
                    hubs_to_check.add(clean_hub)

    hub_ops = []
    for hub in sorted(hubs_to_check):
        if not path_exists(hub):
            hub_filename = f"{hub}.md"
            hub_path = os.path.join(target_dir, hub_filename).replace("\\", "/")

            already_creating = any(
                (o.op == OpType.write and o.heading == hub) or
                (o.path and same_file_key(o.path) == same_file_key(hub_path))
                for o in ops
            )

            if not already_creating:
                source_basename = "auto_generated"
                if ops:
                    source_basename = ops[0].source_basename or "auto_generated"

                hub_op = Op(
                    op=OpType.write,
                    heading=hub,
                    path=hub_path,
                    snippet="Hub automatically generated by the Injector pipeline.",
                    hub=hub,
                    source_basename=source_basename
                )
                hub_ops.append(hub_op)
                logger.info("Validation: hub '%s' does not exist. Injected creation operation at %s", hub, hub_path)
    return hub_ops


def _count_repair(normalized_out: dict | None, pattern: str) -> None:
    if normalized_out is not None:
        normalized_out[pattern] = normalized_out.get(pattern, 0) + 1


def _invented_folder(rel_path: str, vault_root: str) -> str:
    """The mirror-relative parent of `rel_path` when no folder answers for it
    yet; "" when one does, when it is the mirror root, or when no vault.

    Two places answer, because the mirror is a preview of the vault after the
    paste: a folder the vault already has, and a folder an earlier run
    already created INSIDE the mirror (justified once, at creation — the
    gate is per folder, not per note, and the user's veto is declining to
    paste it).

    Presence check only — whether the reason is a *good* one is the prompt's
    job, not an LLM adjudication this seam has to stay deterministic without.
    """
    under = (rel_path or "").replace("\\", "/").strip("/").split("/", 1)
    parent = os.path.dirname(under[1]) if len(under) == 2 else ""
    if not parent or not vault_root:
        return ""
    if os.path.isdir(os.path.join(vault_root, parent)):
        return ""
    if os.path.isdir(os.path.join(vault_root, SAFE_WRITE_DIR, parent)):
        return ""
    return parent


def _is_within_dir(path_abs: str, dir_abs: str) -> bool:
    if not dir_abs:
        return True
    try:
        return os.path.commonpath([path_abs, dir_abs]) == dir_abs
    except ValueError:
        return False


def _justified_folders(ops: list[Op], *, mirror: bool, target_dir: str, vault_root: str) -> set[str]:
    """Safe mode: the invented folders this batch has already accounted for.

    Resolved up front so the gate is order-independent: with a per-op check
    five notes into one new folder rejected four of them, and WHICH four
    depended on where the model happened to put the sentence.
    """
    justified_folders: set[str] = set()
    if mirror:
        # The run's landing folder is the user's own filing decision (typed at
        # /nucleate, folded onto the tree by resolve_target_dir), so it never
        # owes the model's justification. Exact match, not `within`: a subtree
        # improvised UNDER it is still the model's invention.
        landing = (target_dir or "").replace("\\", "/").strip("/")
        if landing and not os.path.isabs(target_dir or ""):
            head, _, tail = landing.partition("/")
            if head.lower() == SAFE_WRITE_DIR:
                landing = tail  # the user typed the mirror path themselves
            if landing:
                justified_folders.add(landing.lower())
        for op in ops:
            if op.op == OpType.write and op.path and (op.reason or "").strip():
                stated = _invented_folder(op.path, vault_root)
                if stated:
                    justified_folders.add(stated.lower())
    return justified_folders


@dataclass(frozen=True)
class _Batch:
    """What every branch of the main loop reads and none of them changes.

    Built once the ops have their final paths and types (after
    normalization and coercion), so the two sets computed over the ops,
    run_write_stems and justified_folders, see what the loop will see.
    """
    target_dir: str
    target_dir_abs: str
    write_root: str
    mirror: bool
    vault_root: str
    has_payloads: bool
    index: PayloadIndex
    title_gate: TitleGate
    path_exists: Callable[[str], bool]
    grounding: Grounding
    justified_folders: set[str]
    run_write_stems: set[str]
    normalized_out: dict | None
    cleared_parents_out: list | None

    def repaired(self, pattern: str) -> None:
        _count_repair(self.normalized_out, pattern)

    def resolve_parent(self, op: Op) -> None:
        """Neutralise an unresolvable parent — fall back to hub, no Rejection.

        With a cleared_parents_out sink, records the cleared reference as a
        forward-link hint so the distiller can anticipate the note in future iterations.
        """
        if not op.parent:
            return
        p_key = op.parent.lower()
        if p_key in self.run_write_stems:
            return
        matches = DRIVER.search_names(op.parent)
        if not any(r.name.lower() == p_key for r in matches):
            logger.warning(
                "validate: parent '%s' not found in vault or current run — clearing to hub fallback",
                op.parent,
            )
            if self.cleared_parents_out is not None:
                self.cleared_parents_out.append({
                    "cleared_parent": op.parent,
                    "note_heading": op.heading or "",
                    "note_path": op.path or "",
                })
            op.parent = None


def _validate_patch(op: Op, batch: _Batch) -> Rejection | None:
    """A patch lands on the note it names, and that note must be real.

    Under safe mode the model reads TARGET in mirror space and hands the
    collision back mirror-prefixed: the prefix is undone so every check
    judges the note actually enriched, and put back at the end so the
    append goes to the mirror copy while the vault note stays untouched
    until the user pastes.
    """
    heading, source_basename, path = op.heading, op.source_basename, op.path
    assert path is not None  # Op's model validator requires it for patch
    mirror_patch = batch.mirror
    # Safe mode: the model reads TARGET in mirror space and hands the
    # collision back mirror-prefixed. Undo the prefix so every check
    # below judges the note actually enriched, symmetric with the rebase
    # that re-applies it at the bottom of this branch. Only an exact
    # mirror image of the path the payload named is repaired — a patch
    # aimed at a different note stays a rejection.
    if mirror_patch and batch.write_root and within(path, batch.write_root):
        expected = batch.index.expected_collision_paths.get((source_basename, heading))
        unmirrored = path.replace("\\", "/").strip("/")[len(batch.write_root) + 1:]
        if expected and unmirrored == expected.replace("\\", "/").strip("/"):
            logger.debug("validate: mirror patch un-rebase '%s' → '%s'",
                         path, unmirrored)
            batch.repaired("mirror_patch_unrebase")
            op.path = path = unmirrored

    path_abs = os.path.abspath(path)
    if batch.has_payloads:
        expected_path = batch.index.expected_collision_paths.get((source_basename, heading))
        if expected_path:
            if path_abs != os.path.abspath(expected_path):
                return Rejection(op=op, reason=f"Path '{path}' does not match expected collision '{expected_path}'")
        else:
            if not _is_within_dir(path_abs, batch.target_dir_abs):
                return Rejection(op=op, reason=f"Coerced patch path '{path}' not in target folder")
    elif not _is_within_dir(path_abs, batch.target_dir_abs):
        return Rejection(op=op, reason=f"Path '{path}' not in target folder")

    if not batch.path_exists(path):
        return Rejection(op=op, reason=f"Collision path '{path}' does not exist in vault")

    batch.resolve_parent(op)
    batch.grounding.check(op)
    _reason = batch.grounding.extractive_reject(op)
    if _reason:
        return Rejection(op=op, reason=_reason)
    # Everything above judged the op against the note it actually
    # enriches. Only the landing moves: under safe mode the append goes
    # to the mirror copy (`_execute_patch` seeds it from the original),
    # so the vault note itself is untouched until the user pastes.
    if mirror_patch and not within(path, batch.write_root):
        rebased = batch.write_root + "/" + path.replace("\\", "/").strip("/")
        logger.debug("validate: mirror patch rebase '%s' → '%s'", path, rebased)
        batch.repaired("mirror_patch_rebase")
        op.path = rebased
    return None

class _Skip(Enum):
    """A write that is neither landed nor rejected."""
    # Its concept was only mentioned in the source, never defined: nothing
    # to distill, nothing a retry could recover. The note stays a forward
    # reference, linked from the notes that mention it, until a later
    # source defines it.
    FORWARD_REFERENCE = auto()


def _validate_write(op: Op, batch: _Batch) -> Rejection | _Skip | None:
    """A new note lands in the target folder with a body worth landing.

    Two verdicts are soft and stay on the op as `review`: a title fuzzy-near
    an existing one (the dedup judge is asked once the note exists) and a
    body thinner than the floor (the expand worker re-authors it in place).
    An empty body is hard, an announcement of the section instead of the
    section is hard, and both route through defer/steer with the reason.
    """
    heading, source_basename, path = op.heading, op.source_basename, op.path
    assert path is not None  # Op's model validator requires it for write
    path_abs = os.path.abspath(path)
    if not _is_within_dir(path_abs, batch.target_dir_abs):
        return Rejection(op=op, reason=f"Path '{path}' not in target folder")

    if batch.path_exists(path):
        return Rejection(op=op, reason=f"Target path '{path}' already exists (should be patch/overwrite)")

    # C3 gate, band 2: fuzzy-near an existing title (Descriptor vs
    # Description). SOFT: the note lands with `review:` set and the
    # dedup judge is asked once it exists (write.py _settle_flagged).
    # It used to reject-and-defer "so the judge decides", but the judge
    # only ever ran inside the FSM: the anneal retry re-validated with
    # no judge, the same band fired again, and 13 of the 23 ops parked
    # on the ML lessons (2026-09-02) were distinct topics ("Algoritmo
    # online del percettrone" vs "Algoritmo del percettrone") waiting
    # for a verdict nobody could give. A flagged near-duplicate the
    # reader can see beats a topic that silently expires at 30 days.
    from silica.kernel.text.title import near_titles
    stem = os.path.splitext(os.path.basename(path))[0]
    near_hits = near_titles(stem, batch.title_gate.names())
    if near_hits:
        cand_title, ratio = near_hits[0]
        cand_path = next(
            (p for (t, p) in batch.title_gate.titles().values() if t == cand_title), ""
        )
        if cand_path:
            # A superseded neighbour hands the judge its winner: the
            # verdict is about where the content lives, not the stub.
            from silica.kernel.write.contested import follow_superseded
            winner = follow_superseded(cand_path)
            if winner != cand_path:
                cand_path = winner
                cand_title = os.path.splitext(os.path.basename(winner))[0]
        _flag(op, f"near_title candidate='{cand_title}' path='{cand_path}' "
                  f"ratio={ratio:.2f}")

    body_len = len(_content_only(op.snippet or "", claim_first=bool(getattr(op, "section", None))))
    if body_len == 0 and batch.has_payloads:
        # Distinguish two ways a write lands with an empty body:
        #  (a) the source excerpt itself is empty — the concept was only
        #      *mentioned*, never defined. Nothing to distill or expand;
        #      deferring only churns (and a whole chunk of these drives the
        #      rejection rate to 100% and aborts the run). Skip it as a
        #      forward-reference — it stays linked from the notes that
        #      mention it, to be authored when a later source defines it.
        #  (b) the excerpt HAD content but the distiller dropped the body
        #      (run 5d0a3350 regression) — that must still be rejected so
        #      the expand arc retries it. Falls through below.
        excerpt = batch.index.concept_excerpts.get((source_basename, heading))
        if excerpt is not None and not excerpt.strip():
            logger.info(
                "validate: write '%s' — empty source excerpt, skipped as a "
                "forward-reference (nothing to distill)", op.path,
            )
            return _Skip.FORWARD_REFERENCE
    # Floor read at call time (not import) so an arm can lower it via env
    # without import-order fragility. Extractive selects verbatim spans, and
    # a durable fact can live in a legitimately short turn — a 60-char
    # verbatim fact is real content, not the prose-placeholder this gate
    # guards against — so the extractive arm sets a lower floor.
    _min_snippet = min_write_snippet_chars(batch.grounding.profile)
    if body_len == 0:
        # Nothing to land: an empty body IS the placeholder the floor
        # exists to keep out of the vault (run 5d0a3350). Stays hard.
        return Rejection(
            op=op,
            reason=(
                f"snippet too short ({body_len} < {_min_snippet} chars) "
                f"— would write a placeholder note, deferred for retry"
            ),
        )
    if body_len < _min_snippet:
        # SOFT: a 200-char body is thin, not empty. It lands flagged and
        # the expand worker re-authors it in place (expand.py); before,
        # the same op sat in the deferred store where deterministic
        # re-validation could never clear it.
        _flag(op, f"snippet too short ({body_len} < {_min_snippet} chars)")

    # Shape gate: a long-enough body can still be an announcement of the
    # section instead of the section. Skipped under extractive enforce
    # (verbatim-selected content is grounded by construction, and the
    # source's own wording must not be judged); env kill-switch for
    # eval arms, same pattern as the length floor.
    if not batch.grounding.enforce_extractive and os.getenv("SILICA_META_SHAPE_CHECK", "1") != "0":
        _shape_reason = meta_description_reason(op.snippet or "")
        if _shape_reason:
            return Rejection(op=op, reason=_shape_reason)

    batch.resolve_parent(op)
    batch.grounding.check(op)
    _reason = batch.grounding.extractive_reject(op)
    if _reason:
        return Rejection(op=op, reason=_reason)
    return None


def _validate_overwrite(op: Op, batch: _Batch) -> Rejection | None:
    """A whole-note rewrite of a note in the target folder.

    A target that is not there degrades the op to a write of the same body
    and lands without the write branch's own gates (characterized
    2026-09-19). A producer that forgot to snapshot the base gets the note
    as it is now, a weak guard that only covers the validate-to-execute
    window.
    """
    path = op.path
    assert path is not None  # Op's model validator requires it for overwrite
    path_abs = os.path.abspath(path)
    if not _is_within_dir(path_abs, batch.target_dir_abs):
        return Rejection(op=op, reason=f"Path '{path}' not in target folder")

    if not batch.path_exists(path):
        # If target note doesn't exist, overwrite degrades to write gracefully.
        # overwrite body lives in op.content; _execute_write reads op.snippet,
        # so carry it over or the degraded write persists an empty note (A17).
        op.op = OpType.write
        op.snippet = op.snippet or op.content or ""
    elif op.base_content is None:
        # LAST-RESORT fallback, deliberately weak: it only guards the
        # validate->execute window. An edit that landed while the
        # producer computed `content` is already on disk here and
        # becomes the base itself — no conflict detected, edit stomped.
        # Producers must snapshot base_content at READ time (the dedup
        # merges and expand do); this exists only for a producer that
        # forgets, so its overwrites are not entirely unguarded.
        op.base_content = DRIVER.read_note(path).content

    batch.resolve_parent(op)
    return None


def _admit(op: Op, batch: _Batch) -> Rejection | None:
    """The checks every op type passes before its own branch judges it.

    In order: the vault write boundary (under safe mode a patch is exempt,
    judged against the real note it enriches and rebased onto the mirror
    copy at the bottom of its branch; an overwrite is exempt too and is
    never rebased, it rewrites the note it names), the folder a new note
    invents under safe mode, the heading and source against the payload
    (both may be corrected on the op: a unique normalized, token-subset or
    stemmed match becomes the canonical name, a lone source in the chunk
    claims a mismatched basename), and the inbox tree no op may write into.
    """
    heading, op_type, source_basename, path = op.heading, op.op, op.source_basename, op.path

    # Vault write boundary (`write_dir` in vault.yaml). Distinct from
    # target_dir, which is a per-run landing folder: this one is per-vault,
    # applies to every op type, and protects files Silica reads but must not
    # author (a source tree, someone's hand-written notes). "" ⇒ whole vault,
    # so a vault that declares nothing is unaffected.
    # Safe mode exempts patches: they are judged against the REAL note they
    # enrich (collision path, existence) and only then rebased onto its
    # mirror copy, at the bottom of the patch branch. Rejecting them here
    # left safe mode able to create and never to enrich, which is most of
    # what an ingest does — 11 of 20 deferred ops in one real run, and 6
    # patches on the same lecture without safe mode against 1 with.
    mirror_patch = batch.mirror and op_type in (OpType.patch, OpType.overwrite)
    if batch.write_root and not mirror_patch:
        touched = [op.touched_ref()]
        if op_type == OpType.move:
            touched.append(op.to_path)  # a move must not leave the boundary either
        outside = [t for t in touched if t and not within(t, batch.write_root)]
        if outside:
            return Rejection(
                op=op,
                reason=f"outside the vault write boundary '{batch.write_root}/': {outside[0]}",
            )

    # Safe mode: a new note may land in any folder that already answers for
    # it without saying anything. A folder nobody has claimed yet is a
    # filing decision the model made alone — it costs one sentence to state
    # it, and the steering loop lets it retry with one.
    if batch.mirror and op_type == OpType.write and path:
        invented = _invented_folder(path, batch.vault_root)
        if invented and invented.lower() not in batch.justified_folders:
            return Rejection(
                op=op,
                reason=(
                    f"'{invented}/' does not exist in the vault — file this "
                    f"under a folder that does, or set \"reason\" to state "
                    f"why no existing folder fits"
                ),
            )

    if batch.has_payloads:
        if not heading:
            return Rejection(op=op, reason="Missing 'heading' field")
        if not source_basename:
            return Rejection(op=op, reason="Missing 'source_basename' field")
        if source_basename not in batch.index.valid_concepts:
            if len(batch.index.valid_concepts) == 1:
                # Unambiguous: exactly one source file in this chunk, so a
                # mismatched source_basename can only mean that one —
                # coerce instead of rejecting (mirrors the heading
                # near-match fallback below, same unique-candidate logic).
                (only_basename,) = batch.index.valid_concepts.keys()
                logger.info(
                    "validate: source_basename '%s' coerced to only candidate '%s'",
                    source_basename, only_basename,
                )
                op.source_basename = source_basename = only_basename
            else:
                return Rejection(op=op, reason=f"Unknown source_basename '{source_basename}'")
        if heading not in batch.index.valid_concepts[source_basename]:
            # The distiller re-cases names and swaps typographic for
            # straight apostrophes — remap a normalized unique match to
            # the canonical payload name instead of rejecting.
            def _heading_key(s: str) -> str:
                return " ".join(s.replace("’", "'").casefold().split())

            near = [
                n for n in batch.index.valid_concepts[source_basename]
                if _heading_key(n) == _heading_key(heading)
            ]
            if len(near) != 1:
                # Fallback: the op title is often a cleaner rephrase of the
                # registered concept ("DataFrame" vs "dataframe spark").
                # Accept only a UNIQUE token-subset match (one side's word
                # set contained in the other) — ambiguous / 1-to-many still
                # rejects, so content can't attach to the wrong concept.
                ht = frozenset(_heading_key(heading).split())
                if ht:
                    subset = [
                        n for n in batch.index.valid_concepts[source_basename]
                        if (nt := frozenset(_heading_key(n).split())) and (ht <= nt or nt <= ht)
                    ]
                    if len(subset) == 1:
                        near = subset
            if len(near) != 1:
                # Fallback: same idea, one more step of normalization for
                # morphological variants a raw-token subset can't see
                # ("Stima parametrica" vs "Stima dei parametri" — no shared
                # token, same Snowball stem). Same unique-match-only
                # discipline as above.
                from silica.config import CONFIG
                from silica.kernel.text.text import stem_word

                lang = getattr(CONFIG, "cooccurrence_lang", "auto")
                hs = frozenset(
                    stem_word(t, lang=lang) for t in _heading_key(heading).split()
                )
                if hs:
                    stemmed = [
                        n for n in batch.index.valid_concepts[source_basename]
                        if (ns := frozenset(
                            stem_word(t, lang=lang) for t in _heading_key(n).split()
                        )) and (hs <= ns or ns <= hs)
                    ]
                    if len(stemmed) == 1:
                        near = stemmed
            if len(near) != 1:
                return Rejection(op=op, reason=f"Heading '{heading}' not present in payload concepts")
            logger.info("validate: heading '%s' normalized to payload concept '%s'", heading, near[0])
            op.heading = heading = near[0]

    if path:
        path_abs = os.path.abspath(path)
        # Guard the whole configured inbox tree, not just the folders of the
        # current run — a patch aimed at a *different* Inbox subfolder used
        # to slip through validate and die (or land) at WRITE.
        from silica.kernel.recall.paths import is_inbox_path
        forbidden = (
            is_inbox_path(path)
            or any(_is_within_dir(path_abs, folder) for folder in batch.index.inbox_folders)
        )
        if "/0 Inbox/" in path or "/0 inbox/" in path.lower() or forbidden:
            return Rejection(op=op, reason=f"Target path '{path}' contains forbidden inbox segment")
    return None


def validate_operations(
    ops: list[Op] | list[dict],
    payloads: list,
    target_dir: str,
    hub: str | None = None,
    cleared_parents_out: list | None = None,
    future_ref_whitelist: list[str] | None = None,
    cleared_links_out: list | None = None,
    ungrounded_out: list | None = None,
    profile: str | None = None,
    normalized_out: dict | None = None,
    grounding_out: list | None = None,
    ambiguous_links_out: list | None = None,
) -> tuple[list[Op], list[Rejection]]:
    """Validates operations against payloads and target_dir using DRIVER.

    ambiguous_links_out (optional): one `{link, note_path, resolves_to,
    candidates}` per bare wikilink that a name shared across folders sends
    elsewhere (`_record_ambiguous_links`). Never causes a rejection.

    ungrounded_out (optional): collects warn-only span-grounding hits —
    write/patch ops whose math/code spans can't be located in their source
    excerpt (fabrication candidates). Never causes a rejection.

    grounding_out (optional): one `{path, heading, source_basename, spans,
    ungrounded, profile}` per ACCEPTED op that had a source excerpt to judge
    against, whether or not anything was flagged (epistemic-state spec M1).
    `ungrounded_out` is the warning; this is the score, and a score needs the
    zero rows too. Ops rejected later in the same pass are dropped at return.

    profile (optional): the RUN's distill profile. When set it decides the
    extractive enforcement and the snippet floor instead of the process-global
    resolution — a /promote run judges its ops as extractive while a
    concurrent /nucleate keeps the default gate.

    normalized_out (optional): counts the malformed-but-unambiguous ops this
    call repaired, keyed by pattern. A reject total says nothing an author can
    act on; "nine of ten repairs were a path rebuilt from the title" names the
    prompt rule to change. Repairs already happened and only reached a debug
    log — this is the same set, counted. Absent keys mean no repair of that
    kind, never a zero row.
    """
    from silica.config import CONFIG
    from silica.kernel.write.ops_io import parse_ops
    from silica.kernel.vault_manifest import active_write_dir
    ops_parsed = parse_ops(ops)
    ops = [op.model_copy(deep=True) for op in ops_parsed]

    # Vault write boundary (`write_dir` in vault.yaml); "" ⇒ the whole vault, so
    # a vault that declares nothing behaves exactly as before.
    write_root = active_write_dir()
    # Safe mode only: `silica/` mirrors the vault's own tree, so a subfolder
    # inside it is a claim that the vault has no folder for this note. The rebase
    # below repairs a FORGOTTEN prefix for free, but an INVENTED subtree is
    # indistinguishable from a correct one — hence the check further down.
    mirror = write_root == SAFE_WRITE_DIR
    vault_root = (getattr(CONFIG, "vault_path", "") or "").strip()

    # The run's landing folder follows the ops into the boundary. Without this
    # the rebase below moves every write into `write_root/` while target_dir
    # still names the folder outside it, and the "not in target folder" check
    # downstream then rejects the whole batch — the two were comparing paths
    # from different spaces. Reachable before safe mode (a code vault plus any
    # target_dir outside docs/silica); reachable on every prose vault after it.
    if write_root and target_dir and not os.path.isabs(target_dir) and not within(
        target_dir, write_root
    ):
        target_dir = write_root + "/" + target_dir.replace("\\", "/").strip("/")

    _normalize_paths(ops, target_dir=target_dir, write_root=write_root,
                     repaired=lambda pattern: _count_repair(normalized_out, pattern))

    has_payloads = bool(payloads)
    index = _index_payloads(payloads)

    path_exists = _path_exists_memo()

    _dedup_paths(ops)

    title_gate = TitleGate(target_dir)

    # 2. Coerce write <-> patch and enforce default hub fallback
    if not hub and target_dir:
        hub = os.path.basename(target_dir.rstrip("/\\"))

    _coerce_types(ops, index=index, has_payloads=has_payloads, title_gate=title_gate,
                  path_exists=path_exists, hub=hub)
    _dedup_coerced_paths(ops)

    batch = _Batch(
        target_dir=target_dir,
        target_dir_abs=os.path.abspath(target_dir) if target_dir else "",
        write_root=write_root,
        mirror=mirror,
        vault_root=vault_root,
        has_payloads=has_payloads,
        index=index,
        title_gate=title_gate,
        path_exists=path_exists,
        grounding=Grounding(index, profile=profile, ungrounded_out=ungrounded_out,
                            grounding_out=grounding_out),
        justified_folders=_justified_folders(ops, mirror=mirror, target_dir=target_dir,
                                             vault_root=vault_root),
        # Note stems created in this run, so parent validation can allow forward
        # references to notes being written in the same batch.
        run_write_stems={
            os.path.splitext(os.path.basename(op.path))[0].lower()
            for op in ops
            if op.op in (OpType.write, OpType.overwrite) and op.path
        },
        normalized_out=normalized_out,
        cleared_parents_out=cleared_parents_out,
    )

    validated_ops = []
    rejected_ops = []

    for op in ops:
        # Skip ops are no-ops (dedup/axis demotion keeps the original path on
        # them) — rejecting one inflates the rejection rate past its
        # actionable-only denominator, so short-circuit before any check.
        if op.op == OpType.skip:
            continue
        rejection = _admit(op, batch)
        if rejection:
            rejected_ops.append(rejection)
            continue

        if op.op == OpType.patch:
            rejection = _validate_patch(op, batch)
            if rejection:
                rejected_ops.append(rejection)
            else:
                validated_ops.append(op)

        elif op.op == OpType.write:
            verdict = _validate_write(op, batch)
            if isinstance(verdict, Rejection):
                rejected_ops.append(verdict)
            elif verdict is None:
                validated_ops.append(op)

        elif op.op == OpType.overwrite:
            rejection = _validate_overwrite(op, batch)
            if rejection:
                rejected_ops.append(rejection)
            else:
                validated_ops.append(op)

        else:
            rejected_ops.append(Rejection(op=op, reason=f"Unknown operation type '{op.op}'"))

    validated_ops = _missing_hub_ops(validated_ops, target_dir=target_dir, path_exists=path_exists) + validated_ops

    _record_dangling_links(validated_ops, future_ref_whitelist=future_ref_whitelist,
                           path_exists=path_exists, cleared_links_out=cleared_links_out)
    _record_ambiguous_links(validated_ops, ambiguous_links_out)

    if grounding_out is not None and rejected_ops:
        # Grounding.check runs before the extractive/shape rejections that
        # follow it, so a rejected op has a row here; a score for a note that
        # never landed is a score for nothing.
        gone = {(r.op.path, r.op.heading) for r in rejected_ops}
        grounding_out[:] = [g for g in grounding_out if (g["path"], g["heading"]) not in gone]
    return validated_ops, rejected_ops
