# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Onboarding checks — pure diagnostics shared by `silica doctor` and `silica init`.

Each check reads config / env / filesystem / HTTP and returns a CheckResult.
No check mutates state and none makes a paid LLM completion call — key
presence and HTTP reachability only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import httpx

from silica.agent.providers import (
    LOCAL_RERANK_MODEL,
    PROVIDER_PRESETS,
    has_local_rerank,
    model_limits,
)
from silica.config import SilicaConfig
from silica.kernel.code import gitstate
from silica.kernel.scrub import scrub_credentials

_HTTP_TIMEOUT = 3.0

# One agentic turn is system prompt + tool schemas + history; below this the
# first turn already overflows. Ollama's own default window is 4096.
_MIN_OLLAMA_WINDOW = 8192


# A local server that is still loading answers 503 on every path, so the socket
# accepting proves nothing. Any other status means something served the request.
_LOADING = 503


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Literal["ok", "warn", "fail", "unknown"]
    detail: str
    hint: str = ""

    def __post_init__(self) -> None:
        # Scrubbed at composition, not per output surface: the doctor table,
        # the --json payload, the GUI's /health endpoint and the settings
        # panel's bug report all consume these fields, and a scrub call at
        # each renderer is the call the next surface forgets. httpx exception
        # text carries the full request URL, query included, so the endpoint
        # checks cannot promise these fields are clean on their own.
        object.__setattr__(self, "detail", scrub_credentials(self.detail))
        object.__setattr__(self, "hint", scrub_credentials(self.hint))


# `unknown` is deliberately distinct from `ok`: when a check cannot read the
# state it must say so softly, not imply the thing is live. Folding the two
# together is how a run reported "rerank ready" and marked rerank unreachable in
# the same session. It is not a failure either — nothing is known to be broken.


def check_chat_model(config: SilicaConfig) -> CheckResult:
    if not config.model.strip():
        return CheckResult(
            "chat model", "fail",
            "SILICA_MODEL is not set, and no provider key is exported",
            "run `silica init` — or serve the vault read-only with `silica mcp`, "
            "whose recall tools need no model",
        )
    key_env = PROVIDER_PRESETS.get(config.provider, {}).get("api_key_env")
    if key_env and not os.getenv(key_env):
        return CheckResult(
            "chat model", "fail",
            f"{config.model} — provider {config.provider} but {key_env} is unset",
            f"export {key_env} or run `silica init`",
        )
    return CheckResult("chat model", "ok", f"{config.model} via {config.provider}")


def check_chat_endpoint(config: SilicaConfig) -> CheckResult:
    if not config.model.strip():
        # Not probed because there was nothing to probe: a skip, not a
        # fallback. Warning on it every run trains the operator to skim the
        # column where a real degradation appears.
        return CheckResult("chat endpoint", "unknown", "skipped — no model configured")
    if config.provider in ("lmstudio", "ollama"):
        base_url = PROVIDER_PRESETS[config.provider]["base_url"]
    elif config.provider == "custom":
        base_url = config.provider_base_url
        if not base_url:
            return CheckResult(
                "chat endpoint", "fail",
                "custom provider but SILICA_PROVIDER_BASE_URL is unset",
                "run `silica init`",
            )
    else:
        return CheckResult(
            "chat endpoint", "unknown", f"{config.provider} (hosted, not probed)"
        )
    label = {"lmstudio": "LM Studio", "ollama": "Ollama"}.get(config.provider, "the endpoint")
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/models", timeout=_HTTP_TIMEOUT)
    except Exception:
        return CheckResult(
            "chat endpoint", "fail",
            f"{base_url} unreachable",
            f"start {label}, or switch provider with `silica init`",
        )
    if resp.status_code == _LOADING:
        return CheckResult(
            "chat endpoint", "unknown",
            f"{base_url} answering but still loading the model",
            "re-run `silica doctor` once the server reports ready",
        )
    return CheckResult("chat endpoint", "ok", f"{base_url} reachable")


