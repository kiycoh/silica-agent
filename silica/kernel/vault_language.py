# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Which language a vault is in: what it declares, what its notes say, and what its
co-occurrence store froze to.

A fact about the vault, read by the doctor, by /vault and by the speech APIs, so it
does not live in the doctor. Not `kernel/text/language.py`, which imports nothing of
Silica's and says so: this reads the manifest and the store.
"""
from __future__ import annotations

import re

from silica.kernel.base import frontmatter


_LANG_SAMPLE_MAX_FILES = 30


_LANG_SAMPLE_PER_FILE_CHARS = 150


_LANG_SAMPLE_TOTAL_CHARS = 4000


# `AI: true` anywhere in a frontmatter head — the system floor stamps it as a
# top-level key, and the sample only needs a cheap screen, not a YAML parse.
_AI_FLAG_RE = re.compile(r"^AI:\s*true\s*$", re.MULTILINE)


def sample_vault_text(vault: str) -> str:
    """Deterministic, cheap sample of a vault's prose for language detection.

    Up to `_LANG_SAMPLE_MAX_FILES` notes (`layout.iter_notes`, in path order, so
    deterministic across runs/platforms), the first `_LANG_SAMPLE_PER_FILE_CHARS` characters
    of each, concatenated and capped at `_LANG_SAMPLE_TOTAL_CHARS`. The
    per-file cap is kept small (well under total/max_files) so the budget is
    actually SPREAD across the file cap rather than exhausted by the first
    handful of alphabetically-sorted files — a minority-language head (e.g.
    a lone "AAA notes.md") must not drown out the vault's real majority
    language, which only shows up once later files get sampled too. Returns
    "" when the vault has no readable `.md` files. Degrades on any
    filesystem error instead of raising — matches this module's
    pure-diagnostic contract.

    Single seam for this sampling logic: both `check_language` (doctor) and
    the `/vault` info block in commands/vault.py go through `detect_vault_language`
    below, which calls this — no duplicated sampling.
    """
    from silica.kernel.base.layout import is_vault_artifact, iter_notes
    from silica.kernel.base.manifest import load_manifest, resolve_done_dir

    try:
        # The vault's own walk, so `.trash/`, `node_modules/` and what
        # .silicaignore names never vote: a sorted rglob put the trash ahead of
        # every note, and 30 deleted notes filled the file cap. scope="vault"
        # keeps the verbatim source leaves the sample has always read.
        files = iter_notes(vault, scope="vault")
    except Exception:
        return ""  # nothing to sample: detection answers None, as for an empty vault
    # The agent's own output never votes on the vault's language: 13 generated
    # English notes flipped a human-Italian vault to `english`, and doctor then
    # proposed rebuilding the co-occurrence store in English — freezing the
    # error. Excluded: root artifacts (GRAPH_REPORT.md sorts before every note
    # and is English scaffolding), the conversion archive under `done/` (the
    # input's language, not the vault's), and any note stamped `AI: true`.
    try:
        write_dir = load_manifest(vault).write_dir or ""
    except Exception:
        write_dir = ""
    done_prefix = (resolve_done_dir(vault, write_dir) or "done").casefold() + "/"
    parts: list[str] = []
    total = 0
    sampled = 0
    for rel, f in files:
        if sampled >= _LANG_SAMPLE_MAX_FILES or total >= _LANG_SAMPLE_TOTAL_CHARS:
            break
        if is_vault_artifact(rel) or rel.casefold().startswith(done_prefix):
            continue
        try:
            head = f.read_text(encoding="utf-8", errors="ignore")[:2000]
        except Exception:
            continue
        # `head` may cut a long block short: its whole text is then the block.
        span = frontmatter.block(head)
        if head.startswith("---\n") and _AI_FLAG_RE.search(head[: span[1] if span else len(head)]):
            continue
        chunk = head[:_LANG_SAMPLE_PER_FILE_CHARS]
        parts.append(chunk)
        total += len(chunk)
        sampled += 1
    return "".join(parts)[:_LANG_SAMPLE_TOTAL_CHARS]


def detect_vault_language(vault: str) -> str | None:
    """Cheap, deterministic dominant-language detection for `vault`.

    None when there is nothing to sample (no `.md` files, or all unreadable)
    — callers treat that as "no notes yet". Never raises.
    """
    if not vault:
        return None
    sample = sample_vault_text(vault)
    if not sample.strip():
        return None
    from silica.kernel.text import language

    return language.detect(sample)


def frozen_store_language(vault: str) -> str | None:
    """Read `vault`'s persisted cooccurrence store's frozen `lang` field, if
    a store exists on disk for THIS vault.

    Thin pass-through to `kernel.cooccurrence.frozen_lang` — this module
    owns no on-disk store schema knowledge; the store's own module does.
    Resolved from the `vault` argument, never from the global CONFIG
    singleton, so a caller comparing a specific (possibly non-active) vault
    never cross-checks a different vault's store. None when no store file
    exists yet, or on any read/parse error (degrade, never raise — inherited
    from the accessor this delegates to).

    Direct leg import — allowlisted in tests/test_relatedness_boundary.py:
    metadata-only read via the public accessor, no store construction.
    """
    from silica.kernel.recall.cooccurrence import frozen_lang

    return frozen_lang(vault)


def declared_language(vault: str) -> str | None:
    """The language `vault` DECLARES in its `vault.yaml` (`cooccurrence_lang`),
    or None when it declares none — or declares the `auto` sentinel, meaning
    "detect me". A declaration is authority: it is the language the
    co-occurrence store is (or should be) frozen to, so it SUPERSEDES the
    stopword heuristic. A frontmatter-heavy sample fools `detect` into
    "english" (the bundled english stoplist matches `last:`/`related:`/`null`
    scaffolding), but a vault that declares italian is italian.

    Resolved from the `vault` argument, never the global CONFIG singleton —
    same contract as frozen_store_language above.
    """
    from silica.kernel.base.manifest import load_manifest

    lang = load_manifest(vault).cooccurrence_lang
    return lang if lang and lang != "auto" else None


def language_status(vault: str) -> tuple[str | None, str | None, bool]:
    """`(authority, store, drift)` for `vault` — the single seam behind both
    the doctor's `check_language` and the `/vault` info block.

    authority = the declared language if any, else the heuristically detected
    dominant language (None when there is nothing to sample). store = the
    frozen co-occurrence store language (None if no store on disk yet). drift =
    both known and differing — the signal that `/cooccur --force` is needed to
    rebuild the store in the authoritative language.
    """
    authority = declared_language(vault) or detect_vault_language(vault)
    store = frozen_store_language(vault) if authority else None
    return authority, store, bool(authority and store and authority != store)


def reply_language_for(vault: str) -> str | None:
    """The language a surface with no message of the person's to follow
    speaks for `vault`: the speech APIs (ui/web/stt_routes.speech_lang).

    Explicit conventions win (`reply_language`, else `language`); the fallback
    is the vault's own authority (declared `cooccurrence_lang`, else detected
    from the human notes). The chat used it too until 2026-10-16 and now reads
    the conventions alone (session.seed.reply_language): it has the person's messages.
    """
    if not vault:
        return None
    from silica.kernel.base.manifest import load_manifest

    conv = load_manifest(vault).conventions
    return conv.reply_language or conv.language or language_status(vault)[0]
