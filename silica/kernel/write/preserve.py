# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What a write may not drop: the lines and values a note already had
(ADR-0093).

`lost(before, after)` is the rule, a pure function: None when `after` kept
every non-blank body line of `before` in order and in the context it renders
in, and every frontmatter value; otherwise the first thing lost, named.
Inserting is always free. It keeps lines and values, not meaning: an inserted
line that contradicts the note passes (ADR-0093, Consequences).

`check_write` is the same rule at the driver's write site. Until 2026-12-15 it
only logs (`REFUSE = False`); on that date the owner reads the WARNING lines
and either sets `REFUSE = True` (at least 1 true catch, at most 3 false alarms)
or removes the check (ADR-0093 decision 5). A caller that must remove names
why with a `Loss`, from the closed set below, as a keyword on the one write;
`tests/test_preserve_pins.py` pins every such keyword in the tree.

Nothing at module level imports the driver: the rule is shared with the
golden probe and the CI fixture test, which must run without a vault.
"""
from __future__ import annotations

import enum
import hashlib
import logging
import re
import sys
import time
from collections import Counter
from contextvars import ContextVar
from types import FrameType

from silica.kernel.write import frontmatter

logger = logging.getLogger(__name__)

# The switch of decision 5. Log-only until the owner's review on 2026-12-15.
REFUSE = False

# Keys Silica itself rewrites on any write, named from the code: `last
# modified` (templates.ensure_system_floor), `agent` (templates._stamp_agent),
# `review` (set by bulk.render_write, cleared by templates.drop_props),
# `code_ref` (replaced by templates.stamp_documents). `type` and `AI` are only
# ever added when absent, so they need no exemption.
SYSTEM_KEYS = frozenset({"last modified", "agent", "review", "code_ref"})

# The sections learn.plans writes and rewrites on an edit (plans._GENERATED,
# compared by test_preserve_rule): copied, not imported, because plans pulls
# the whole Learn stack in and this module must stay importable alone.
PLAN_SECTIONS = frozenset({"objectives", "tree", "steps"})

# The patch headers written until 2026-09-04 (templates.PROVENANCE_HEADER_
# PREFIXES, compared by test_preserve_rule). resolve_contested prunes one left
# empty by the callout it moved, so RESOLVE relaxes them with the callouts;
# the suite's census of would-refuse lines found it (test_superseded.py).
PROVENANCE_HEADERS = ("## Additional notes", "## Note aggiuntive")


class Loss(enum.Enum):
    """Why one write may remove what the rule would keep. Closed: a new member
    or a new call site is an amendment to ADR-0093, which the pin test
    enforces. Never a field of an op, never parsed from tool arguments or
    model output: a field there would let a model write `"loss": "PERSON"`."""

    PERSON = "person"      # the person typed it (the GUI editor, /verify)
    RESTORE = "restore"    # bytes captured before an earlier write go back
    STAGED = "staged"      # inbox and sources/ leaves, rebuilt from their source file
    DERIVED = "derived"    # the code wiki, derived from the code
    MIGRATE = "migrate"    # /migrate --write, confirmed per run and journaled
    PLAN = "plan"          # Learn's re-scope rebuilds what the plan generated
    RESOLVE = "resolve"    # resolve_contested files callouts under ## Superseded
    CITE = "cite"          # CLEANUP rewrites footnote references and definitions
    UNLINK = "unlink"      # the sweep drops related: entries that resolve nowhere
    RENAME = "rename"      # a move rewrites link targets in its referrers


# Reasons that switch the whole rule off for their one write. STAGED holds only
# on a path under the inbox or sources/, which check_write tests at run time.
_RELAX_ALL = frozenset({Loss.PERSON, Loss.RESTORE, Loss.STAGED, Loss.DERIVED, Loss.MIGRATE})
_LOSS_KEYS = {
    Loss.PLAN: frozenset({"scope", "target", "objectives", "by", "restrict", "refused"}),
    Loss.RESOLVE: frozenset({"contested", "contradictions"}),
    Loss.UNLINK: frozenset({"related"}),
}


class PreservationError(RuntimeError):
    """A write refused before a byte landed (only once REFUSE is True).

    Deliberately not VerifyMismatchError: that one tells commit_note_atomic
    the bytes landed and a revert is due; this one means nothing was written."""

    def __init__(self, path: str, reason: str):
        super().__init__(f"refused to write {path}: it would lose {reason}")
        self.path = path
        self.reason = reason


# ---------------------------------------------------------------- the rule

# [[T]], [[T|A]], [[T\|A]] (autolink's escaped pipe inside a table row),
# [[T#H]], and the embed form. The target is lazy so `T\|A` splits at the
# escaped pipe instead of swallowing the backslash.
_WIKILINK = re.compile(r"(!?)\[\[([^\]|#\n]*?)(#[^\]|\n]*)?(?:\\?\|([^\]\n]*))?\]\]")
_MDLINK = re.compile(r"(!?)\[([^\]\n]*)\]\(([^)\n]*)\)")
_FN_REF = re.compile(r"\[\^[^\]\s]+\](?!:)")
_FN_DEF = re.compile(r"^\[\^[^\]\s]+\]:")
_TOP_KEY = re.compile(r"^([^\s#\-:][^:\n]*):")
_H2 = re.compile(r"^##\s+(.*?)\s*#*\s*$")
# What a link target reduces to under RENAME: one placeholder, so a retarget
# passes and a dropped link still does not.
_TARGET = "\x01"


def lf(text: str) -> str:
    """`text` on LF, as Python's universal newlines read it: the one input
    every composer is written for (decision 8)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _reduce(line: str, loss: Loss | None) -> str:
    """A line as the reader sees it: a wikilink becomes its alias or its
    target, an embed keeps its `!`."""
    def _wl(m: re.Match) -> str:
        if m.group(4):
            return m.group(1) + m.group(4)
        return m.group(1) + (_TARGET if loss is Loss.RENAME else m.group(2).strip())

    line = _WIKILINK.sub(_wl, line)
    if loss is Loss.RENAME:
        line = _MDLINK.sub(lambda m: m.group(1) + m.group(2), line)
    if loss is Loss.CITE:
        line = _FN_REF.sub("", line)
    return line