def check_ollama_context(config: SilicaConfig) -> CheckResult:
    """Report the window silica pins per request, and warn when it cannot hold a turn.

    Ollama does not reject an oversized prompt — it drops the overflow and
    answers anyway (measured: a 6645-token prompt came back with
    prompt_eval_count=2051 and the tool definitions gone, HTTP 200, no warning).
    Silica pins num_ctx on every request so the runtime's 4096 default cannot
    cause that, which leaves two ways to end up under water: OLLAMA_NUM_CTX set
    too low by hand, or a model whose trained maximum is smaller than one turn.
    """
    window, _ = model_limits(config.provider, config.model)
    if not window:
        return CheckResult(
            # The check could not read the window. Nothing is known to be
            # wrong, and the detail already says "unknown".
            "ollama context", "unknown",
            f"{config.model} — window unknown (model not pulled, or Ollama unreachable)",
            f"`ollama pull {config.model.removeprefix('ollama/')}`, then re-run `silica doctor`",
        )
    if window < _MIN_OLLAMA_WINDOW:
        return CheckResult(
            "ollama context", "warn",
            f"{window} tokens — too small for one turn, Ollama discards the rest with no error",
            f"raise OLLAMA_NUM_CTX to {_MIN_OLLAMA_WINDOW} or more (costs VRAM); if the model's "
            "own trained maximum is the limit, use a model with a wider window",
        )
    return CheckResult("ollama context", "ok", f"{window} tokens pinned per request")


def check_vault(config: SilicaConfig) -> CheckResult:
    vault = config.vault_path.strip()
    if vault:
        p = Path(vault)
        if not p.is_dir():
            return CheckResult(
                "vault", "fail", f"{vault} does not exist",
                "fix SILICA_VAULT or run `silica init`",
            )
        if not os.access(p, os.W_OK):
            return CheckResult("vault", "fail", f"{vault} is not writable", "fix permissions")
        # Doctor may be handed a config that is not the active vault (the wizard
        # does exactly that), so the boundary comes from this path's own
        # manifest rather than from active_inbox_dir().
        from silica.kernel.vault_manifest import load_manifest, resolve_inbox_dir

        write_dir = load_manifest(str(p)).write_dir or ""
        inbox = resolve_inbox_dir(p, write_dir, config.inbox_dir)
        if inbox and not (p / inbox).is_dir():
            return CheckResult(
                "vault", "warn",
                f"{vault} ok, but inbox folder `{inbox}/` is missing",
                f"create `{inbox}/` inside the vault for nucleation",
            )
        return CheckResult("vault", "ok", vault)
    root = gitstate.find_repo_root(Path.cwd())
    if root is not None:
        # Same rule startup applies: the repo root is the vault, as-is. The old
        # "is this repo a vault *yet*" test predates that and reported a failure
        # for the very repo `silica` would have opened without asking.
        return CheckResult("vault", "ok", f"repo mode → {root}")
    return CheckResult(
        "vault", "fail",
        "SILICA_VAULT not set and this repo is not a Silica vault yet",
        "set SILICA_VAULT=/path/to/vault in .env, or run `silica init`",
    )


