# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""`silica init` — interactive setup wizard. Writes .env, then runs the doctor checks."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Callable

from silica.config import SilicaConfig
from silica.kernel import gitstate
from silica.kernel.vault_manifest import MANIFEST_REL
from silica.onboarding.checks import has_failures, render_report, run_checks
from silica.ui.banner import print_banner
from silica.ui.console import CONSOLE
from silica.ui.style import GLYPHS

# Optional leading `#` so merge_env can uncomment-and-fill a `# KEY=default`
# line seeded from .env.example, not just rewrite an already-active key.
_KEY_RE = re.compile(r"^\s*#?\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

# (key env var, default model, key prompt) per hosted preset.
_HOSTED = {
    "openrouter": ("OPENROUTER_API_KEY", "openrouter/anthropic/claude-sonnet-5", "OpenRouter API key"),
    "gemini": ("GEMINI_API_KEY", "gemini/gemini-2.5-flash", "Google Gemini API key"),
    "openai": ("OPENAI_API_KEY", "openai/gpt-4o", "OpenAI API key"),
    "groq": ("GROQ_API_KEY", "groq/llama-3.3-70b-versatile", "Groq API key"),
    "deepseek": ("DEEPSEEK_API_KEY", "deepseek/deepseek-chat", "DeepSeek API key"),
    "mistral": ("MISTRAL_API_KEY", "mistral/mistral-large-latest", "Mistral API key"),
    "xai": ("XAI_API_KEY", "xai/grok-2-latest", "xAI (Grok) API key"),
}

_EMBED_KEYS = ("SILICA_EMBEDDING_MODEL", "SILICA_EMBEDDING_BASE_URL", "SILICA_EMBEDDING_API_KEY")


class BackRequested(Exception):
    """Raised by _ask when the user types `back` — the wizard driver rewinds
    to the previous step that actually asked something."""

_LANG_PROMPT = (
    "Force a language for distilled notes? "
    "[Enter = no, follow the source language]"
)
# Bare language names only: letters and spaces. Rejects punctuation (a colon
# above all — see _ask_language) that would corrupt the raw YAML the answer
# is embedded into.
_LANG_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z ]*$")
# YAML 1.1 boolean literals: they'd pass the letters-only regex above but
# parse as `True`/`False`, which `_parse_conventions` folds to None — the
# user would believe they forced a language but silently didn't.
_LANG_ANSWER_REJECT = {"y", "n", "yes", "no", "true", "false", "on", "off"}


def _find_env_example(repo_root: Path | str | None) -> Path | None:
    """Locate `.env.example` to seed a fresh `.env`: the vault repo root first,
    then this package's own checkout. `None` when neither exists (a future
    non-editable install) — the caller then falls back to a minimal write."""
    candidates: list[Path] = []
    if repo_root is not None:
        candidates.append(Path(repo_root) / ".env.example")
    candidates.append(Path(__file__).resolve().parents[2] / ".env.example")
    return next((c for c in candidates if c.is_file()), None)


def _endpoint_model_ids(base_url: str) -> list[str]:
    """Model ids advertised by an OpenAI-compatible `/models` endpoint, best-effort
    ([] on any error). Powers LM Studio autodetect and the local-embeddings
    suggestion, mirroring _ollama_installed_models / check_chat_endpoint."""
    import httpx

    try:
        data = httpx.get(f"{base_url.rstrip('/')}/models", timeout=3.0).json()
        return [m["id"] for m in data.get("data", []) if m.get("id")]
    except Exception:
        return []


def merge_env(existing: str, updates: dict[str, str]) -> str:
    """Update KEY=VALUE lines in place — uncommenting a `# KEY=default` line when
    KEY is collected — preserve every other line untouched, and append keys that
    were not present. Never deletes a line it did not write."""
    pending = dict(updates)
    out: list[str] = []
    for line in existing.splitlines():
        m = _KEY_RE.match(line)
        if m and m.group(1) in pending:
            key = m.group(1)
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(line)
    for key, value in pending.items():
        out.append(f"{key}={value}")
    text = "\n".join(out)
    return text + "\n" if text else ""


