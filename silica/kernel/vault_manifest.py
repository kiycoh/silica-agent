# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Vault manifest — declared capabilities per vault (ADR-0014).

`<vault>/vault.yaml` declares which source adapters participate, the active
domain overlay (ADR-0005 pack name) and the co-occurrence language. This is
composition, not taxonomy: there is no vault *type*. Absence of the file ⇒
retro-compatible defaults (prose always on; code on iff the vault sits
inside a git repo) — no migration required. Cached like kernel/overlay.py;
reset on /vault switch.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from silica.kernel import paths

logger = logging.getLogger(__name__)

MANIFEST_REL = "vault.yaml"


@dataclass(frozen=True)
class VaultConventions:
    """Per-vault authoring conventions — single source for prompt + linter.

    Consumed by `prep_delegation.render_prompt` ({LANGUAGE}/{MAX_TAGS}
    placeholders) and `ofm.ofm_lint` (LIMITS/CALLOUT_TYPES resolution).
    max_tags/extra_callouts/max_lines/max_chars default to today's hardcoded
    values, so a vault without a `conventions:` block behaves bit-identically
    to before this existed for those fields.

    `language: None` (the default) means "follow the source document's
    language" — resolved per-note downstream via `kernel.language.detect`.
    A declared non-empty string means "force/translate everything into this
    language" — an explicit declaration is translation intent.

    `reply_language` is a *different* axis: the language Silica speaks in chat
    (button/slash-command turns included), independent of note content. None
    ⇒ the call site falls back to `language`, then to follow-the-user.
    """

    language: str | None = None
    reply_language: str | None = None
    max_tags: int = 3
    extra_callouts: tuple[str, ...] = ()
    # ADR-0021 F1b: free-form authoring rules injected into the distiller prompt
    # ({CAPTURE_RULES} placeholder). "" ⇒ placeholder renders empty, bit-identical
    # to before. This is where a vault declares spatial/format capture conventions
    # (F3), e.g. "Record every measurement in metric with the imperial in parens".
    capture_rules: str = ""
    # Distill profile: named lens (rubric/quality/examples fragments) spliced
    # into the distiller prompt contract. "" ⇒ "default", which renders
    # bit-identically to the pre-split prompt. SILICA_DISTILL_PROFILE env
    # overrides this for eval A/Bs.
    distill_profile: str = ""
    wiki_dir: str = ""  # landing dir for /wiki notes; "" ⇒ vault root
    # Frontmatter templates (2026-07-17 spec): None ⇒ built-in template_spoke
    # layout — a vault with no config behaves bit-identically to before.
    default_template: str | None = None
    templates_dir: str = "templates"
    # ADR-0021: None ⇒ no episodic key enforcement (bit-identical to today).
    # Only meaningful on the MEMORY vault's manifest; other vaults ignore it.
    episodic_keys: "EpisodicKeySchema | None" = None


@dataclass(frozen=True)
class EpisodicKeySchema:
    """Declared grammar of episodic keys (ADR-0021).

    Owned by the MEMORY vault's manifest (the episodic store's home), never
    by the vault active at capture: one store, one schema. Enforcement is
    structural and write-time (see `episodic.enforce_key_schema`).
    """

    prefixes: tuple[str, ...] = ("user", "assistant")
    default_prefix: str = "user"
    max_depth: int = 3


DEFAULT_CONVENTIONS = VaultConventions()


@dataclass(frozen=True)
class VaultManifest:
    sources: tuple[str, ...]
    overlay: str | None = None
    cooccurrence_lang: str | None = None
    conventions: VaultConventions = DEFAULT_CONVENTIONS
    # Write boundary: the only subtree of the vault Silica may create, patch,
    # move or delete notes in. "" ⇒ the vault root (in place, today's Obsidian
    # behaviour and the default for a vault with no manifest). A relative subdir
    # ⇒ reads stay vault-wide, writes are confined there, so everything outside
    # is read-only context. Top-level rather than under `conventions:` on
    # purpose: this is a boundary the framework enforces against the model, and
    # a malformed sibling block must never widen it (`_parse_conventions` folds
    # the whole block to defaults). None ⇒ declared but unresolvable; the
    # activation seam refuses the vault instead of silently writing everywhere.
    write_dir: str | None = ""


# There is deliberately no function mapping a directory to "the vault it really
# means". The vault is the directory you launched in or named, full stop, and a
# resolver that could answer something else is what made the vault a thing you
# had to reconstruct rather than read off the screen. Where notes may be written
# inside it is the separate `write_dir` axis (`onboarding.adopt`).


