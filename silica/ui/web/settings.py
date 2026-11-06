# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What the web settings panel may show, and what changing a row costs.

One table, two readers — `GET /settings` renders it and `POST /settings` applies
a row — so the surface and the writer cannot drift apart.

Admission rule (docs/specs/settings-panel.md §3): a row is a setting only when the
user can answer it without running a benchmark. Most of config.py is not that.
`sim_threshold_*`, `dedup_scan_k`, the `episodic_*` family and
`cooccur_bm25` are research levers with a measured verdict behind them; offering
them in a GUI reopens that verdict for someone with no harness to re-run the
gate, and a vault degraded by `sim_threshold_high = 0.95` gives no sign of it.
They stay env vars.

Persistence is live *and* durable. Live is free: nearly every consumer reads the
CONFIG singleton at call time rather than capturing it at import. Durable means
writing the key into the .env that would win at the next boot — which is not
always the user-level one, because config.py layers project-then-user with
`override=False`. Same reason a key the shell already exported is reported
`locked`: no write to any .env can outrank it, so the control is disabled rather
than accepting an edit we know evaporates.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

from silica import SHELL_ENV
from silica.config import (
    CONFIG,
    HOSTED_PROVIDERS,
    PROVIDER_PREFIXES,
    USER_ENV,
    claim_env,
    hosted,
)
from silica.onboarding import settings_ops


@dataclass(frozen=True)
class Row:
    """One line of the panel.

    ``key`` is the env var written to the .env; ``attr`` the CONFIG attribute
    mutated live (empty when the value only lives in the environment, as the
    hosted API keys do). ``kind`` picks the control: toggle, text (with a
    ``<datalist>`` when options are known), enum (a closed set, so a real
    ``<select>``), secret, int, or readonly.

    ``warn`` non-empty marks an *invalidating* row: applying it degrades the
    vault unless a repair runs, so the panel confirms first and names the
    consequence. ``models_of`` names the endpoint whose /models list feeds the
    row's suggestions.
    """
    key: str
    attr: str
    label: str
    kind: str
    help: str = ""
    options: tuple[str, ...] = ()
    warn: str = ""
    models_of: str = ""
    curated: tuple[str, ...] = field(default=())


# Rows the confirm lane owns: applying them needs a server-side sequence, not an
# assignment. Everything else in `warn` confirms in the browser and posts here.
VAULT_KEY = "SILICA_VAULT"
EMBED_KEYS = ("SILICA_EMBEDDING_MODEL", "SILICA_EMBEDDING_BASE_URL")

_EMBED_WARN = "changing this invalidates every stored vector"

# The rows a hosted account sees and may write (ADR-0104 point 1). sections()
# filters by it, so the panel and apply's allow-list shrink together: hiding
# rows in the page alone would leave every key the host does not set writable
# by a bare POST. By section, not one flat tuple of keys, because the model is
# also a Brains row and Brains is hidden.
HOSTED_ROWS: dict[str, tuple[str, ...]] = {
    "Session": ("SILICA_MODEL", "context", "SILICA_SHOW_THINKING"),
    "Language": ("SILICA_STT_LANG", "SILICA_STT_BROWSER"),
    "Display": ("SILICA_USER_NAME", "SILICA_THEME", "SILICA_SHOW_THINKING",
                "SILICA_GRAPH_PARTICLES", "SILICA_GRAPH_SHADING"),
    "Memory": ("SILICA_MEMORY",),
}


def _hosted_key_rows() -> tuple[Row, ...]:
    """One secret row per hosted provider, from the table the fallback chain and
    the wizard already share — a provider added there shows up here for free."""
    return tuple(
        Row(key_env, "", f"{name} key", "secret", f"api key for {name}")
        for name, (key_env, _models) in HOSTED_PROVIDERS.items()
    )