def _lines(text: str, loss: Loss | None, *, context: bool = True) -> list[str]:
    """The reduced non-blank lines of `text`, each prefixed with the context
    it renders in: inside a code fence (c), inside a %% or <!-- --> comment
    (h), or neither. A line moved into a fence or a comment no longer matches
    its old self, which is how a whole-body wrap is caught."""
    out: list[str] = []
    fence = pct = html = False
    section: str | None = None
    for raw in text.split("\n"):
        ctx = ("c" if fence else "") + ("h" if (pct or html) else "")
        stripped = raw.lstrip()
        if stripped.startswith(("```", "~~~")):
            fence = not fence
        elif not fence:
            if raw.count("%%") % 2:
                pct = not pct
            if "<!--" in raw and "-->" not in raw[raw.rfind("<!--"):]:
                html = True
            elif html and "-->" in raw:
                html = False
            if loss is Loss.PLAN and (h := _H2.match(raw)):
                section = h.group(1).casefold()
        if loss is Loss.PLAN and (section is None or section in PLAN_SECTIONS):
            continue  # the preamble and the generated sections are the plan's to rebuild
        if loss is Loss.CITE and _FN_DEF.match(raw):
            continue
        line = _reduce(raw, loss).rstrip()
        if not line.strip():
            continue
        if loss is Loss.RESOLVE and (line.lstrip().startswith(">")
                                     or line.startswith(PROVENANCE_HEADERS)):
            continue  # the resolved callouts move, with their emptied header
        out.append(f"{ctx}\x00{line}" if context else line)
    return out


def _first_missing(old: list[str], new: list[str]) -> str | None:
    """The first line of `old` not matched, in order, in `new`."""
    j = 0
    for line in old:
        while j < len(new) and new[j] != line:
            j += 1
        if j == len(new):
            return line
        j += 1
    return None


def _first_missing_any_order(old: list[str], new: list[str]) -> str | None:
    have = Counter(new)
    for line in old:
        if have[line] <= 0:
            return line
        have[line] -= 1
    return None


def _shown(line: str) -> str:
    ctx, _, text = line.rpartition("\x00")
    where = {"c": " (in a code fence)", "h": " (in a comment)",
             "ch": " (in a fenced comment)"}.get(ctx, "")
    return f"{text.strip()[:80]!r}{where}"


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _as_list(v) -> list:
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _alias_list(v) -> list[str]:
    # The spellings frontmatter.aliases_of reads: a list, or a CSV scalar.
    if _empty(v):
        return []
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    return [str(x).strip() for x in _as_list(v)]