def check_embeddings(config: SilicaConfig) -> CheckResult:
    """Report the embeddings leg's actual state.

    Probes with a real /v1/embeddings call rather than comparing against the
    /models id list: llama-server (the default local runtime) reports the
    loaded gguf's file PATH as that id, not a friendly name, and ignores the
    requested `model` field on a single-model server — a literal id match
    false-positives on every llama-server setup even though embeddings work
    fine. Never "fail": relatedness degrades to the co-occurrence leg by design.
    """
    url = f"{config.embedding_base_url.rstrip('/')}/embeddings"
    try:
        resp = httpx.post(
            url,
            json={"model": config.embedding_model, "input": ["ping"]},
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code == _LOADING:
            # Still loading its weights (llama-server answers 503 on every
            # path meanwhile). The warn below would send the operator to edit
            # a config that is correct — this is a transient, not a rejection.
            return CheckResult(
                "embeddings", "unknown",
                f"{config.embedding_base_url} answering but still loading the model",
                "re-run `silica doctor` once the server reports ready",
            )
        resp.raise_for_status()
        data = resp.json().get("data") or []
        if not data or not data[0].get("embedding"):
            raise ValueError("no embedding vector in response")
    except Exception:
        return CheckResult(
            "embeddings", "warn",
            f"{config.embedding_base_url} unreachable or rejected `{config.embedding_model}`",
            "load the embedding model, or update SILICA_EMBEDDING_MODEL "
            "(dedup routing and /find fall back to co-occurrence)",
        )
    return CheckResult(
        "embeddings", "ok",
        f"{config.embedding_model} @ {config.embedding_base_url}",
    )


def check_rerank(config: SilicaConfig) -> CheckResult:
    """Report the rerank pass's actual state.

    Never "fail": recall degrades to the fused pool's order by design. It warns
    rather than staying silent because the failure mode is invisible — rerank
    just never runs, and the results look plausible.
    """
    if config.rerank_base_url and config.rerank_model:
        url = f"{config.rerank_base_url.rstrip('/')}/rerank"
        try:
            resp = httpx.post(
                url,
                json={"model": config.rerank_model, "query": "ping", "documents": ["ping"]},
                timeout=_HTTP_TIMEOUT,
            )
            if resp.status_code == _LOADING:
                # Still loading its weights. "unreachable" would send the
                # operator to start a server that is already starting.
                return CheckResult(
                    "rerank", "unknown",
                    f"{config.rerank_base_url} answering but still loading the model",
                    "re-run `silica doctor` once the reranker reports ready",
                )
            resp.raise_for_status()
        except Exception:
            # get_reranker (agent/providers.py) falls back to the in-process
            # extra per call when this endpoint is down — report that fallback
            # rather than a bare "unreachable" that reads as rerank being off.
            if has_local_rerank():
                return CheckResult(
                    "rerank", "warn",
                    f"{config.rerank_base_url} unreachable — using in-process fallback ({LOCAL_RERANK_MODEL})",
                    "start the reranker to use it instead of the in-process cross-encoder",
                )
            return CheckResult(
                "rerank", "warn",
                f"{config.rerank_base_url} unreachable",
                "start the reranker, or unset SILICA_RERANK_* and `pip install silica-agent[rerank]`",
            )
        return CheckResult(
            "rerank", "ok", f"{config.rerank_model} @ {config.rerank_base_url}",
        )
    if has_local_rerank():
        return CheckResult("rerank", "ok", f"in-process ({LOCAL_RERANK_MODEL})")
    return CheckResult(
        "rerank", "warn",
        "disabled (no cross-encoder available)",
        "`pip install silica-agent[rerank]` sharpens recall; LM Studio and Ollama cannot serve one",
    )


_LANG_SAMPLE_MAX_FILES = 30
_LANG_SAMPLE_PER_FILE_CHARS = 150
_LANG_SAMPLE_TOTAL_CHARS = 4000


def sample_vault_text(vault: str) -> str:
    """Deterministic, cheap sample of a vault's prose for language detection.

    Up to `_LANG_SAMPLE_MAX_FILES` `.md` files (sorted rglob — deterministic
    across runs/platforms), the first `_LANG_SAMPLE_PER_FILE_CHARS` characters
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
    the `/vault` info block in cli.py go through `detect_vault_language`
    below, which calls this — no duplicated sampling.
    """
    try:
        files = sorted(Path(vault).rglob("*.md"))[:_LANG_SAMPLE_MAX_FILES]
    except Exception:
        return ""
    parts: list[str] = []
    total = 0
    for f in files:
        if total >= _LANG_SAMPLE_TOTAL_CHARS:
            break
        try:
            chunk = f.read_text(encoding="utf-8", errors="ignore")[:_LANG_SAMPLE_PER_FILE_CHARS]
        except Exception:
            continue
        parts.append(chunk)
        total += len(chunk)
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
    from silica.kernel.vault_manifest import load_manifest

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


def check_language(config: SilicaConfig) -> CheckResult:
    """The vault's authoritative language (declared in vault.yaml, else
    detected) vs. the cooccurrence store's frozen language. A divergence is
    the signature of the historic bug that froze stores to "english" on
    non-English vaults — this is how existing users discover a store needs a
    `/cooccur` rebuild.

    Resolved from `config.vault_path` — never from the global CONFIG
    singleton — so a caller that just reconfigured (e.g. the init wizard
    building a fresh `SilicaConfig()` right after a vault switch) never
    compares the newly-chosen vault against a *different*, still-active
    vault's frozen store.
    """
    vault = config.vault_path.strip()
    if not vault:
        return CheckResult("language", "ok", "no vault — skipped")

    authority, store_lang, drift = language_status(vault)
    if authority is None:
        return CheckResult("language", "ok", "no notes yet")
    if store_lang is None:
        return CheckResult("language", "ok", f"language={authority}, no store frozen yet")
    if not drift:
        return CheckResult("language", "ok", f"language={authority}, store={store_lang}")
    return CheckResult(
        "language", "warn",
        f"language={authority}, store frozen={store_lang} — mismatch",
        "run `/cooccur --force` to rebuild the co-occurrence store in the vault's language",
    )


def check_manifest(config: SilicaConfig) -> CheckResult:
    from silica.kernel.vault_manifest import MANIFEST_REL, load_manifest
    from silica.sources.registry import ALL_ADAPTERS

    vault = config.vault_path.strip()
    if not vault:
        return CheckResult("vault manifest", "ok", "no vault — defaults apply")
    path = Path(vault) / MANIFEST_REL
    if not path.is_file():
        return CheckResult("vault manifest", "ok", "absent — retro-compatible defaults")
    m = load_manifest(vault)
    known = {a.name for a in ALL_ADAPTERS}
    unknown = [s for s in m.sources if s not in known]
    if unknown:
        return CheckResult(
            "vault manifest", "warn",
            f"unknown source(s) {unknown} in {MANIFEST_REL}",
            f"known sources: {sorted(known)}",
        )
    detail = f"sources={list(m.sources)}"
    return CheckResult("vault manifest", "ok", detail)


def check_quarantine(config: SilicaConfig) -> CheckResult:
    """Corrupt state files quarantined as *.corrupt.* — preserved, not lost."""
    from silica.kernel.recall.paths import index_dir_for

    roots = [Path(p) for p in (config.vault_path,) if p]
    roots.append(index_dir_for(config.vault_path or ""))
    # ~/.silica holds cross-vault state (undo_journal.db, checkpoints): its
    # quarantined copies were invisible to doctor, the only surface for them.
    roots.append(Path.home() / ".silica")
    found = [p.name for r in roots if r.exists() for p in sorted(r.glob("*.corrupt.*"))]
    if found:
        return CheckResult(
            "quarantine", "warn",
            f"{len(found)} corrupt state file(s) preserved: {', '.join(found)}",
            "inspect or delete; derived indexes rebuild via /cooccur",
        )
    return CheckResult("quarantine", "ok", "no quarantined state")


def check_okf(config: SilicaConfig) -> CheckResult:
    """Open Knowledge Format §11: the vault IS a bundle, or it says why not.

    Only what the user can act on raises the status. A file with no frontmatter
    at all (§11.1) is counted and reported but stays `ok`: Silica's write path
    never produces one, and in repo mode the vault is a source tree whose
    README and prompt templates are markdown by right — warning about those
    every run would be noise nobody can clear.
    """
    from silica.kernel.write.notetype import okf_conformance

    vault = config.vault_path.strip()
    if not vault:
        return CheckResult("OKF §11", "ok", "no vault — nothing to census")
    violations = okf_conformance(vault)
    if not violations:
        return CheckResult("OKF §11", "ok", "conformant bundle")
    by_clause: dict[str, int] = {}
    for v in violations:
        by_clause[v.clause] = by_clause.get(v.clause, 0) + 1
    detail = ", ".join(f"§{c}: {n}" for c, n in sorted(by_clause.items()))
    actionable = [v for v in violations if v.clause != "11.1"]
    if not actionable:
        return CheckResult("OKF §11", "ok", f"typed bundle, {detail} without frontmatter")
    hint = ""
    if any(v.clause == "11.2" for v in actionable):
        hint = "run `uv run python scripts/backfill_notetype.py` to stamp the missing types"
    if any(v.clause == "11.3" for v in actionable):
        hint = (hint + "; " if hint else "") + "rename any `index`/`log` note by hand"
    sample = ", ".join(v.path for v in actionable[:3])
    return CheckResult(
        "OKF §11", "warn",
        f"{len(actionable)} non-conformant note(s) — {detail} (e.g. {sample})",
        hint,
    )


HOOK_SNIPPET = """\
"hooks": {
  "SessionEnd": [{"hooks": [{"type": "command", "command": "silica capture"}]}],
  "PreCompact": [{"hooks": [{"type": "command", "command": "silica capture"}]}]
}"""


def check_capture_hook(config: SilicaConfig) -> CheckResult:
    """Session capture is opt-in and hand-registered: say so when it is absent.

    Silica never edits `.claude/settings.json` itself — a tool that rewrites
    another tool's config is a support burden, and the hook is three lines.
    """
    # Claude Code resolves project settings from the session's cwd, not from
    # the vault: for an adopted source tree the two differ, and looking only
    # under the vault warned about a hook that was registered and firing.
    roots = [Path.home(), Path.cwd()]
    vault = config.vault_path.strip()
    if vault:
        roots.append(Path(vault))
    candidates = [root / ".claude" / name for root in roots
                  for name in ("settings.json", "settings.local.json")]
    for path in candidates:
        try:
            if "silica capture" in path.read_text(encoding="utf-8"):
                return CheckResult("session capture", "ok", f"hook registered in {path}")
        except OSError:
            continue
    return CheckResult(
        "session capture", "warn",
        "no `silica capture` hook — sessions are not captured",
        f"add to .claude/settings.json:\n{HOOK_SNIPPET}",
    )


def check_session_capture(config: SilicaConfig) -> CheckResult:
    """Silica's own conversations: opt-in, and never notes.

    Off is a legitimate choice, so this never warns — it only says the knob
    exists, and where the memory goes when it is on.
    """
    if getattr(config, "capture_sessions", False):
        return CheckResult(
            "own sessions", "ok",
            "captured to the WAL; /nucleate distills them into episodic memory",
        )
    return CheckResult(
        "own sessions", "ok", "not captured",
        "set SILICA_CAPTURE_SESSIONS=true to remember your own sessions "
        "(facts only, never notes — promotion is what writes to the vault)",
    )


def _guarded(name: str, check: Callable[[SilicaConfig], CheckResult],
             config: SilicaConfig) -> CheckResult:
    """Run one check, degrading it in place instead of taking down the report.

    The HTTP checks guard themselves, but the filesystem and parsing ones do
    not: a single OSError on a vault the user just unmounted used to abort the
    whole run, including the twelve checks that would have answered. A check
    that raises is a failure, not a new state.

    `name` is the row name the check itself uses when it answers, so a consumer
    keying on `results[].name` finds the row in exactly the degraded run the
    guard exists to report — deriving it from `check.__name__` gave "manifest"
    for a row every healthy run calls "vault manifest".
    """
    try:
        return check(config)
    except Exception as exc:  # noqa: BLE001 — the doctor must survive any check
        return CheckResult(name, "fail", f"check raised: {type(exc).__name__}: {exc}")


def run_checks(config: SilicaConfig) -> list[CheckResult]:
    checks: list[tuple[str, Callable[[SilicaConfig], CheckResult]]] = [
        ("chat model", check_chat_model),
        ("chat endpoint", check_chat_endpoint),
        # Ollama-only: the silent-truncation trap is specific to it.
        *([("ollama context", check_ollama_context)] if config.provider == "ollama" else []),
        ("vault", check_vault),
        ("vault manifest", check_manifest),
        ("language", check_language),
        ("embeddings", check_embeddings),
        ("rerank", check_rerank),
        ("quarantine", check_quarantine),
        ("OKF §11", check_okf),
        ("session capture", check_capture_hook),
        ("own sessions", check_session_capture),
    ]
    return [_guarded(name, c, config) for name, c in checks]


def has_failures(results: list[CheckResult]) -> bool:
    return any(r.status == "fail" for r in results)


# `?` and not `⚠`: a warning says a fallback was taken, unknown says nothing
# could be read. Give them the same glyph and the real warnings drown, which is
# the whole reason routine lines must not shout.
_STATUS_GLYPH = {"ok": ("✓", "green"), "warn": ("⚠", "yellow"),
                 "fail": ("✗", "red"), "unknown": ("?", "dim")}


def report_payload(results: list[CheckResult]) -> dict:
    """Machine-readable mirror of `render_report` — how the agent reads its own health.

    Deliberately a flat mirror of the dataclass rather than a shaped schema:
    once something routes on the field names, growing a shape is a breaking
    change, and `CheckResult` is already the contract. Credentials are already
    scrubbed — CheckResult redacts its own fields at composition.
    """
    return {
        "results": [
            {"name": r.name, "status": r.status, "detail": r.detail, "hint": r.hint}
            for r in results
        ],
        "ok": not has_failures(results),
    }


def render_report(results: list[CheckResult]) -> None:
    from rich.markup import escape
    from rich.table import Table

    from silica.ui.console import CONSOLE

    table = Table(show_header=False, box=None, padding=(0, 1))
    for r in results:
        glyph, style = _STATUS_GLYPH[r.status]
        # escape: detail/hint carry data (paths, model ids, `silica-agent[rerank]`), and
        # rich reads a bare [word] as a style tag and swallows it.
        hint = f"[dim]→ {escape(r.hint)}[/]" if r.hint else ""
        table.add_row(f"[{style}]{glyph}[/]", f"[bold]{r.name}[/]", escape(r.detail), hint)
    CONSOLE.print()
    CONSOLE.print(table)
    CONSOLE.print()