def sections() -> dict[str, tuple[Row, ...]]:
    """The admitted rows, by panel section. A row may appear twice (thinking is
    both the session's one live toggle and a display preference); the panel
    syncs every control bound to the same key after a write."""
    from silica.onboarding.wizard import EMBED_MODELS, RERANK_MODELS

    providers = tuple(sorted(PROVIDER_PREFIXES))
    table = {
        "Session": (
            Row("SILICA_MODEL", "model", "model", "text", "what answers you",
                models_of="chat"),
            Row("SILICA_PROVIDER", "provider", "provider", "enum",
                "where that model is served", options=providers),
            Row("context", "", "context", "readonly", "tokens a turn is cut to"),
            Row("SILICA_SHOW_THINKING", "show_thinking", "thinking", "toggle",
                "show the model's reasoning as it works"),
        ),
        "Vault": (
            Row(VAULT_KEY, "vault_path", "vault", "text",
                "the folder silica reads and writes this session; the next launch curates the folder it starts in",
                warn="switching rebuilds every index for the new folder"),
            Row("SILICA_INBOX_DIR", "inbox_dir", "inbox folder", "text",
                "where nucleated files land"),
            Row(settings_ops.SAFE_MODE_KEY, "", "safe mode", "toggle",
                "new notes land in a staging folder that mirrors your vault · "
                "paste its contents over the vault to file them in place"),
            Row("write_dir", "", "write dir", "readonly",
                "writes confined here · the rest is read-only context"),
            Row("SILICA_GIT_COMMIT", "git_commit", "git commit", "enum",
                "commit each write to the vault's repo", options=("off", "auto")),
        ),
        "Brains": (
            Row("SILICA_PROVIDER", "provider", "provider", "enum",
                "who serves the chat model · changing this also sets model and base url",
                options=providers),
            Row("SILICA_PROVIDER_BASE_URL", "provider_base_url", "base url", "text",
                "endpoint for the chat model · used when the provider is custom"),
            Row("SILICA_PROVIDER_API_KEY", "provider_api_key", "api key", "secret",
                "key for that endpoint"),
            Row("SILICA_MODEL", "model", "model", "text", "what answers you",
                models_of="chat"),
            Row("SILICA_EMBEDDING_MODEL", "embedding_model", "embedding model", "text",
                "what turns notes into vectors", models_of="embeddings",
                warn=_EMBED_WARN, curated=tuple(EMBED_MODELS)),
            Row("SILICA_EMBEDDING_BASE_URL", "embedding_base_url", "embedding url", "text",
                "where embeddings are computed", warn=_EMBED_WARN),
            Row("SILICA_RERANK_MODEL", "rerank_model", "reranker", "text",
                "reorders recall results · optional", models_of="rerank",
                curated=tuple(RERANK_MODELS)),
            Row("SILICA_RERANK_BASE_URL", "rerank_base_url", "reranker url", "text",
                "empty turns reranking off · the [rerank] extra serves 127.0.0.1:1235/v1"),
            Row("SILICA_WORKER_MODEL", "worker_model", "worker model", "text",
                "used for distilling · falls back to the chat model", models_of="chat"),
            Row("SILICA_FALLBACK_MODELS", "fallback_models", "fallback models", "text",
                "comma-separated · tried in order when a model's endpoint fails"),
            Row("SILICA_TAVILY_API_KEY", "tavily_api_key", "web search key", "secret",
                "tavily, the backstop when duckduckgo challenges us"),
            *_hosted_key_rows(),
        ),
        "Language": (
            Row("SILICA_COOCCURRENCE_LANG", "cooccurrence_lang", "vault language", "text",
                "frozen per vault · drives keyword extraction",
                warn="the language is frozen per vault · changing it after notes "
                     "exist makes old keywords disagree with new ones"),
            Row("SILICA_STT_LANG", "stt_lang", "dictation language", "text",
                "what the microphone expects"),
            Row("SILICA_STT_BROWSER", "stt_browser", "browser speech", "enum",
                "the browser's own recognizer, when no transcriber answers, and its voices "
                "for reading Memorize's questions aloud · on-device keeps audio on this machine, "
                "cloud may send it to the browser's vendor",
                options=("off", "on-device", "cloud")),
            Row("SILICA_PDF_PROVIDER", "pdf_provider", "pdf reader", "enum",
                "how pdfs are turned into text"),
            Row("SILICA_PDF_OCR_LANG", "pdf_ocr_lang", "pdf ocr languages", "text",
                "tried in order when a pdf has no text layer"),
        ),
        "Display": (
            Row("SILICA_USER_NAME", "user_name", "your name", "text",
                "how the new chat greets you · empty greets no one by name"),
            Row("SILICA_THEME", "theme", "theme", "enum",
                "auto follows your system · dark is the crystal, light is warm paper",
                options=("auto", "dark", "light")),
            Row("SILICA_SHOW_THINKING", "show_thinking", "thinking", "toggle",
                "show the model's reasoning as it works"),
            Row("SILICA_SHOW_BANNER", "show_banner", "banner", "toggle",
                "show the banner on startup"),
            Row("SILICA_TOOL_PROGRESS", "tool_progress", "tool progress", "enum",
                "how much of each tool call you see",
                options=("off", "new", "all", "verbose")),
            Row("SILICA_CAPTURE_SESSIONS", "capture_sessions", "capture sessions", "toggle",
                "keep a transcript of each session"),
            Row("SILICA_VAULT_BRIEF", "vault_brief", "vault brief", "toggle",
                "a written sentence about what the vault holds, which the agent "
                "reads when it picks among vaults · one worker call"),
            Row("SILICA_MAX_CONTEXT", "max_context_tokens", "max context", "int",
                "tokens before the transcript is compacted"),
            Row("SILICA_GRAPH_PARTICLES", "graph_particles", "graph particles", "toggle",
                "drifting dots along gap and similarity edges · off is quieter and cheaper"),
            Row("SILICA_GRAPH_SHADING", "graph_shading", "3d graph shading", "toggle",
                "faceted nodes, crystal light and depth fog · off is the plain renderer"),
        ),
        # The switch is a row like any other so it persists the way they do;
        # the two files under it are drawn by the page (app-settings.js) from
        # GET /memory, since a file is not an env var (ADR-0076 §8).
        "Memory": (
            Row("SILICA_MEMORY", "user_memory", "memory", "toggle",
                "what silica remembers about you and your work, shown to every chat · "
                "off pauses it and keeps the files"),
        ),
    }
    if not hosted():
        return table
    return {
        name: tuple(_hosted_row(r) for r in table[name] if r.key in keys)
        for name, keys in HOSTED_ROWS.items()
    }