def _safe_rel_dir(value) -> str | None:
    """Normalize a user-authored vault-relative dir; None when it escapes.

    Trust boundary: vault.yaml is hand-written and these paths reach the read
    and write paths — an absolute path or a traversal would scatter notes
    outside the vault, invisible to the index, /undo and snapshots. "" and "."
    both mean the vault root. Shared by write_dir, wiki_dir and templates_dir
    so the rule is stated once.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip().replace("\\", "/")
    if not raw or raw == ".":
        return ""
    if raw.startswith("/"):
        return None
    parts = [p for p in raw.split("/") if p and p != "."]
    if ".." in parts or ":" in parts[0]:
        return None
    return "/".join(parts)


def within(rel_path: str, root: str) -> bool:
    """True when vault-relative `rel_path` sits inside vault-relative dir `root`.

    `root=""` is the vault root, which contains everything. Segment-wise so
    `docs/silica` never matches `docs/silicate/x.md`.
    """
    if not root:
        return True
    prefix = root.strip("/").lower()
    p = (rel_path or "").replace("\\", "/").strip("/").lower()
    return p == prefix or p.startswith(prefix + "/")


def default_sources(vault: str | Path) -> tuple[str, ...]:
    out = ["prose"]
    try:
        if vault and paths.repo_root_for(vault) is not None:
            out += ["code", "notebook"]
    except Exception:
        pass
    return tuple(out)


def _parse_conventions(raw: dict) -> VaultConventions:
    """Parse the optional `conventions:` block; malformed/missing ⇒ defaults (soft)."""
    conv_raw = raw.get("conventions")
    if conv_raw is None:
        return DEFAULT_CONVENTIONS
    if not isinstance(conv_raw, dict):
        logger.warning("vault.yaml: `conventions` must be a mapping — using defaults")
        return DEFAULT_CONVENTIONS

    # Absent/malformed (non-string, empty or whitespace-only) -> None ("follow
    # the source"). A declared non-blank string passes through unchanged
    # (translation intent) — {LANGUAGE} must always get a concrete name.
    language = conv_raw.get("language")
    if isinstance(language, str) and language.strip():
        language = language.strip()
    else:
        language = None

    reply_language = conv_raw.get("reply_language")
    if isinstance(reply_language, str) and reply_language.strip():
        reply_language = reply_language.strip()
    else:
        reply_language = None

    max_tags = conv_raw.get("max_tags")
    if not (isinstance(max_tags, int) and not isinstance(max_tags, bool) and max_tags > 0):
        max_tags = DEFAULT_CONVENTIONS.max_tags

    capture_rules = conv_raw.get("capture_rules")
    capture_rules = capture_rules.strip() if isinstance(capture_rules, str) else ""

    distill_profile = conv_raw.get("distill_profile")
    distill_profile = distill_profile.strip() if isinstance(distill_profile, str) else ""

    extra_callouts = conv_raw.get("extra_callouts")
    if isinstance(extra_callouts, list) and all(isinstance(c, str) for c in extra_callouts):
        extra_callouts = tuple(c.lower() for c in extra_callouts)
    else:
        extra_callouts = DEFAULT_CONVENTIONS.extra_callouts

    wiki_dir = _safe_rel_dir(conv_raw.get("wiki_dir")) if "wiki_dir" in conv_raw else ""
    if wiki_dir is None:
        logger.warning("vault.yaml: conventions.wiki_dir must be a relative "
                       "path inside the vault — ignoring %r", conv_raw.get("wiki_dir"))
        wiki_dir = ""

    default_template = conv_raw.get("default_template")
    if isinstance(default_template, str) and default_template.strip():
        default_template = default_template.strip()
    else:
        default_template = None

    templates_dir = (
        _safe_rel_dir(conv_raw.get("templates_dir")) if "templates_dir" in conv_raw else ""
    )
    if templates_dir is None:
        logger.warning("vault.yaml: conventions.templates_dir must be a relative "
                       "path inside the vault — ignoring %r", conv_raw.get("templates_dir"))
        templates_dir = ""
    if not templates_dir:
        templates_dir = "templates"

    episodic_keys = None
    ek_raw = conv_raw.get("episodic_keys")
    if isinstance(ek_raw, dict):
        defaults = EpisodicKeySchema()
        prefixes = ek_raw.get("prefixes")
        if not (isinstance(prefixes, list) and prefixes
                and all(isinstance(p, str) and p.strip() for p in prefixes)):
            prefixes = list(defaults.prefixes)
        default_prefix = ek_raw.get("default_prefix")
        if not (isinstance(default_prefix, str) and default_prefix.strip()):
            default_prefix = defaults.default_prefix
        max_depth = ek_raw.get("max_depth")
        if not (isinstance(max_depth, int) and not isinstance(max_depth, bool)
                and max_depth > 0):
            max_depth = defaults.max_depth
        episodic_keys = EpisodicKeySchema(
            prefixes=tuple(p.strip() for p in prefixes),
            default_prefix=default_prefix.strip(),
            max_depth=max_depth,
        )
    elif ek_raw is not None:
        logger.warning("vault.yaml: `episodic_keys` must be a mapping — "
                       "no key schema (enforcement off)")

    return VaultConventions(
        language=language,
        reply_language=reply_language,
        max_tags=max_tags,
        extra_callouts=extra_callouts,
        capture_rules=capture_rules,
        distill_profile=distill_profile,
        wiki_dir=wiki_dir,
        default_template=default_template,
        templates_dir=templates_dir,
        episodic_keys=episodic_keys,
    )


def load_manifest(vault: str | Path) -> VaultManifest:
    """Parse <vault>/vault.yaml; absent or malformed ⇒ defaults (soft)."""
    defaults = VaultManifest(sources=default_sources(vault))
    if not vault:
        return defaults
    path = Path(vault) / MANIFEST_REL
    if not path.is_file():
        return defaults
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("vault.yaml: parse failed (%s) — using defaults", exc)
        return defaults
    if not isinstance(raw, dict):
        logger.warning("vault.yaml: expected a mapping — using defaults")
        return defaults

    sources = raw.get("sources")
    if isinstance(sources, list) and sources and all(isinstance(s, str) for s in sources):
        src = tuple(sources)
    else:
        if sources is not None:
            logger.warning("vault.yaml: `sources` must be a non-empty string list — using defaults")
        src = defaults.sources

    overlay = raw.get("overlay")
    lang = raw.get("cooccurrence_lang")

    # Absent ⇒ "" (vault root, in place). Declared-but-unresolvable ⇒ None, and
    # unlike every other field that does NOT degrade to the default: the default
    # is the widest write scope, so a typo would silently hand the whole vault
    # to the writer. `cli` refuses to activate the vault instead.
    write_dir = "" if raw.get("write_dir") is None else _safe_rel_dir(raw.get("write_dir"))
    if write_dir is None:
        logger.warning(
            "vault.yaml: `write_dir` must be a relative path inside the vault — got %r",
            raw.get("write_dir"),
        )

    conventions = _parse_conventions(raw)
    if write_dir and not conventions.wiki_dir:
        # `/wiki` writes through commit_derived, which bypasses the validate gate.
        # Defaulting its landing dir to the boundary (instead of the vault root)
        # is what keeps derived notes inside it without a second check.
        conventions = replace(conventions, wiki_dir=write_dir)
    elif write_dir and not within(conventions.wiki_dir, write_dir):
        # wiki_dir is a landing dir for writes, so it cannot sit outside the
        # write boundary. Collapse rather than reject: /wiki still works, just
        # inside the declared subtree.
        logger.warning(
            "vault.yaml: conventions.wiki_dir %r is outside write_dir %r — using %r",
            conventions.wiki_dir, write_dir, write_dir,
        )
        conventions = replace(conventions, wiki_dir=write_dir)

    return VaultManifest(
        sources=src,
        overlay=overlay if isinstance(overlay, str) and overlay else None,
        cooccurrence_lang=lang if isinstance(lang, str) and lang else None,
        conventions=conventions,
        write_dir=write_dir,
    )


_cached: VaultManifest | None = None


def reset_manifest_cache() -> None:
    """Invalidate the cache. Use in tests and after /vault switch."""
    global _cached
    _cached = None


def get_active_manifest() -> VaultManifest:
    global _cached
    if _cached is None:
        from silica.config import CONFIG

        _cached = load_manifest((getattr(CONFIG, "vault_path", "") or "").strip())
    return _cached


# A path no note can have (validate sanitizes filenames and prunes hidden dirs),
# so every write op is rejected while a broken declaration stands.
_UNRESOLVABLE_WRITE_DIR = ".invalid-write-dir"


def active_write_dir() -> str:
    """Vault-relative write boundary for the active vault; "" ⇒ the whole vault.

    An unresolvable declaration (None) never degrades to "": the /vault seam
    refuses activation, and answering with an impossible path keeps any caller
    that skipped that seam (GUI, MCP) from writing vault-wide.
    """
    declared = get_active_manifest().write_dir
    return declared if declared is not None else _UNRESOLVABLE_WRITE_DIR


def active_inbox_dir() -> str:
    """Vault-relative inbox root for the active vault; "" ⇒ no inbox configured.

    The inbox is Silica's own staging area, so it belongs inside the write
    boundary like everything else Silica creates. Composed here rather than read
    raw off `CONFIG.inbox_dir` because that field knows nothing about
    `write_dir`: every caller that built a path from it was dropping an `Inbox/`
    at the root of the user's source tree, outside the one folder writes are
    supposed to land in. An unresolvable boundary propagates, same as above.
    """
    from silica.config import CONFIG

    inbox = (getattr(CONFIG, "inbox_dir", "") or "").replace("\\", "/").strip("/")
    if not inbox:
        return ""
    write_dir = active_write_dir()
    return f"{write_dir}/{inbox}" if write_dir else inbox


def apply_manifest_to_config() -> None:
    """Manifest determines CONFIG fields the environment did not set (env
    wins). Symmetric on purpose: a vault that declares no overlay clears a
    previous vault's overlay on /vault switch instead of leaking it."""
    from silica.config import CONFIG

    m = get_active_manifest()
    if os.getenv("SILICA_DOMAIN") is None:
        CONFIG.domain = m.overlay
    if os.getenv("SILICA_COOCCURRENCE_LANG") is None:
        # "auto" mirrors the config-level default for this field (per-store
        # detection, frozen at build — see kernel/cooccurrence.py). A vault
        # without a declared cooccurrence_lang must NOT be silently pinned to
        # english.
        CONFIG.cooccurrence_lang = m.cooccurrence_lang or "auto"