def _renamed(v):
    if isinstance(v, str):
        return _reduce(v, Loss.RENAME)
    if isinstance(v, list):
        return [_renamed(x) for x in v]
    if isinstance(v, dict):
        return {k: _renamed(x) for k, x in v.items()}
    return v


def _dup_keys(raw: str | None) -> set[str]:
    seen: set[str] = set()
    dup: set[str] = set()
    for line in (raw or "").split("\n"):
        if m := _TOP_KEY.match(line):
            key = m.group(1).strip()
            (dup if key in seen else seen).add(key)
    return dup


def _raw_lines(raw: str, exempt: frozenset, loss: Loss | None) -> list[str]:
    """An unparseable block's lines, minus those of an exempt key (the key
    line and the indented or dashed lines under it)."""
    keep: list[str] = []
    current = None
    for line in raw.split("\n"):
        if m := _TOP_KEY.match(line):
            current = m.group(1).strip()
        if current in exempt:
            continue
        keep.append(line)
    return _lines("\n".join(keep), loss, context=False)


def _values_lost(b_data: dict, a_data, a_raw, exempt: frozenset, loss: Loss | None) -> str | None:
    new = a_data if isinstance(a_data, dict) else None
    for key, old in b_data.items():
        if key in exempt or _empty(old):
            continue
        if new is None:
            return "frontmatter: no longer parses" if a_raw is not None else "frontmatter: removed"
        if key in ("alias", "aliases"):
            have = _alias_list(new.get("aliases")) + _alias_list(new.get("alias"))
            if all(x in have for x in _alias_list(old)):
                continue
            return f"frontmatter: key {key!r} lost a value"
        if key not in new:
            return f"frontmatter: key {key!r} removed"
        cur = new[key]
        if loss is Loss.RENAME:
            old, cur = _renamed(old), _renamed(cur)
        if old == cur:
            continue
        if isinstance(old, (list, tuple)) or isinstance(cur, (list, tuple)):
            items = _as_list(cur)
            if all(x in items for x in _as_list(old)):
                continue
        return f"frontmatter: key {key!r} changed"
    return None


def lost(before: str, after: str, *, loss: Loss | None = None, free=()) -> str | None:
    """None when `after` keeps what `before` had; else the first thing lost.

    `loss` relaxes what its reason names (ADR-0093 decision 6); `free` names
    frontmatter keys this one write may change or drop. Line terminators are
    never compared."""
    if loss in _RELAX_ALL:
        return None
    before, after = lf(before), lf(after)
    if before == after:
        return None
    b_data, b_raw, b_body = frontmatter.split(before)
    a_data, a_raw, a_body = frontmatter.split(after)
    exempt = SYSTEM_KEYS | frozenset(free) | (_LOSS_KEYS.get(loss, frozenset()) if loss else frozenset())

    dups = _dup_keys(a_raw) - _dup_keys(b_raw)
    if dups:
        return f"frontmatter: key {sorted(dups)[0]!r} now appears twice"
    if b_raw is not None:
        if b_data is not None:
            why = _values_lost(b_data, a_data, a_raw, exempt, loss)
            if why:
                return why
        else:
            miss = _first_missing(_raw_lines(b_raw, exempt, loss),
                                  _raw_lines(a_raw or "", exempt, loss))
            if miss is not None:
                return f"frontmatter (unparsed): line lost: {_shown(miss)}"

    old, new = _lines(b_body, loss), _lines(a_body, loss)
    miss = (_first_missing_any_order if loss is Loss.RESOLVE else _first_missing)(old, new)
    if miss is not None:
        return f"body: line lost: {_shown(miss)}"
    return None


# ---------------------------------------------------------------- the write site

# When the running chat turn began, set by run_agent (agent/loop.py) for an
# interactive turn. A chat delete journaled after it is "in this turn"; the
# journal row is the membership record, so silica_delete needs no hook here.
_TURN_STARTED: ContextVar[float | None] = ContextVar("preserve_turn_started", default=None)


def begin_turn() -> None:
    _TURN_STARTED.set(time.time())