def _hosted_row(row: Row) -> Row:
    """A row as a hosted account gets it (ADR-0104 point 1)."""
    if row.key == "SILICA_MODEL":
        # Read-only by kind, not only by host.env's lock: a host.env that lost
        # the line would otherwise let a POST pick any model on the tenant's key.
        return replace(row, kind="readonly", models_of="")
    if row.key == "SILICA_STT_BROWSER":
        # "cloud" sends the audio to the browser's vendor, a recipient the
        # privacy notice does not name.
        return replace(row, options=("off", "on-device"), help=(
            "the browser's own recognizer, when no transcriber answers, and its "
            "voices for reading Memorize's questions aloud · on-device keeps audio on this device"))
    return row


def _applicable() -> dict[str, Row]:
    """Every writable row by key — the allow-list `apply` validates against, so
    a POST can only reach a key this table admitted."""
    out: dict[str, Row] = {}
    for rows in sections().values():
        for r in rows:
            if r.kind != "readonly":
                out[r.key] = r
    return out


# --- values, origins, options ------------------------------------------------

def _file_keys() -> set[str]:
    """Keys carried by the .env on disk — the user-level file, the only one
    config.py layers and so the only one a row can have come from."""
    from dotenv import dotenv_values

    if not USER_ENV.exists():
        return set()
    return {k for k in dotenv_values(USER_ENV) if k}


def locked(key: str) -> bool:
    """Whether this key was exported before any .env was layered in. `override=False`
    means it wins at boot no matter what we write, so the row is not editable."""
    return key in SHELL_ENV


def _origin(key: str, on_file: set[str]) -> str:
    if key in SHELL_ENV:
        return "env"  # exported before any .env was layered in — nothing outranks it
    if key == VAULT_KEY:
        # The launch folder, or a switch this session: a line still sitting in
        # the .env is ignored at boot and must not read as the source.
        return "derived"
    return "file" if key in on_file else "default"


def _value(row: Row) -> str:
    """The live value as the panel shows it. CONFIG first (it is what the next
    operation will actually use), the environment for the rows that have no
    CONFIG field of their own."""
    if row.kind == "readonly":
        return _readonly_value(row.key)
    if row.key == settings_ops.SAFE_MODE_KEY:
        from silica.kernel.base.manifest import active_write_dir

        # An unresolvable declaration reads as on (it is truthy, and every write
        # is being rejected): flipping the toggle off is then the repair.
        return "true" if active_write_dir() else "false"
    if row.attr:
        val = getattr(CONFIG, row.attr)
        if isinstance(val, bool):
            return "true" if val else "false"
        return "" if val is None else str(val)
    return os.getenv(row.key, "")


def _shown(row: Row) -> str:
    """`_value` as it may leave the process: a secret only says it is set, by
    its last four characters. The browser used to mask it, so the key itself
    travelled to any page that could read GET /settings (audit 2026-10-09, S3).
    """
    val = _value(row)
    if row.kind != "secret" or not val:
        return val
    return "•" * 8 + (val[-4:] if len(val) > 12 else "")