def _ask(
    input_fn: Callable[[str], str],
    prompt: str,
    default: str = "",
    *,
    secret: bool = False,
) -> str:
    shown = f"…{default[-4:]}" if (secret and default) else default
    suffix = f" [{shown}]" if default else ""
    try:
        # `→` gutter marks every question with the TUI's arrow glyph (same one
        # render_report uses for hints). Plain text: input() ignores markup.
        raw = input_fn(f"  {GLYPHS['arrow']} {prompt}{suffix}: ").strip()
    except (EOFError, StopIteration):
        # EOF (Ctrl+D) or an exhausted scripted input — treat like Ctrl+C.
        raise KeyboardInterrupt
    if raw.lower() == "back":
        raise BackRequested
    return raw or default


def _ollama_installed_models() -> list[str]:
    """Tags installed in the local Ollama, best-effort ([] if it's down/absent).

    Lets the wizard offer a pick-list instead of asking the user to recall an
    exact tag. Never raises — a down Ollama just means no suggestions.
    """
    import httpx

    from silica.agent.providers import PROVIDER_PRESETS

    base = PROVIDER_PRESETS["ollama"]["base_url"].removesuffix("/v1")
    try:
        data = httpx.get(f"{base}/api/tags", timeout=3.0).json()
        return [m["name"] for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def _section(glyph_key: str, title: str, n: int, total: int) -> None:
    """Flat-gutter step header in the TUI's brand vocabulary: glyph + title in
    bold brand cyan, a dim `· n/N` counter riding after it."""
    CONSOLE.print()
    CONSOLE.print(f"  [bold brand.cyan]{GLYPHS[glyph_key]} {title}[/]  [dim]· {n}/{total}[/]")


def _rerank_extra_present() -> bool:
    """Whether the optional [rerank] extra is importable. Guarded find_spec
    (via has_local_rerank) — never raises."""
    try:
        from silica.agent.providers import has_local_rerank
        return has_local_rerank()
    except Exception:
        return False


def _rerank_install_cmd() -> str:
    """Exact install command for the [rerank] extra, matching how this process
    was installed (uv-managed interpreter → uv pip, else pip)."""
    pip = "uv pip" if "uv" in Path(sys.executable).resolve().parts else "pip"
    return f"{pip} install 'silica[rerank]'"


def _ask_language(input_fn: Callable[[str], str]) -> str:
    """Ask the "force a language" question and return an answer safe to embed
    raw into vault.yaml: either a plausible bare language name or "" (no
    language forced — same as Enter).

    Both call sites below splice the answer directly into unquoted YAML.
    Left unvalidated: "yes"/"no"/"true" etc. parse as YAML booleans that
    `_parse_conventions` folds to None (the user believes they forced a
    language but silently didn't), and any other stray punctuation — a colon
    above all — can break the surrounding YAML, degrading the WHOLE manifest
    (in repo mode this silently drops sources/overlay too). Anything that
    isn't a bare name is treated as no answer rather than risking either
    failure mode.
    """
    raw = _ask(input_fn, _LANG_PROMPT).strip()
    if not raw:
        return ""
    if not _LANG_NAME_RE.match(raw) or raw.lower() in _LANG_ANSWER_REJECT:
        CONSOLE.print(
            f"  [yellow]'{raw}' doesn't look like a language name — skipping "
            "(no language forced; distiller follows the source language).[/]"
        )
        return ""
    return raw


def _run_wizard_inner(
    input_fn: Callable[[str], str],
    env_path: Path,
    advanced: bool = False,
) -> int:
    updates: dict[str, str] = {}
    # Cross-step state shared by the step closures below. Each step owns a
    # fixed set of `updates` keys and pops them on entry, so a re-run after
    # `back` never leaves stale leftovers.
    state: dict = {"advanced": advanced, "provider": "", "high_value": True, "write": False}
    repo_root = gitstate.find_repo_root(env_path.parent)

    print_banner()
    CONSOLE.print()
    CONSOLE.print(
        "  [bold]Interactive setup[/]  [dim]· Enter accepts the shown default"
        " · type back to redo the previous step[/]"
    )

    def total() -> int:
        return 6 if state["advanced"] else 5

    def step_mode() -> bool:
        if advanced:  # `silica init --advanced` skips the question
            return False
        answer = ""
        while answer not in ("essential", "e", "advanced", "a"):
            answer = _ask(input_fn, "Setup mode — essential or advanced", "essential").lower()
        state["advanced"] = answer in ("advanced", "a")
        return True

    def step_vault() -> bool:
        # Repo mode (docs/silica/) when inside a git repo, else explicit path.
        # An Obsidian-vault repo (.obsidian/) is adopted verbatim instead.
        updates.pop("SILICA_VAULT", None)
        _section("vault", "Vault", 1, total())
        use_repo_mode = False
        if repo_root is not None:
            from silica.kernel.paths import is_obsidian_vault, repo_mode_vault

            repo_vault = Path(repo_root) if is_obsidian_vault(repo_root) else repo_mode_vault(repo_root)
            exists = "exists" if repo_vault.is_dir() else "will be created"
            answer = _ask(
                input_fn,
                f"Git repo detected — use repo mode? vault = {repo_vault} ({exists}) [y/n]",
                "y",
            )
            if answer.lower() in ("y", "yes"):
                use_repo_mode = True
                repo_vault.mkdir(parents=True, exist_ok=True)
                manifest = repo_vault / MANIFEST_REL
                if not manifest.exists():
                    # Declared capabilities (ADR-0014): repo-mode vault wants the
                    # codebase overlay and the code source active.
                    lang_answer = _ask_language(input_fn)
                    content = "sources: [prose, code]\noverlay: codebase\n"
                    if lang_answer:
                        # cooccurrence_lang (stemmer/stopwords) is separate from
                        # conventions.language (distiller translation intent). Pin
                        # both from the one answer so the co-occurrence store never
                        # falls back to fragile auto-detection.
                        content += f"cooccurrence_lang: {lang_answer.lower()}\n"
                        content += f"conventions:\n  language: {lang_answer}\n"
                    manifest.write_text(content, encoding="utf-8")
        if not use_repo_mode:
            while True:
                path = _ask(input_fn, "Vault path (existing directory)")
                resolved = Path(path).expanduser() if path else None
                if resolved is not None and resolved.is_dir():
                    updates["SILICA_VAULT"] = str(resolved)
                    break
                CONSOLE.print(f"  [red]{GLYPHS['err']} Not a directory — try again.[/]")
            # The design's language question is unscoped to repo mode ("init asks
            # whether to force a language"): an explicit-path vault with no
            # vault.yaml yet must be asked too. Unlike repo mode there is no other
            # content due to be written for this vault, so Enter writes nothing —
            # a vault.yaml wouldn't otherwise exist, and conventions is the only
            # thing this question could ever put in it. An existing manifest is
            # never touched, and the question is skipped entirely in that case.
            manifest = resolved / MANIFEST_REL
            if not manifest.exists():
                lang_answer = _ask_language(input_fn)
                if lang_answer:
                    # Pin cooccurrence_lang (stemmer) alongside conventions.language
                    # (distiller) — two separate axes, one answer. See repo-mode note.
                    manifest.write_text(
                        f"cooccurrence_lang: {lang_answer.lower()}\n"
                        f"conventions:\n  language: {lang_answer}\n",
                        encoding="utf-8",
                    )
        return True

    def step_provider() -> bool:
        # The hosted PROVIDER_PRESETS entries that need a key, plus `custom`
        # for any other OpenAI-compatible URL (vLLM, llama.cpp, ...).
        for key in (
            "SILICA_PROVIDER", "SILICA_MODEL", "SILICA_PROVIDER_BASE_URL",
            "SILICA_PROVIDER_API_KEY", *(v[0] for v in _HOSTED.values()),
        ):
            updates.pop(key, None)
        _section("model", "Chat provider", 2, total())
        from silica.agent.providers import PROVIDER_PRESETS
        provider = ""
        while provider not in ("lmstudio", "ollama", "custom", *_HOSTED):
            provider = _ask(
                input_fn,
                "Chat provider — lmstudio or ollama (local), custom (any OpenAI-compatible URL), "
                "or hosted: " + ", ".join(_HOSTED),
                "lmstudio",
            ).lower()
        updates["SILICA_PROVIDER"] = provider
        state["provider"] = provider
        if provider in _HOSTED:
            key_env, default_model, key_prompt = _HOSTED[provider]
            model = _ask(input_fn, "Model id", default_model)
            key = ""
            while not key:
                key = _ask(input_fn, key_prompt, os.getenv(key_env, ""), secret=True)
            updates[key_env] = key
        elif provider == "custom":
            base_url = ""
            while not base_url:
                base_url = _ask(input_fn, "Base URL (OpenAI-compatible, e.g. http://localhost:8000/v1)")
            updates["SILICA_PROVIDER_BASE_URL"] = base_url
            # Local servers usually ignore the key but the OpenAI SDK demands non-empty.
            updates["SILICA_PROVIDER_API_KEY"] = _ask(
                input_fn, "API key [Enter for none / local]", "dummy-key", secret=True
            )
            model = ""
            while not model:
                model = _ask(input_fn, "Model id served at that URL")
        elif provider == "ollama":
            installed = _ollama_installed_models()
            prompt = (
                f"Ollama model id (installed: {', '.join(installed)})"
                if installed else "Ollama model id (e.g. llama3.2)"
            )
            default = installed[0] if installed else ""
            model = ""
            while not model:
                model = _ask(input_fn, prompt, default)
        else:  # lmstudio — probe /models like the Ollama branch does with tags.
            loaded = _endpoint_model_ids(PROVIDER_PRESETS["lmstudio"]["base_url"])
            prompt = (
                f"LM Studio model id (loaded: {', '.join(loaded)})"
                if loaded else "Model id as loaded in LM Studio (e.g. qwen3-30b)"
            )
            default = loaded[0] if loaded else ""
            model = ""
            while not model:
                model = _ask(input_fn, prompt, default)
        updates["SILICA_MODEL"] = model
        return True

    def step_gate() -> bool:
        # Essential-only: one question covering embeddings + reranker. `n`
        # jumps straight to write. Advanced asks both steps directly.
        if state["advanced"]:
            return False
        answer = ""
        while answer not in ("y", "yes", "n", "no"):
            answer = _ask(
                input_fn,
                "Configure high-value options now? Embeddings (semantic search, "
                "dedup) and reranker (better recall) [y/n]",
                "y",
            ).lower()
        state["high_value"] = answer in ("y", "yes")
        if not state["high_value"]:
            # A `back` may have left embedding keys from an earlier `y` pass;
            # the embeddings step won't run again to clear them.
            for key in _EMBED_KEYS:
                updates.pop(key, None)
        return True

    def step_embeddings() -> bool:
        # Optional; skipping degrades gracefully.
        if not (state["advanced"] or state["high_value"]):
            return False
        for key in _EMBED_KEYS:
            updates.pop(key, None)
        _section("think", "Embeddings", 3, total())
        from silica.agent.providers import PROVIDER_PRESETS
        defaults = SilicaConfig()
        provider = state["provider"]
        answer = _ask(
            input_fn,
            "Configure embeddings? `skip` degrades dedup//find to co-occurrence [y/skip]",
            "y",
        )
        if answer.lower() in ("skip", "s", "n", "no"):
            CONSOLE.print(
                "  [yellow]Embeddings skipped. Dedup routing and /find will not run; "
                "relatedness falls back to co-occurrence.[/]"
            )
            return True
        # Reuse the chat endpoint when it is local — it can usually serve
        # embeddings too, so a good setup needs no separate server.
        local = provider in ("lmstudio", "ollama")
        local_base = PROVIDER_PRESETS[provider]["base_url"] if local else defaults.embedding_base_url
        # ponytail: the "embed" substring is the ceiling — covers nomic-embed-text,
        # text-embedding-*; a served embedder without "embed" in its id needs the
        # explicit prompts below. Upgrade path: probe each model's capabilities.
        candidate = next(
            (m for m in _endpoint_model_ids(local_base) if "embed" in m.lower()), ""
        ) if local else ""
        if candidate and _ask(
            input_fn, f"Use {candidate} at {local_base} for embeddings? [y/n]", "y"
        ).lower() in ("y", "yes"):
            updates["SILICA_EMBEDDING_MODEL"] = candidate
            updates["SILICA_EMBEDDING_BASE_URL"] = local_base
            updates["SILICA_EMBEDDING_API_KEY"] = defaults.embedding_api_key
        else:
            updates["SILICA_EMBEDDING_MODEL"] = _ask(
                input_fn, "Embedding model", defaults.embedding_model
            )
            updates["SILICA_EMBEDDING_BASE_URL"] = _ask(
                input_fn, "Embedding base URL", local_base
            )
            updates["SILICA_EMBEDDING_API_KEY"] = _ask(
                input_fn, "Embedding API key", defaults.embedding_api_key
            )
        return True

    def step_rerank() -> bool:
        # In-process cross-encoder via the [rerank] extra. The wizard never
        # installs anything — it prints the exact command and moves on.
        # SILICA_RERANK_* (an externally served reranker) is advanced-only.
        if not (state["advanced"] or state["high_value"]):
            return False
        _section("gear", "Reranker", 4, total())
        if _rerank_extra_present():
            # markup=False: the literal "[rerank]" would otherwise parse as a rich tag.
            CONSOLE.print(
                f"  {GLYPHS['ok']} Reranker active ([rerank] extra installed) — "
                "nothing to configure.",
                markup=False,
            )
            return False
        answer = ""
        while answer not in ("y", "yes", "n", "no"):
            answer = _ask(
                input_fn,
                "Enable the in-process reranker? ~2GB of weights download on first use [y/n]",
                "n",
            ).lower()
        if answer in ("y", "yes"):
            CONSOLE.print(f"  {GLYPHS['arrow']} Install it, then re-run [bold]silica doctor[/]:")
            CONSOLE.print(f"      {_rerank_install_cmd()}", markup=False)
        return True

    def step_worker() -> bool:
        if not state["advanced"]:
            return False
        updates.pop("SILICA_WORKER_MODEL", None)
        updates.pop("SILICA_WORKER_PROVIDER", None)
        _section("worker", "Advanced options", 5, total())
        provider = state["provider"]
        suggestion = ""
        if provider == "lmstudio":
            from silica.agent.providers import PROVIDER_PRESETS
            ids = _endpoint_model_ids(PROVIDER_PRESETS["lmstudio"]["base_url"])
            suggestion = ids[0] if ids else ""
        elif provider == "ollama":
            ids = _ollama_installed_models()
            suggestion = ids[0] if ids else ""
        hint = f", e.g. {suggestion}" if suggestion else ""
        model = _ask(
            input_fn,
            f"Worker model for background tasks (dedup, refiner){hint} "
            "[Enter = inherit main model]",
        )
        if model:
            updates["SILICA_WORKER_MODEL"] = model
            if provider == "ollama":
                # lmstudio is already the worker-provider fallback; ollama must be pinned.
                updates["SILICA_WORKER_PROVIDER"] = "ollama"
        return True

    def step_git() -> bool:
        if not state["advanced"]:
            return False
        updates.pop("SILICA_GIT_COMMIT", None)
        answer = None
        while answer not in ("", "y", "yes", "n", "no"):
            answer = _ask(
                input_fn,
                "Git auto-commit every vault write — the undo safety net? "
                "y = auto, n = off [Enter = leave off]",
            ).lower()
        if answer:
            updates["SILICA_GIT_COMMIT"] = "auto" if answer in ("y", "yes") else "off"
        return True

    def step_websearch() -> bool:
        if not state["advanced"]:
            return False
        updates.pop("SILICA_TAVILY_API_KEY", None)
        key = _ask(
            input_fn, "Tavily API key — enables /web-search [Enter = skip]", secret=True
        )
        if key:
            updates["SILICA_TAVILY_API_KEY"] = key
        return True

    def step_pdf() -> bool:
        if not state["advanced"]:
            return False
        updates.pop("SILICA_PDF_PROVIDER", None)
        updates.pop("SILICA_PDF_OCR_LANG", None)
        answer = None
        while answer not in ("", "mineru", "docling", "opendataloader"):
            answer = _ask(
                input_fn,
                "PDF converter — mineru, docling, or opendataloader [Enter = mineru]",
            ).lower()
        if answer:
            updates["SILICA_PDF_PROVIDER"] = answer
        if answer == "docling":
            updates["SILICA_PDF_OCR_LANG"] = _ask(
                input_fn, "OCR languages for docling (comma-separated)", "en,it,fr,de,es"
            )
        return True

    def step_ext_rerank() -> bool:
        # Only for a reranker the user already serves; all three keys or none.
        if not state["advanced"]:
            return False
        for key in ("SILICA_RERANK_BASE_URL", "SILICA_RERANK_MODEL", "SILICA_RERANK_API_KEY"):
            updates.pop(key, None)
        answer = ""
        while answer not in ("y", "yes", "n", "no"):
            answer = _ask(
                input_fn,
                "Point at an external reranker you already serve "
                "(llama.cpp --reranking, Infinity, Jina)? [y/n]",
                "n",
            ).lower()
        if answer in ("y", "yes"):
            updates["SILICA_RERANK_BASE_URL"] = _ask(
                input_fn, "Reranker base URL", "http://localhost:1235/v1"
            )
            updates["SILICA_RERANK_MODEL"] = _ask(
                input_fn, "Reranker model id", "bge-reranker-v2-m3"
            )
            updates["SILICA_RERANK_API_KEY"] = _ask(
                input_fn, "Reranker API key", "lm-studio", secret=True
            )
        return True

    def step_write() -> bool:
        _section("arrow", "Write configuration", total(), total())
        CONSOLE.print(
            f"  {len(updates)} key(s) → [bold]{env_path}[/]: "
            f"[dim]{', '.join(sorted(updates))}[/]"
        )
        state["write"] = _ask(input_fn, "Write? [y/n]", "y").lower() in ("y", "yes")
        return True

    # Driver: run steps in order; `back` rewinds to the previous step that
    # actually asked something (auto-skipped steps are transparent).
    steps = [
        step_mode, step_vault, step_provider, step_gate, step_embeddings,
        step_rerank, step_worker, step_git, step_websearch, step_pdf,
        step_ext_rerank, step_write,
    ]
    asked = [False] * len(steps)
    i = 0
    while i < len(steps):
        try:
            asked[i] = steps[i]()
        except BackRequested:
            j = i - 1
            while j >= 0 and not asked[j]:
                j -= 1
            if j < 0:
                CONSOLE.print("  [dim]Already at the first question.[/]")
            else:
                i = j
            continue
        i += 1

    if not state["write"]:
        CONSOLE.print(f"  [dim]{GLYPHS['err']} Aborted — nothing written.[/]")
        return 1
    # Fresh .env: seed from .env.example so every knob ships documented, with the
    # collected keys filled in. Existing .env: merge in place, untouched otherwise.
    if env_path.exists():
        base = env_path.read_text()
    else:
        example = _find_env_example(repo_root)
        base = example.read_text(encoding="utf-8") if example else ""
    env_path.write_text(merge_env(base, updates))
    CONSOLE.print(f"  [green]{GLYPHS['ok']} Wrote {env_path}[/]")

    # Doctor checks against the values just chosen.
    CONSOLE.print()
    CONSOLE.print(f"  [bold brand.cyan]{GLYPHS['run']} Checking your setup[/]")
    os.environ.update(updates)
    results = run_checks(SilicaConfig())
    render_report(results)

    CONSOLE.print()
    CONSOLE.print(f"  [bold brand.cyan]{GLYPHS['arrow']} Next steps[/]")
    CONSOLE.print("  [dim]·[/] Run [bold]silica[/] — try ingesting a file or asking a question.")
    CONSOLE.print(
        f"  [dim]·[/] Every other option lives documented in [bold]{env_path}[/] — edit anytime."
    )
    CONSOLE.print(
        "  [dim]·[/] Re-run [bold]silica init[/] anytime — it updates values in place "
        "and never deletes your edits."
    )
    return 1 if has_failures(results) else 0


def run_wizard(
    input_fn: Callable[[str], str] = input,
    env_path: Path | None = None,
    advanced: bool = False,
) -> int:
    cwd = Path.cwd()
    if env_path is None:
        repo_root = gitstate.find_repo_root(cwd)
        env_path = (Path(repo_root) if repo_root else cwd) / ".env"
    try:
        return _run_wizard_inner(input_fn, env_path, advanced=advanced)
    except KeyboardInterrupt:
        CONSOLE.print(
            f"\n  [dim]{GLYPHS['err']} Aborted — nothing written beyond what was already confirmed.[/]"
        )
        return 1