def _staged_path(path: str) -> bool:
    from silica.kernel.recall.paths import is_inbox_path, is_source_leaf

    try:
        return bool(is_inbox_path(path) or is_source_leaf(path))
    except Exception as exc:
        # An unreadable manifest must not make a staging write judged as a
        # declared one: off the path test, STAGED is judged like no reason.
        logger.debug("preservation: staged-path test failed for %s: %s", path, exc)
        return False


def _caller() -> tuple[str, str]:
    """The first frame outside the driver and this module: the site that
    asked for the write, which is what the review needs to name."""
    f: FrameType | None = sys._getframe(2)
    while f is not None:
        mod = f.f_globals.get("__name__", "")
        if not (mod.startswith("silica.driver") or mod == __name__):
            return mod, f.f_code.co_name
        f = f.f_back
    return "?", "?"


def _vault() -> str | None:
    from silica.config import CONFIG

    return (getattr(CONFIG, "vault_path", "") or "").strip() or None


def _untouched_draft(path: str, prior: str) -> bool:
    """Decision 9: the prior is byte-identical to what the agent last landed
    (the newest journal inverse for the path carries its hash) and still
    carries `review:`. The grant is the journal state; `review:` only narrows
    it, so a stamp from anywhere cannot reopen a note the person edited."""
    data, _raw, _body = frontmatter.split(prior)
    if not isinstance(data, dict) or "review" not in data:
        return False
    from silica.kernel.write.undo_journal import get_undo_journal

    try:
        post = get_undo_journal().newest_post_hash(path, _vault())
    except Exception as exc:
        # No journal, no grant: the write is judged like any other.
        logger.debug("preservation: journal unreadable for %s: %s", path, exc)
        return False
    return post is not None and post == hashlib.sha256(prior.encode("utf-8")).hexdigest()


def check_write(path: str, before: str | None, after: str, *,
                loss: Loss | None = None, free=()) -> None:
    """The rule at the write site, before a byte lands. Logs; raises
    PreservationError only once REFUSE is True."""
    if before is None:
        return
    if loss is Loss.STAGED and not _staged_path(path):
        loss = None  # a refined code note outside the inbox is the person's
    why = lost(before, after, free=free)
    if why is None:
        return
    if loss is not None:
        relaxed = lost(before, after, loss=loss, free=free)
        if relaxed is None:
            logger.debug("preservation: %s under %s: %s", path, loss.name, why)
            return
        why = relaxed
    if _untouched_draft(path, lf(before)):
        logger.debug("preservation: %s re-authored as an untouched draft", path)
        return
    mod, fn = _caller()
    if REFUSE:
        logger.warning("preservation: refused %s from %s.%s: %s", path, mod, fn, why)
        raise PreservationError(path, why)
    logger.warning("preservation: would refuse %s from %s.%s: %s", path, mod, fn, why)


def check_recreate(path: str, after: str, *, loss: Loss | None = None, free=()) -> None:
    """A create at a path the chat deleted in this same turn is judged
    against the body the delete saved (decision 4). Out of scope by
    construction: a `user:/delete` (the lookup reads chat deletes only), a
    create in a later turn, an MCP client's delete and create (no turn)."""
    started = _TURN_STARTED.get()
    if started is None:
        return
    from silica.kernel.write.undo_journal import get_undo_journal

    try:
        journal = get_undo_journal()
        hit = journal.last_chat_delete(_vault(), path)
        if hit is None or hit[2] < started:
            return
        saved = next((inv.prior_content for inv, _h in journal.inverses_for(hit[0])
                      if inv.prior_content is not None), None)
    except Exception as exc:
        # The journal is the only before-state there is; without it the
        # create is what it was before this record, unchecked.
        logger.debug("preservation: re-create lookup failed for %s: %s", path, exc)
        return
    check_write(path, saved, after, loss=loss, free=free)


# ---------------------------------------------------------------- line endings

def crlf_dominant(raw: bytes) -> bool:
    """CRLF when the bytes hold more `\\r\\n` than lone `\\n` (decision 8)."""
    crlf = raw.count(b"\r\n")
    return crlf > raw.count(b"\n") - crlf


def encode(content: str, crlf: bool) -> str:
    """`content` (composed on LF) with the prior's terminator put back."""
    content = lf(content)
    return content.replace("\n", "\r\n") if crlf else content