def _readonly_value(key: str) -> str:
    if key == "SILICA_MODEL":
        return CONFIG.model or ""
    if key == "context":
        # The window a turn is actually cut to, not the model's: since
        # 2026-10-27 a laptop's SILICA_MAX_CONTEXT holds on OpenRouter too, and
        # model_limits misses the dated id the host pins ((0, 0) for
        # qwen3.7-flash-20260727, measured 2026-10-14).
        return f"{CONFIG.max_context_tokens:,} tokens"
    if key == "write_dir":
        from silica.kernel.base.manifest import get_active_manifest

        declared = get_active_manifest().write_dir
        if declared is None:
            return "invalid: see vault.yaml"
        return declared or "the vault root"
    return ""


def _enum_options(row: Row) -> tuple[str, ...]:
    if row.key == "SILICA_PDF_PROVIDER":
        from silica.sources.convert import PDF_PROVIDERS

        return tuple(PDF_PROVIDERS)
    return row.options


def _text_options(row: Row, models: dict[str, list[str]]) -> tuple[str, ...]:
    """Suggestions for a text row: what the endpoint actually advertises, plus
    the curated ids, plus whatever the enumerated domain is. Never a closed set
    — `endpoint_model_ids` returns [] on any error, and an endpoint is down
    exactly when the user opens this panel to fix it (spec §5.2)."""
    from silica.kernel.text.language import SNOWBALL_TO_ISO

    if row.key == "SILICA_COOCCURRENCE_LANG":
        return ("auto", *sorted(SNOWBALL_TO_ISO))
    if row.key == "SILICA_STT_LANG":
        return ("auto", *sorted(SNOWBALL_TO_ISO.values()))
    found = tuple(models.get(row.models_of, ())) if row.models_of else ()
    return (*found, *(c for c in row.curated if c not in found))


def _probe_models() -> dict[str, list[str]]:
    """Model ids per endpoint, probed concurrently. Deduped by URL because chat
    and embeddings usually share one server, so the usual cost is two requests."""
    from concurrent.futures import ThreadPoolExecutor

    from silica.onboarding.wizard import endpoint_model_ids

    urls = settings_ops.base_urls()
    distinct = sorted({u for u in urls.values() if u})
    if not distinct:
        return {}
    with ThreadPoolExecutor(max_workers=len(distinct)) as pool:
        found = dict(zip(distinct, pool.map(endpoint_model_ids, distinct)))
    return {label: found.get(url, []) for label, url in urls.items()}


def read_sections(probe: bool = True) -> list[dict]:
    """The whole panel as data: every admitted row with its value, where that
    value came from, whether it is locked, and its suggestions."""
    models = _probe_models() if probe else {}
    on_file = _file_keys()
    out = []
    for name, rows in sections().items():
        out.append({"name": name, "rows": [
            {
                "key": r.key,
                "label": r.label,
                "kind": r.kind,
                "help": r.help,
                "warn": r.warn,
                # Confirming is not enough for these two: the repair is a
                # server-side sequence, so the panel posts them elsewhere.
                "confirm": r.key == VAULT_KEY or r.key in EMBED_KEYS,
                "value": _shown(r),
                "origin": _origin(r.key, on_file) if r.kind != "readonly" else "derived",
                "locked": r.kind != "readonly" and r.key in SHELL_ENV,
                "options": list(_enum_options(r) if r.kind == "enum"
                                else _text_options(r, models)),
            }
            for r in rows
        ]})
    return out


# --- writing -----------------------------------------------------------------

def _env_text(row: Row, value: str) -> str:
    """How the value is spelled in the .env, which is what config.py parses back."""
    if row.kind == "toggle":
        return "True" if _truthy(value) else "False"
    return str(value)


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("true", "1", "t", "on", "yes")


def _live_value(row: Row, value: str):
    """The value as the CONFIG field's own type, so the next operation reads
    what the panel shows rather than a string that only looks right."""
    if row.kind == "toggle":
        return _truthy(value)
    if row.kind == "int":
        return int(str(value).strip())
    return str(value)


def provider_group(provider: str) -> dict[str, str]:
    """The keys a provider change drags with it.

    Without this, "live and persistent" plus "one row at a time" produces a
    broken state that is immediately live: the instant SILICA_PROVIDER becomes
    openrouter, the model is still a local id served by nobody. merge_env takes
    a dict, so the multi-key write costs nothing extra — the group is atomic on
    disk and in CONFIG both.

    The model is the provider's own first choice: the head of its HOSTED_PROVIDERS
    list, or whatever a local endpoint actually advertises. Neither answers → the
    model is left alone and the user picks it in the row below.
    """
    from silica.model.providers import PROVIDER_PRESETS
    from silica.onboarding.wizard import endpoint_model_ids

    updates = {"SILICA_PROVIDER": provider}
    preset = PROVIDER_PRESETS.get(provider, {})
    base_url = preset.get("base_url", "")
    hosted = HOSTED_PROVIDERS.get(provider)
    if hosted:
        updates["SILICA_MODEL"] = hosted[1][0]
    elif base_url:
        found = endpoint_model_ids(base_url)
        if found:
            updates["SILICA_MODEL"] = found[0]
    if base_url:
        # Only `custom` reads this field (get_provider falls back to the preset
        # otherwise), but keeping it truthful means the Endpoints section and the
        # model pick-list point at the server that is really being talked to.
        updates["SILICA_PROVIDER_BASE_URL"] = base_url
    return updates


def apply(key: str, value) -> dict:
    """Apply one row: mutate CONFIG, then persist every key it drags.

    Returns ``{ok, path, values}`` — values being every key this write touched
    with its new value, so the panel can resync the rows it did not edit.
    ``{ok: False, error}`` when the key is unknown, locked, or unusable.
    """
    from silica.onboarding.wizard import write_env

    rows = _applicable()
    row = rows.get(key)
    if row is None:
        return {"ok": False, "error": f"not a setting: {key}"}
    if key in SHELL_ENV:
        return {"ok": False, "error": f"defined in the environment ({key})"}
    if key == settings_ops.SAFE_MODE_KEY:
        # Not an env var: the manifest is the store, so none of the .env
        # machinery below applies to this row.
        return settings_ops.apply_safe_mode(_truthy(value))
    if row.kind == "enum" and str(value) not in _enum_options(row):
        # The <select> is closed but a POST does not come from it, and on hosted
        # the narrowed set is the boundary (browser speech without "cloud").
        return {"ok": False, "error": f"{row.label}: one of {', '.join(_enum_options(row))}"}

    updates = provider_group(str(value)) if key == "SILICA_PROVIDER" else {}
    if not updates:
        try:
            updates = {key: _env_text(row, value)}
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": f"{row.label}: {exc}"}

    locked = [k for k in updates if k in SHELL_ENV and k != key]
    if locked:
        # A group write that silently skips an exported key would leave exactly
        # the mixed state the group exists to prevent.
        return {"ok": False, "error": f"defined in the environment ({', '.join(locked)})"}

    for env_key, env_val in updates.items():
        target = rows.get(env_key)
        if target and target.attr:
            try:
                setattr(CONFIG, target.attr, _live_value(target, env_val))
            except (TypeError, ValueError) as exc:
                return {"ok": False, "error": f"{target.label}: {exc}"}
        # Keys with no CONFIG field of their own (the hosted API keys) are read
        # from the environment at call time, so this is what makes them live.
        claim_env({env_key: env_val})
    # merge_env never deletes a line it did not write, so hand-edits and comments survive.
    path = write_env(updates)
    return {"ok": True, "path": settings_ops.short_path(path), "values": {
        k: _shown(rows[k]) if k in rows else v for k, v in updates.items()
    }}


# What a bug report may carry. An allow-list, not a deny-list: every secret row
# is excluded by construction rather than by remembering to exclude it, so a key
# added to the panel tomorrow cannot leak into a public issue by default.
_REPORTABLE = (
    "SILICA_PROVIDER", "SILICA_MODEL", "SILICA_WORKER_MODEL",
    "SILICA_EMBEDDING_MODEL", "SILICA_EMBEDDING_BASE_URL",
    "SILICA_RERANK_MODEL", "SILICA_RERANK_BASE_URL",
    "SILICA_PROVIDER_BASE_URL", "SILICA_COOCCURRENCE_LANG",
    "SILICA_PDF_PROVIDER", "SILICA_TOOL_PROGRESS", "SILICA_MAX_CONTEXT",
)


def reportable() -> list[tuple[str, str]]:
    """(label, value) of each row a bug report may name; a row with no value says nothing."""
    rows = {r.key: r for rows_ in sections().values() for r in rows_}
    return [(rows[key].label, value) for key in _REPORTABLE if key in rows and (value := _value(rows[key]))]
