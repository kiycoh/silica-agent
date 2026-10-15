# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""LLM wrapper — agentic loop calls via litellm.

Handles the interactive agentic loop (tool-calling, multi-turn). Provider
selection for the Distiller's constrained decoding path is in agent/providers.py
(openai SDK directly, per ADR-0008 §M2). This module handles everything else.
"""
from __future__ import annotations

from types import FrameType

import atexit
import collections
import json
import logging
import os
import random
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

# Quiet down Bedrock/SageMaker missing botocore warnings during import
logging.getLogger("LiteLLM").setLevel(logging.ERROR)

import functools

logger = logging.getLogger(__name__)

from silica.config import CONFIG, hosted
from silica.metrics import emit


@functools.cache
def _litellm():
    """litellm, imported by the first call that needs it (ADR-0073): its 161 MB
    and 2.2 s are paid by a process that talks to a model, not by doctor, the
    MCP server or a GUI that only reads, all of which import this module."""
    import litellm
    # silica/__init__.py sets LITELLM_MODE so litellm skips load_dotenv(); this
    # undoes what a release that renamed the switch would inject under our prefix.
    from silica.config import drop_foreign_env

    drop_foreign_env()
    litellm.suppress_debug_info = True
    litellm.drop_params = True
    _keep_openrouter_cache_markers(litellm)
    return litellm


# Run-wide adaptive pacing. A 429 anywhere lifts a floor delay that is slept
# before the *first* attempt of every later call this process makes, so we back
# off an upstream rate limit instead of hammering it. Per-process = per-run for
# the CLI; the TUI and the GUI server keep it for their (long) lifetime, so the
# floor also *halves* on every clean first-try success — one bad 429 episode
# must not slow every later message of a day-long GUI session by 20s.
_run_cooldown = 0.0
_COOLDOWN_STEP = 2.0   # seconds added to the floor per 429
_COOLDOWN_CAP = 20.0   # ceiling on the floor delay
_RATE_LIMIT_ATTEMPTS = 6  # 429s get more tries than other transients (backoff to ~1min)


def retry_transient(fn, exceptions: tuple, attempts: int = 3, base_delay: float = 1.0, jitter: float = 0.0, cancel: threading.Event | None = None, on_retry=None):
    """Call fn(), retrying on transient exceptions with exponential backoff.

    Sleeps base_delay * 2**attempt (+ uniform jitter) between attempts and
    re-raises the last exception once attempts are exhausted. The single
    retry policy for every LLM call site (litellm and openai SDK alike).

    Rate limits (HTTP 429) are treated specially: they get _RATE_LIMIT_ATTEMPTS
    tries (an upstream limit clears on the order of seconds), and each one lifts
    a run-wide cooldown paced before the next call so the whole run slows down
    rather than repeatedly re-hitting the limit.

    `cancel` marks the call abandoned (e.g. Ctrl+C orphaned the worker running
    it): once set, the in-flight attempt still finishes but no further retry is
    scheduled, and a backoff sleep wakes early. Without it an orphaned worker
    keeps hammering the API for minutes and can outlive the interpreter.

    `on_retry(attempt, ceiling, delay, exc)` fires once per scheduled retry,
    never on the exhausting failure or on abandonment. A plain callback and
    not a narration call: this helper is generic, and the one caller that
    narrates (call_llm) is the only one that knows the span it belongs to.
    """
    global _run_cooldown
    ceiling = attempts
    for attempt in range(1, max(attempts, _RATE_LIMIT_ATTEMPTS) + 1):
        if attempt == 1 and _run_cooldown:
            time.sleep(_run_cooldown)  # pace the start of every call once a 429 was seen
        try:
            result = fn()
            if attempt == 1 and _run_cooldown:  # clean first try → upstream healthy, decay the floor
                _run_cooldown = _run_cooldown / 2 if _run_cooldown > 0.5 else 0.0
            return result
        except exceptions as e:
            if getattr(e, "status_code", None) == 429:
                _run_cooldown = min(_run_cooldown + _COOLDOWN_STEP, _COOLDOWN_CAP)
                ceiling = _RATE_LIMIT_ATTEMPTS
            if cancel is not None and cancel.is_set():
                logger.info("Call abandoned; dropping retries: %s", e)
                raise
            # Thread name, because these lines are the only trace a concurrent
            # lane leaves: distill prefetch runs k=3 calls (SILICA_DISTILL_CONCURRENCY)
            # that start and time out in the same second, so three identical
            # warnings read as one call retried three times unless they are named.
            who = threading.current_thread().name
            if attempt >= ceiling:
                logger.error("[%s] Transient error, %d attempts exhausted: %s", who, attempt, e)
                raise
            delay = base_delay * (2 ** attempt) + (random.uniform(0, jitter) if jitter else 0.0)
            logger.warning(
                "[%s] Transient error (attempt %d/%d): %s. Retrying in %.1fs...",
                who, attempt, ceiling, e, delay,
            )
            if on_retry is not None:
                on_retry(attempt, ceiling, delay, e)
            if cancel is not None:
                if cancel.wait(delay):  # backoff sleep that wakes on abandonment
                    logger.info("Call abandoned during backoff; dropping retries: %s", e)
                    raise
            else:
                time.sleep(delay)


# --- key pools and fallback models (ADR-0063) ----------------------------------

# How long a key sits out once the endpoint blamed it. A rate limit clears in
# about the minute _RATE_LIMIT_ATTEMPTS already backs off over, so 60 s, not
# the hour Hermes Agent benches a key for after a second 429. An unpaid key
# (402) does not clear by waiting: the hour Hermes uses. A refused key (401,
# 403) gets the hour too, where Hermes waits 5 minutes: it has an OAuth token
# to refresh first, and a static key here has nothing that could change.
_KEY_BENCH_S = {429: 60.0, 401: 3600.0, 402: 3600.0, 403: 3600.0}
_benched: dict[str, float] = {}   # key -> monotonic time it may serve again

# A model whose endpoint failed sits behind the rest of its chain this long.
# Without it every iteration of a turn would pay the dead endpoint's whole
# retry budget again (three attempts, up to 130 s each on a hung socket)
# before reaching the model that answered the iteration before.
_FALLBACK_HOLD_S = 300.0
_held: dict[str, float] = {}   # model -> monotonic time it leads its chain again


def _status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def key_pool(model: str) -> list[str]:
    """The keys `model` may be called with: its provider's key variable, then
    the numbered siblings (OPENROUTER_API_KEY_2, _3, ...) up to the first gap.
    custom/ reads SILICA_PROVIDER_API_KEY the same way. Fewer than two keys is
    no pool, and the call goes out exactly as it did before pools."""
    from silica.config import HOSTED_PROVIDERS

    prefix = model.split("/", 1)[0]
    if prefix == "custom":
        env, first = "SILICA_PROVIDER_API_KEY", CONFIG.provider_api_key
    elif prefix in HOSTED_PROVIDERS:
        env = HOSTED_PROVIDERS[prefix][0]
        first = os.getenv(env, "")
    else:
        return []
    keys = [first]
    n = 2
    while sibling := os.getenv(f"{env}_{n}", ""):
        keys.append(sibling)
        n += 1
    return [k for k in keys if k]


def _with_keys(keys: list[str], kwargs: dict, call):
    """One attempt, made with each key in turn while the endpoint blames the
    key; any other failure, or the last key's, goes up to the retry policy.
    Fill-first: the first healthy key serves until it is benched."""
    if len(keys) < 2:
        return call()
    now = time.monotonic()
    ready = [k for k in keys if _benched.get(k, 0.0) <= now]
    order = ready + sorted((k for k in keys if k not in ready), key=lambda k: _benched[k])
    last: BaseException | None = None
    for key in order:
        kwargs["api_key"] = key
        try:
            return call()
        except Exception as e:
            bench = _KEY_BENCH_S.get(_status(e) or 0)
            if bench is None:
                raise
            _benched[key] = time.monotonic() + bench
            logger.warning("key ...%s for %s sits out %.0fs: %s", key[-4:], kwargs["model"], bench, e)
            last = e
    assert last is not None
    raise last


def fallback_chain(model: str) -> list[str]:
    """`model`, then CONFIG.fallback_models in order; a model held after a
    failure moves behind the others until its hold expires."""
    extra = [m.strip() for m in (CONFIG.fallback_models or "").split(",") if m.strip()]
    chain = [model] + [m for m in dict.fromkeys(extra) if m != model]
    now = time.monotonic()
    held = sorted((m for m in chain if _held.get(m, 0.0) > now), key=lambda m: _held[m])
    return [m for m in chain if m not in held] + held


def _falls_back(exc: BaseException) -> bool:
    """The endpoint's failure, not the request's: another model may answer.
    A 400 (a malformed request, a context the window cannot hold) is the
    request's and would fail anywhere; compaction owns the window."""
    status = _status(exc)
    if status is not None:
        return status in (401, 402, 403, 404, 408, 429) or status >= 500
    return isinstance(exc, (_litellm().Timeout, _litellm().APIConnectionError))


_LOCAL_LLM_TIMEOUT = float(os.getenv("SILICA_LLM_TIMEOUT", "130"))
# Wall-clock backstop we enforce ourselves (> the litellm timeout below, so litellm's
# own timeout wins if it ever fires). litellm's `timeout` kwarg does NOT fire on a
# provider that accepts the request then never sends a body: observed, OpenRouter
# holding an ESTAB socket idle ~58min, zero retries, the whole process wedged on one
# call. Kept 10s above the 120s litellm timeout so litellm fires first on a normal
# timeout and this only catches the silent-hang case.
#
# Overridable because the default cannot tell "hung" from "slow" on a non-streaming
# call, and a slow reasoning model lives inside the band: measured 2026-08-23 on
# openrouter/stealth/ox-alpha, the distiller shape (8.1k prompt -> 4k completion) took
# 78s alone and 106-112s with the default k=3 prefetch in flight, i.e. the run kills
# its own healthy calls. Raise it for such a model (SILICA_LLM_TIMEOUT=400), or drop
# SILICA_DISTILL_CONCURRENCY to 1. The real repair is to stream the distiller call so
# the deadline resets per chunk (_bounded_stream already does that for the loop) and
# no wall-clock number has to be guessed at all.


def run_with_deadline(fn, timeout: float, on_timeout, *, catch: type = Exception):
    """Run fn() on a daemon thread, joining up to `timeout` seconds.

    Past the deadline, raise `on_timeout()`; if fn raised (of type `catch`),
    re-raise it on the caller thread; otherwise return fn()'s value. The only
    wall-clock bound we control — a transport read-timeout can silently not fire
    when the provider trickles keep-alive bytes.

    ponytail: on timeout the worker thread is abandoned (daemon) — a blocked
    C-level socket read can't be force-cancelled. Bounded by the caller's retry
    cap / single-turn use; swap for a cancellable HTTP client if abandoned
    threads ever pile up.
    """
    box: dict = {}

    def _work():
        try:
            box["r"] = fn()
        except catch as e:  # noqa: BLE001 - carried to the calling thread
            box["e"] = e

    # Caller's name plus a marker per nesting level: retry_transient logs from
    # *inside* this worker, and two deadlines nest per distiller call
    # (DISTILLER_TIMEOUT outside, _LOCAL_LLM_TIMEOUT inside), so the default
    # "Thread-18 (_work)" hides which lane a retry belongs to. The suffix is not
    # decoration: a bare copy let an abandoned worker log as "MainThread" long
    # after the main thread had given up on it.
    th = threading.Thread(target=_work, daemon=True,
                          name=f"{threading.current_thread().name}~dl")
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise on_timeout()
    if "e" in box:
        raise box["e"]
    return box["r"]


def _bounded(fn, timeout: float, model: str):
    """Run fn() but raise litellm.Timeout if it exceeds `timeout` seconds, routing
    the silent-hang case into the normal transient-retry path (see _LOCAL_LLM_TIMEOUT)."""
    return run_with_deadline(
        fn, timeout,
        lambda: _litellm().Timeout(
            message=f"local wall-clock timeout after {timeout:.0f}s (provider sent no response)",
            model=model, llm_provider=model.split("/", 1)[0]),
        catch=BaseException,
    )


def _bounded_stream(make_iter, per_chunk_timeout: float, model: str):
    """Yield chunks from make_iter()'s stream, raising litellm.Timeout if any
    single gap (including connecting + the first chunk) exceeds per_chunk_timeout.

    The streaming twin of _bounded: the non-stream call's silent-hang mode
    (provider accepts the request then never sends a body) shows up on the stream
    path as a blocking next() that never returns. A per-chunk deadline catches
    that without capping a healthy long stream — the clock resets on every chunk.

    ponytail: pump thread is daemon and abandoned on timeout, same trade as
    _bounded; swap for a cancellable HTTP client if abandoned threads pile up.
    """
    import queue

    q: "queue.Queue" = queue.Queue()
    _DONE = object()

    def _pump():
        try:
            for c in make_iter():
                q.put(("chunk", c))
        except BaseException as e:  # noqa: BLE001 - carried to the consumer
            q.put(("err", e))
        finally:
            q.put(("done", _DONE))

    threading.Thread(target=_pump, daemon=True, name="llm-stream").start()
    while True:
        try:
            kind, payload = q.get(timeout=per_chunk_timeout)
        except queue.Empty:
            raise _litellm().Timeout(
                message=f"local wall-clock timeout after {per_chunk_timeout:.0f}s (stream stalled)",
                model=model, llm_provider=model.split("/", 1)[0])
        if kind == "err":
            raise payload
        if kind == "done":
            return
        yield payload


# Models whose endpoint answered the `reasoning: {enabled: False}` block with a
# 400. Process-lived and keyed by model string: the refusal is a property of the
# endpoint, so paying it once per process is enough, and nothing persists a
# judgement about a model id that may be repointed tomorrow.
_REASONING_MANDATORY: set[str] = set()


def _without_refused_reasoning(model: str, kwargs: dict, call):
    """Run `call()`, and if the endpoint 400s over the reasoning knob, strip it
    and run once more with thinking left on.

    `reasoning=False` is a request, not a guarantee: measured 2026-08-24 on
    openrouter/stealth/ox-alpha, "Reasoning is mandatory for this endpoint and
    cannot be disabled" (400). BadRequestError is not transient, so it took the
    whole lane down for every caller of the knob — and forms.py catches Exception
    and returns "", so on that model form sniffing was silently off with nothing
    on screen to say why. A thinking model that answers is strictly better than a
    lane that does not run; the budget, not the knob, is the caller's defence
    against a trace eating max_tokens.
    """
    try:
        return call()
    except _litellm().BadRequestError as e:
        text = str(e).lower()
        rt = kwargs.get("extra_body") or {}
        # A strict OpenAI-compatible host behind `custom/` may 400 on the
        # llama.cpp knob it does not know; same remedy, same memo.
        knob = "chat_template_kwargs" if "chat_template_kwargs" in text and "chat_template_kwargs" in rt else "reasoning"
        if knob == "reasoning" and ("reasoning" not in text or not ("mandator" in text or "cannot be disabled" in text)):
            raise
        if knob not in rt:
            raise
        _REASONING_MANDATORY.add(model)
        logger.warning("%s refuses %s — retrying with "
                       "thinking on; budget the trace against max_tokens", model, knob)
        rt.pop(knob)
        if rt:
            kwargs["extra_body"] = rt
        else:
            kwargs.pop("extra_body", None)
        return call()


def _reject_cut_stream(built, streamed_reasoning: list[str], model: str) -> None:
    """Raise a transient error when the reassembled stream carried nothing.

    stream_chunk_builder cannot tell a stream that ENDED from one that was CUT:
    a connection dropped after the opening chunk reassembles into a well-formed
    message with content='', a *synthesised* finish_reason='stop', and usage
    counted locally. Nothing downstream can tell that apart from a model that
    chose to say nothing, so the loop returned it as the answer and the turn
    rendered as silence (measured 2026-08-24 on openrouter/stealth/ox-alpha:
    three turns of 5.6s / 8.5s / 21.3s, completion_tokens=0, cached_tokens=0 —
    the shape of a usage litellm had to count itself).

    Billed completion tokens are what separate the two: a model that generated
    anything, even a trace that ate the whole budget, is charged for it
    (measured on the same model: finish=length, completion_tokens=5, text='').
    So a turn with zero of everything is a transport failure and belongs in the
    retry path, not in the caller's answer.
    """
    try:
        message = built.choices[0].message
    except (AttributeError, IndexError, TypeError):
        message = None
    if message is not None and (
        (getattr(message, "content", None) or "")
        or getattr(message, "tool_calls", None)
        or streamed_reasoning
    ):
        return
    usage = getattr(built, "usage", None)
    if getattr(usage, "completion_tokens", 0) or (
        isinstance(usage, dict) and usage.get("completion_tokens")
    ):
        return
    raise _litellm().APIConnectionError(
        message=f"stream from {model} ended without generating a single token "
                "(cut connection, not an empty answer)",
        model=model, llm_provider=model.split("/", 1)[0])


# How OpenRouter names this app — in the user's own dashboard and in the public
# app rankings, where the other harnesses (opencode, Hermes) appear. Not passing
# them is not "unattributed": litellm builds
# {"HTTP-Referer": "https://litellm.ai", "X-Title": "liteLLM"} for every
# openrouter/ call and then `update`s the caller's headers over its own
# (main.py), so silence files every silica generation under liteLLM.
_OPENROUTER_ATTRIBUTION = {
    "HTTP-Referer": "https://github.com/kiycoh/silica-agent",
    "X-Title": "Silica",
}

# Session key for the lanes that never open a narration session — a nucleate
# batch, a subagent, the distill pool. One per process, so such a run still
# arrives as one session instead of N loose generations nothing groups.
_PROCESS_SESSION = f"silica-{uuid.uuid4().hex[:12]}"


def _openrouter_session_id() -> str:
    """The id OpenRouter groups a conversation's generations under (max 256).

    The narration sid whenever one is open, so the session in OpenRouter's
    dashboard and the file under ~/.silica/narration carry the SAME id and each
    can be read against the other — which is the whole reason to send one rather
    than a fresh uuid per call.

    It is also OpenRouter's sticky-routing key: after the first success every
    later call in the session prefers the provider that served it, which is what
    makes the prompt-cache breakpoints pay off across turns (a cache lives on one
    provider). A preference, not a pin — OpenRouter falls back when that provider
    is unavailable rather than failing the request.

    It does not cost the parallel lanes anything. Measured 2026-08-24, 6
    concurrent calls x 6 rounds per condition on stealth/ox-alpha: wall clock
    stayed far under the sum of the individual calls under a SHARED session id
    (21.3s against 67.1s, and so on for every round), i.e. sharing the id
    serialises nothing; errors were 0/36 shared against 2/36 with distinct ids.
    Latency showed no separable effect — a single burst swung 3s to 108s on that
    free shared pool, which swamps anything the routing does.
    """
    from silica.agent.narration import NARRATOR  # lazy, as call_llm's own beat does

    return NARRATOR.sid or _PROCESS_SESSION


@functools.lru_cache(maxsize=4)
def _provider_table(raw: str) -> dict[str, dict]:
    """OPENROUTER_PROVIDER_BY_MODEL parsed, keyed without the openrouter/
    prefix. A table that does not parse raises rather than reading as empty:
    the pin it carries can be where the data goes (the pilot's keeps Qwen off
    Alibaba's Singapore/China endpoint), so a typo must stop the calls, not
    quietly unpin them."""
    try:
        table = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"OPENROUTER_PROVIDER_BY_MODEL is not JSON: {e}") from e
    if not isinstance(table, dict) or not all(isinstance(v, dict) for v in table.values()):
        raise ValueError("OPENROUTER_PROVIDER_BY_MODEL must map a model id to a provider object")
    return {k.removeprefix("openrouter/"): v for k, v in table.items()}


def openrouter_routing(provider_list: str | None = None, model: str = "") -> dict | None:
    """OpenRouter `extra_body` provider-routing block, or None.

    A model named in OPENROUTER_PROVIDER_BY_MODEL gets its own `provider`
    object and nothing else applies to it. Otherwise:
    `provider_list` is a comma-separated list of provider names pinned as the
    routing `order`; defaults to CONFIG.openrouter_provider. The distiller path
    passes CONFIG.openrouter_provider_distiller for its own pin. `allow_fallbacks`
    is False: an explicit pin means "these providers or fail" — silently bouncing
    to an unpinned (maybe rate-limited) provider is exactly the surprise this knob
    exists to prevent. Shared by both LLM paths — litellm (call_llm) and the
    openai SDK (agent/providers.py) — so the pin applies everywhere openrouter is used.
    """
    if CONFIG.openrouter_provider_by_model.strip() and model:
        own = _provider_table(CONFIG.openrouter_provider_by_model).get(model.removeprefix("openrouter/"))
        if own is not None:
            return {"provider": own}
    raw = CONFIG.openrouter_provider if provider_list is None else provider_list
    order = [p.strip() for p in raw.split(",") if p.strip()]
    return {"provider": {"order": order, "allow_fallbacks": False}} if order else None


@dataclass
class ToolCall:
    """A single tool invocation requested by the model."""

    id: str
    name: str
    args: dict


# --- token meter (opt-in via SILICA_TOKEN_METER=1) -------------------------
# Attributes each call's token usage to the first stack frame outside the LLM
# plumbing, so distill/collision/loop/codewiki show up as separate call-sites.
# Single point: every provider path constructs an LLMResponse, so recording in
# __post_init__ captures all of them with zero wiring. atexit dumps a sorted
# table. Off = one bool check, no stack-walk.
# profiling aid, not a live endpoint — dump on process exit only.
# A hosted tenant meters always and writes each call as an llm_call line
# (ADR-0100 point 3): its process dies by SIGTERM, so the atexit table never
# printed there.
_METER_ON = os.getenv("SILICA_TOKEN_METER") == "1" or hosted()
_meter: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0, 0, 0])  # site -> [calls, prompt, completion, cached]
# The same four counts over every call-site, so a chat turn's usage is the
# difference between two readings (meter_totals, ui/web/server.py run_turn).
_meter_totals = [0, 0, 0, 0]


def meter_totals() -> tuple[int, int, int, int]:
    """(calls, prompt, completion, cached) since the process started."""
    calls, prompt, completion, cached = _meter_totals
    return calls, prompt, completion, cached


def _meter_site() -> str:
    f: FrameType | None = sys._getframe(2)  # skip _meter_site + _meter_record
    while f is not None:
        name = f.f_code.co_filename
        if not (name.endswith(("llm.py", "providers.py")) or name == "<string>"):
            return f"{os.path.basename(name)}:{f.f_code.co_name}"
        f = f.f_back
    return "?"


# Anthropic bills the entire prompt every turn unless the request carries an
# explicit cache breakpoint; the OpenAI family caches long prefixes on its own,
# and local backends have no billing to amortise. Gemini caches implicitly from
# 2.5 on, but an explicit breakpoint raises the hit rate through OpenRouter.
# Applies whether direct or proxied (openrouter/, bedrock/, vertex_ai/); the
# native gemini/ route ignores the marker rather than rejecting it.
_CACHEABLE_MODELS = ("anthropic", "claude", "gemini")
# Qwen through OpenRouter caches implicitly only at fixed checkpoints (7936
# tokens): a 5.7k-token source sent under two different stage prompts got no
# hit, while an explicit breakpoint hit the whole prefix on every later call,
# four in parallel too, at no write premium (alibaba/us-east-1, 2026-10-15).
# Only on that route: a local Qwen behind `custom` has no billing to amortise.
_OPENROUTER_CACHEABLE = ("qwen",)


def _cacheable(model: str) -> bool:
    m = model.lower()
    if any(k in m for k in _CACHEABLE_MODELS):
        return True
    # ponytail: an env gate while the A/B runs (2026-10-15); drop it, and the
    # check with it, once the arm is adopted or refuted.
    return (os.getenv("SILICA_QWEN_CACHE", "").strip() == "1" and m.startswith("openrouter/")
            and any(k in m for k in _OPENROUTER_CACHEABLE))


def _keep_openrouter_cache_markers(litellm) -> None:
    """litellm strips cache_control on OpenRouter for any model outside its own
    list (claude, gemini, minimax, glm, z-ai in 1.86.0: llms/openrouter/chat/
    transformation.py), so the breakpoint _with_prompt_cache puts on a Qwen
    request never reached the provider. Widens that check to the models in
    _OPENROUTER_CACHEABLE, once per process."""
    try:
        from litellm.llms.openrouter.chat.transformation import OpenrouterConfig
        orig = OpenrouterConfig._supports_cache_control_in_content
    except (ImportError, AttributeError):
        # A litellm that moved or renamed the check: the request still goes
        # out, without the breakpoint, i.e. billed as before this existed.
        logger.debug("litellm: no OpenRouter cache_control check to widen")
        return
    if getattr(orig, "_silica", False):
        return

    def supports(self, model: str) -> bool:
        return any(k in model.lower() for k in _OPENROUTER_CACHEABLE) or orig(self, model)

    supports._silica = True
    OpenrouterConfig._supports_cache_control_in_content = supports


# Routes whose server renders the conversation with the model's own Jinja chat
# template (llama-server, LM Studio, vLLM behind `custom`). Qwen3.5's template
# raises "System message must be at the beginning" on any system message past
# index 0 (measured 2026-09-27 on llama-server), and the interactive loop sends a
# leading block of four (cli._fresh_messages) plus mid-run notes (agent/loop.py).
# The same template also expects the reasoning of the turn in flight back:
# without it the step after a tool result came back empty 3/3 (one end-of-turn
# token, or the whole answer left inside an unclosed <think>), with it 3/3
# valid, same request (measured 2026-09-27, Qwen3.5-9B Q4_K_M). The hosted
# routes keep the reasoning out of the history (see silica_reasoning below);
# a local server bills nothing for it.
# Hosted routes take them as they are and carry cache markers on them, and
# Ollama renders with its own Go templates, so neither is rewritten.
_JINJA_ROUTES = ("custom/", "lmstudio/")


def _one_leading_system(messages: list[dict]) -> list[dict]:
    """The leading system block folded into one message; later system messages
    sent as user turns. A mid-run note becomes a user turn rather than being
    hoisted into the head because hoisting would rewrite the prompt prefix on
    every turn and void the local server's prefix cache."""
    head = 0
    while head < len(messages) and messages[head].get("role") == "system":
        head += 1
    out: list[dict] = []
    if head:
        out.append({"role": "system", "content": "\n\n".join(
            _text_of(m.get("content")) for m in messages[:head])})
    out += [{**m, "role": "user"} if m.get("role") == "system" else m
            for m in messages[head:]]
    return out


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content or [] if isinstance(p, dict))


def _cache_marked(msg: dict) -> dict:
    """Copy of `msg` with a cache breakpoint on its content, else `msg` as-is.

    Only plain non-empty string content is marked. A tool-role message's blocks
    get nested inside a `tool_result` by the translation layer, where a
    cache_control marker is not a valid position, so those are left alone.
    """
    content = msg.get("content")
    if not isinstance(content, str) or not content:
        return msg
    return {**msg, "content": [
        {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}},
    ]}


def _with_prompt_cache(model: str, messages: list[dict]) -> list[dict]:
    """Mark up to two cache breakpoints in the request.

    A breakpoint caches everything up to and including its own block, and the
    prompt is ordered tools → system → messages, so:

      • the LAST leading system message caches the whole static head — the tool
        schemas (~7k tokens), the system prompt, and the vault map. Marking only
        messages[0] left every later system message outside the prefix.
      • the last user message caches the conversation accumulated so far, which
        is otherwise re-billed in full on every turn.

    Never mutates `messages`: it is the caller's live history and run_agent keeps
    appending to it, so a marker persisted there would end up in the stored
    conversation and in every later turn.
    """
    if not _cacheable(model):
        return messages
    out = list(messages)
    head = 0
    while head < len(out) and out[head].get("role") == "system":
        head += 1
    if head:
        out[head - 1] = _cache_marked(out[head - 1])
    for i in range(len(out) - 1, head - 1, -1):
        if out[i].get("role") == "user":
            out[i] = _cache_marked(out[i])
            break
    return out


def _cached_tokens(usage: dict) -> int:
    """Cache-hit prompt tokens across provider dialects (0 when unreported)."""
    ptd = usage.get("prompt_tokens_details")
    cached = ptd.get("cached_tokens") if isinstance(ptd, dict) else getattr(ptd, "cached_tokens", None)
    return cached or usage.get("cache_read_input_tokens") or usage.get("prompt_cache_hit_tokens") or 0


def _meter_record(usage: dict) -> tuple[str, int, int, int]:
    """Add one call to its call-site and to the totals; (site, prompt,
    completion, cached) for the caller's llm_call line."""
    site = _meter_site()
    counts = (1, usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0,
              _cached_tokens(usage))
    slot = _meter[site]
    for i, n in enumerate(counts):
        slot[i] += n
        _meter_totals[i] += n
    return site, counts[1], counts[2], counts[3]


@atexit.register
def _meter_dump() -> None:
    if not _meter:
        return
    rows = sorted(_meter.items(), key=lambda kv: kv[1][1] + kv[1][2], reverse=True)
    grand = sum(p + c for _, (_, p, c, _k) in rows)
    grand_p = sum(p for _, (_, p, _c, _k) in rows)
    grand_k = sum(k for _, (_, _p, _c, k) in rows)
    rate = f" · cached {grand_k:,}/{grand_p:,} ({grand_k / grand_p:.0%})" if grand_p else ""
    print(f"\n=== token meter (prompt+completion by call-site) — total {grand:,}{rate} ===", file=sys.stderr)
    print(f"{'call-site':<44}{'calls':>7}{'prompt':>13}{'cached':>13}{'compl':>11}", file=sys.stderr)
    for site, (n, p, c, k) in rows:
        print(f"{site:<44}{n:>7}{p:>13,}{k:>13,}{c:>11,}", file=sys.stderr)


@dataclass
class LLMResponse:
    """Structured response from the LLM."""

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    assistant_message: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    reasoning: str | None = None
    finish_reason: str | None = None
    # The litellm kwargs as sent, minus messages and credentials: what a
    # traced call beat replays (SILICA_TRACE_LLM). Built in _call_llm because
    # only there do the clamped max_tokens and the routing extras exist.
    request: dict = field(default_factory=dict)

    def __post_init__(self):
        if _METER_ON and self.usage:
            site, prompt, completion, cached = _meter_record(self.usage)
            # usage carries OpenRouter's cost only on a non-streamed call:
            # stream_chunk_builder rebuilds usage without it (ADR-0100 Context).
            cost = self.usage.get("cost")
            emit("llm_call", site=site, model=self.request.get("model"), prompt=prompt,
                 cached=cached, completion=completion, finish=self.finish_reason,
                 usd=float(cost) if cost is not None else None)


def _split_arg_objects(cid: str, name: str, arg_str: str) -> list[tuple[str, dict, str]]:
    """Yield (id, args, wire_str) for one raw tool-call arguments string.

    Normally one object in, one out. But some OpenAI-compatible backends can't
    emit parallel tool_calls, so a model wanting N calls concatenates N JSON
    objects into one blob (e.g. '{"name":"a"}{"name":"b"}'). Fan those back out
    with distinct ids. Unsalvageable args degrade to {} as before.
    """
    s = (arg_str or "").strip()
    if not s:
        return [(cid, {}, "{}")]
    try:
        return [(cid, json.loads(s), s)]
    except json.JSONDecodeError:
        pass
    dec, objs, i = json.JSONDecoder(), [], 0
    while i < len(s):
        try:
            obj, end = dec.raw_decode(s, i)
        except json.JSONDecodeError:
            break
        objs.append(obj)
        i = end
        while i < len(s) and s[i].isspace():
            i += 1
    if not objs:
        logger.warning("Failed to parse tool args for %s: %s", name, arg_str)
        return [(cid, {}, "{}")]
    if len(objs) == 1:
        return [(cid, objs[0], json.dumps(objs[0]))]
    return [(f"{cid}_{k}", o, json.dumps(o)) for k, o in enumerate(objs)]


def expand_tool_calls(
    raw: list[tuple[str, str, str]],
) -> tuple[list[ToolCall], list[dict]]:
    """Parse (id, name, arguments) triples into ToolCalls + sanitized wire dicts.

    Fans out concatenated-JSON blobs into separate calls (see _split_arg_objects)
    so the agent loop dispatches each. Returned wire dicts always carry valid
    JSON arguments, keeping the assistant/tool message pairing API-safe.
    """
    parsed: list[ToolCall] = []
    wire: list[dict] = []
    for cid, name, arg_str in raw:
        for sub_id, obj, obj_str in _split_arg_objects(cid, name, arg_str):
            parsed.append(ToolCall(id=sub_id, name=name, args=obj))
            wire.append(
                {"id": sub_id, "type": "function",
                 "function": {"name": name, "arguments": obj_str}}
            )
    return parsed, wire


# Some providers (observed: deepseek through OpenRouter) sometimes serialize a
# tool call into the assistant CONTENT instead of the tool_calls field. Nothing
# downstream can tell that from a deliberate final answer, so the loop accepts
# the markup as the model's reply: in the L3 gate that made one research run
# "write" its note as a raw web_fetch call, 188 chars of tags, and the run was
# counted as a completed note. Recovering the call here covers every provider
# path, since all three of them assemble the message through this function.
_LEAK_BLOCK = re.compile(r"<｜?DSML｜?tool_calls>.*?</｜?DSML｜?tool_calls>", re.S)
_LEAK_INVOKE = re.compile(
    r"<｜?DSML｜?invoke\s+name=\"([^\"]+)\"\s*>(.*?)</｜?DSML｜?invoke>", re.S)
_LEAK_PARAM = re.compile(
    r"<｜?DSML｜?parameter\s+name=\"([^\"]+)\"[^>]*>(.*?)</｜?DSML｜?parameter>", re.S)


# Qwen3.5's own XML call format. llama-server parses it out of the answer, but
# not out of the reasoning: a call written before the model closes its thinking
# reaches us as reasoning text with an empty answer (measured 2026-09-27,
# Qwen3.5-9B Q4_K_M, second turn of a two-tool question).
_QWEN_BLOCK = re.compile(r"<tool_call>.*?</tool_call>", re.S)
_QWEN_INVOKE = re.compile(r"<function=([^>\s]+)>(.*?)</function>", re.S)
_QWEN_PARAM = re.compile(r"<parameter=([^>\s]+)>(.*?)</parameter>", re.S)

_LEAK_FORMATS = (
    (_LEAK_BLOCK, _LEAK_INVOKE, _LEAK_PARAM),
    (_QWEN_BLOCK, _QWEN_INVOKE, _QWEN_PARAM),
)


def recover_leaked_tool_calls(content: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Leaked-as-text tool calls -> (content with them removed, raw triples)."""
    for block, invoke, param in _LEAK_FORMATS:
        found = invoke.findall(content)
        if found:
            return (invoke.sub("", block.sub("", content)).strip(),
                    _leaked_triples(found, param))
    return content, []


def _leaked_triples(found: list[tuple[str, str]], param: re.Pattern) -> list[tuple[str, str, str]]:
    calls = []
    for i, (name, body) in enumerate(found):
        # The leaked markup types nothing, so scalars stay strings: pydantic's
        # lax validation already coerces "5"/"true" per the tool's own schema,
        # and guessing here would turn a string field's "5" into an int it
        # rejects. Containers are the one shape validation cannot recover from
        # a string, so a value that parses as JSON dict/list is passed parsed.
        args: dict[str, object] = {}
        for k, v in param.findall(body):
            v = v.strip()
            if v[:1] in "{[":
                try:
                    parsed = json.loads(v)
                    if isinstance(parsed, (dict, list)):
                        args[k] = parsed
                        continue
                except ValueError:
                    pass
            args[k] = v
        calls.append((f"call_leaked_{i}", name, json.dumps(args)))
    return calls


def build_assistant_message(
    content: str | None, tool_calls_raw: list[tuple[str, str, str]] | None,
    reasoning: str | None = None,
) -> tuple[dict, list[ToolCall]]:
    """Assemble the assistant history dict + parsed ToolCalls both provider paths
    build identically: {"role": "assistant"} (+content if any, +tool_calls if any).

    Callers add path-specific keys (reasoning_content, thinking_blocks) afterwards.
    `reasoning` is read only when the turn came back empty: a call the model
    merely considered while thinking, then answered past, must not run.
    """
    if not tool_calls_raw and content and ("DSML" in content or "<tool_call>" in content):
        content, tool_calls_raw = recover_leaked_tool_calls(content)
        if tool_calls_raw:
            logger.warning(
                "recovered %d tool call(s) the provider leaked into assistant "
                "content: %s", len(tool_calls_raw),
                ", ".join(name for _id, name, _args in tool_calls_raw))
    elif not tool_calls_raw and not content and reasoning and "<tool_call>" in reasoning:
        _, tool_calls_raw = recover_leaked_tool_calls(reasoning)
        if tool_calls_raw:
            logger.warning(
                "recovered %d tool call(s) the model wrote inside its reasoning: %s",
                len(tool_calls_raw), ", ".join(name for _id, name, _args in tool_calls_raw))
    msg: dict = {"role": "assistant"}
    if content:
        msg["content"] = content
    parsed: list[ToolCall] = []
    if tool_calls_raw:
        parsed, msg["tool_calls"] = expand_tool_calls(tool_calls_raw)
    return msg, parsed


# An image part's share of the request, in tokens, for the output clamp. Its
# base64 counted as text read a 120 KB page PNG (160k characters of data URL)
# as about 80k tokens, left no window, and cut the reply to the clamp's 1024
# floor: a page reading stopped at 621 of 673 words with max_tokens 2500 and
# 6000 alike (2026-10-13). Qwen3.5-9B counted 1,874 prompt tokens for a 1600 px
# page with its prompt; 2,000 keeps the estimate on the side of a smaller reply.
_IMAGE_TOKENS = 2000


def _request_chars(messages: list) -> int:
    """The request's size as the output clamp should read it: the serialized
    messages, with each inline image counted as `_IMAGE_TOKENS` instead of its
    data URL's length."""
    from silica.agent.compaction import TEXT_CHARS_PER_TOKEN

    chars = len(str(messages))
    for m in messages:
        parts = m.get("content") if isinstance(m, dict) else None
        for part in parts if isinstance(parts, list) else ():
            url = (part.get("image_url") or {}).get("url", "") if isinstance(part, dict) else ""
            if isinstance(url, str) and url.startswith("data:"):
                chars += _IMAGE_TOKENS * TEXT_CHARS_PER_TOKEN - len(url)
    return max(chars, 0)


def call_llm(
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int | None = None,
    response_format=None,
    temperature: float | None = None,
    on_delta: Callable[[str, str], None] | None = None,
    openrouter_provider: str | None = None,
    cancel: threading.Event | None = None,
    api_key: str | None = None,
    reasoning: bool | None = None,
    sdk_retries: int | None = None,
) -> LLMResponse:
    """Narrating wrapper around the real call (`_call_llm`).

    One graft point covers every LLM call in the process — the loop, the
    subagents, the distill pool — which is why the `call` beat lives here and
    not at each caller (spec §4). No open session → narrate() no-ops, so
    batch entry points pay one dict lookup and nothing else.
    """
    from silica.agent.narration import NARRATOR
    from silica.agent.providers import _to_wire  # lazy: providers.py imports this module
    import uuid as _uuid
    cid = f"c-{_uuid.uuid4().hex[:8]}"
    t0 = time.time()
    trace = _trace_on()
    wire = [_to_wire(m) for m in messages] if trace else None
    retries = 0
    chain = fallback_chain(model)
    used = chain[0]
    if any(m.startswith("openrouter/") for m in chain):
        # ADR-0102 point 5: here and not at the turn's start, so Learn's
        # grading, the capability workers and every iteration of a turn pass
        # the same check. Raises UsageLimited before any span opens: a call
        # refused is a call never made, and the turn's error says so.
        from silica.agent.usage_limits import check

        check()

    def _on_retry(attempt: int, ceiling: int, delay: float, exc: Exception) -> None:
        # Same id, still running: both folds update the open span's summary in
        # place, so a live view reads "retry 2/3" where it read "llm <model>",
        # and the file says why a 260s call took 260s. Before this the only
        # trace of a retry was a stderr warning.
        nonlocal retries
        retries += 1
        NARRATOR.narrate(
            "call", "running",
            f"llm {used} retry {attempt}/{ceiling} in {delay:.0f}s: {str(exc)[:80]}",
            {"model": used, "attempt": attempt, "ceiling": ceiling,
             "delay_s": round(delay, 1), "error": str(exc)}, id=cid)

    NARRATOR.span_open("call", cid, f"llm {model}", {"model": model})
    try:
        for n, used in enumerate(chain):
            try:
                # The caller's key belongs to the caller's model alone: a
                # worker's leashed key must not be sent to a fallback's provider.
                resp = _call_llm(used, messages, tools, max_tokens, response_format,
                                 temperature, on_delta, openrouter_provider, cancel,
                                 api_key if used == model else None, reasoning,
                                 on_retry=_on_retry, sdk_retries=sdk_retries)
                break
            except Exception as e:
                if (n + 1 == len(chain) or not _falls_back(e)
                        or (cancel is not None and cancel.is_set())):
                    raise
                _held[used] = time.monotonic() + _FALLBACK_HOLD_S
                logger.warning("llm %s failed (%s); trying %s", used, str(e)[:120], chain[n + 1])
                NARRATOR.narrate(
                    "call", "running", f"llm {used} failed, trying {chain[n + 1]}: {str(e)[:80]}",
                    {"model": used, "next": chain[n + 1], "error": str(e)}, id=cid)
    except Exception as e:
        payload = {"model": used, "error": str(e),
                   "duration_s": round(time.time() - t0, 3), "retries": retries}
        if trace:
            payload["messages"] = wire
        NARRATOR.span_close("call", cid, "failed",
                            f"llm {used} failed: {str(e)[:80]}", payload)
        raise
    u = resp.usage or {}
    payload = {"model": used, "prompt_tokens": u.get("prompt_tokens"),
               "completion_tokens": u.get("completion_tokens"),
               "cached_tokens": _cached_tokens(u) if u else 0,
               "duration_s": round(time.time() - t0, 3),
               "finish_reason": resp.finish_reason, "retries": retries}
    if used != model:
        payload["fallback_from"] = model
    if resp.reasoning:
        # The trace lives on the call beat because this is the one record every
        # lane writes; the thought beat is the loop's and only the loop opens
        # one, so distill, judge and steer calls had no durable reasoning at
        # all. An interactive iteration now stores it twice (thought + call);
        # revisit if the doctor's 100MB-per-session warning fires on chats.
        payload["reasoning"] = resp.reasoning
    if trace:
        payload["messages"] = wire
        payload["request"] = resp.request
        payload["reply"] = {k: v for k, v in resp.assistant_message.items()
                            if k != "silica_reasoning"}
    NARRATOR.span_close(
        "call", cid, "done",
        f"llm {used} {u.get('prompt_tokens', '?')}→{u.get('completion_tokens', '?')} tok",
        payload)
    return resp


def _trace_on() -> bool:
    """SILICA_TRACE_LLM=1: the call beat also carries the wire messages, the
    request kwargs and the reply, i.e. everything needed to replay the call.
    Off by default because the loop re-sends the whole history every
    iteration, so a traced chat turn writes O(iterations x history) bytes;
    the doctor's 100MB-per-session warning is the ceiling. Read per call, not
    at import, so a REPL can export it mid-session."""
    return os.getenv("SILICA_TRACE_LLM") == "1"


def _session_open() -> bool:
    from silica.agent.narration import NARRATOR  # lazy, as the wrapper above
    return NARRATOR.sid is not None


def _call_llm(
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int | None = None,
    response_format=None,
    temperature: float | None = None,
    on_delta: Callable[[str, str], None] | None = None,
    openrouter_provider: str | None = None,
    cancel: threading.Event | None = None,
    api_key: str | None = None,
    reasoning: bool | None = None,
    on_retry=None,
    sdk_retries: int | None = None,
) -> LLMResponse:
    """Call the LLM with function-calling support.

    Args:
        model: litellm model string (e.g. "openrouter/anthropic/claude-sonnet-4-20250514")
        messages: conversation history in OpenAI format
        tools: list of tool JSON schemas (OpenAI function format)
        max_tokens: optional maximum tokens to generate
        on_delta: optional (chunk_type, content) sink; when given the call streams,
            emitting "reasoning"/"text" deltas as they arrive (plus a "reset" at the
            start of each attempt, so a mid-stream retry can clear any preview).
            A reset's content names what it retracts: "" is everything painted
            this attempt (a replay repeats the reasoning too), "text" is the
            answer alone — the loop sends that one when the turn turns out to be
            a tool call and the reasoning behind it is worth keeping on screen.
            The final LLMResponse is identical to the non-streaming path.
        cancel: optional abandonment flag, forwarded to retry_transient — set it
            when nobody is waiting on this call anymore so retries stop.
        api_key: explicit credential, overriding whatever litellm or the prefix
            blocks below would resolve. The only caller is get_provider's worker
            role, which lets a leashed sub-agent run on a separate key.
        reasoning: False asks an OpenRouter hybrid model not to think at all.
            Thinking is billed against max_tokens, so on a mechanical extraction
            with a tight budget the whole budget can go to the trace and the
            reply comes back empty (residue decompose on a 31KB source:
            3864/3864 completion tokens, zero chars of text). None = leave the
            model's default alone.

    Returns:
        LLMResponse with either text or tool_calls populated
    """
    if CONFIG.verbose:
        tool_count = len(tools) if tools else 0
        logger.info("LLM call: model=%s | msg=%d | tools=%d", model, len(messages), tool_count)

    from silica.agent.providers import (  # lazy: providers.py imports this module
        PROVIDER_PRESETS,
        clamp_max_tokens,
        ollama_num_ctx,
        _to_wire,
    )

    # The wire boundary: strips the internal `origin` field and renders the
    # <silica-cli> marker. The interactive loop calls this module directly, so
    # without it `origin` reached the provider verbatim (litellm forwards unknown
    # message keys) and the CLI marker was never applied on the chat path.
    # Idempotent, so the provider paths that already ran it are unaffected.
    jinja = model.startswith(_JINJA_ROUTES)
    if jinja:
        messages = [{**m, "reasoning_content": m["silica_reasoning"]}
                    if m.get("role") == "assistant" and m.get("silica_reasoning") else m
                    for m in messages]
    messages = [_to_wire(m) for m in messages]
    if jinja:
        messages = _one_leading_system(messages)

    input_chars = _request_chars(messages) + (len(str(tools)) if tools else 0)
    kwargs: dict = {
        "model": model,
        "messages": _with_prompt_cache(model, messages),
        "max_tokens": clamp_max_tokens(model.split("/", 1)[0], model, max_tokens, input_chars),
    }
    if sdk_retries is not None:
        # The OpenAI client retries every 5xx twice on its own, below
        # retry_transient (measured: 3 requests per 500). A caller whose ask
        # is deterministic gets the same reply back each time and says so.
        kwargs["max_retries"] = sdk_retries
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    if response_format is not None:
        kwargs["response_format"] = response_format
    if temperature is not None:
        kwargs["temperature"] = temperature
    if model.startswith("openrouter/"):
        if model in _REASONING_MANDATORY:
            reasoning = None  # the endpoint refused the knob once; don't re-pay the 400
        # Or a narration session is open: the trace is durable only through the
        # call beat, and asking for it is what makes the beat carry it. Gated
        # on the display switches alone, an MCP or batch session recorded
        # "thought (empty)" for every call (11/108 in the 2026-09-06 store).
        if reasoning is not False and (CONFIG.show_thinking or CONFIG.verbose
                                       or _session_open()):
            kwargs["include_reasoning"] = True
        kwargs["extra_headers"] = _OPENROUTER_ATTRIBUTION
        rt = openrouter_routing(openrouter_provider, model) or {}
        rt["session_id"] = _openrouter_session_id()
        if reasoning is False:
            # `enabled: False` is the knob that lands; `max_tokens: 0` is
            # accepted and ignored (measured on deepseek-v4-flash: 12208 chars
            # of trace and an empty reply either way).
            rt["reasoning"] = {"enabled": False}
        if rt:
            kwargs["extra_body"] = rt

    # Ollama: route via litellm's `ollama_chat/` provider (/api/chat — native
    # tool calls + chat templating) rather than `ollama/` (/api/generate, which
    # emulates tools by injecting JSON into the prompt). This is a tool-heavy
    # agentic loop, so the chat endpoint is the correct one. Users keep writing
    # `ollama/` in config; clamp_max_tokens above already ran on that prefix.
    if model.startswith("ollama/"):
        kwargs["model"] = "ollama_chat/" + model.split("/", 1)[1]
        # Without this the model loads at Ollama's 4096-token default and the
        # ~8k-token toolset is truncated away in silence, so the model answers in
        # prose instead of calling a tool (measured: 2051 of 6645 prompt tokens
        # kept, zero tool calls at 4096; the same request calls the right tool at
        # 8192). /api/chat is the only Ollama endpoint that accepts this.
        kwargs["num_ctx"] = ollama_num_ctx()
        # litellm resolves Ollama's endpoint from OLLAMA_API_BASE alone, so
        # without this the chat path talks to localhost while doctor and
        # model_limits honour OLLAMA_HOST and talk to the box the user configured.
        kwargs["api_base"] = PROVIDER_PRESETS["ollama"]["base_url"].removesuffix("/v1")

    # Custom OpenAI-compatible endpoint: litellm has no `custom/` provider, so
    # route via its generic openai/ path with an explicit api_base/api_key.
    if model.startswith("custom/"):
        kwargs["model"] = "openai/" + model.split("/", 1)[1]
        kwargs["api_base"] = CONFIG.provider_base_url or None
        kwargs["api_key"] = CONFIG.provider_api_key or "dummy-key"
        if reasoning is False and model not in _REASONING_MANDATORY:
            # The openrouter knob above never reached a local server, so a
            # hybrid model thought through the whole budget and answered "":
            # measured 2026-10-06 on llama-server Qwen3.5-9B, Learn's item call
            # ran 65 s to finish=length with 0 chars, 22 s and 6.8k chars with
            # this. llama.cpp, vLLM and SGLang read it; a host that 400s on
            # it is retried without (_without_refused_reasoning).
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        if temperature is None:  # a caller's value, and a schema call's 0.0, win
            from silica.config import chat_sampling

            card = chat_sampling(model, thinking=reasoning is not False or model in _REASONING_MANDATORY)
            if card:
                # top_k is not an OpenAI argument: litellm would drop or refuse it
                # top-level, llama.cpp, vLLM and SGLang read it from the body.
                kwargs.setdefault("extra_body", {})["top_k"] = card.pop("top_k")
                kwargs.update(card)

    # LM Studio: litellm's registry has no `lmstudio` (BadRequestError), and its
    # `lm_studio` dialect resolves api_base only from LM_STUDIO_API_BASE — no
    # localhost default. Same generic openai/ route, pinned to the preset
    # endpoint the OpenAI-SDK path (get_provider) already uses.
    if model.startswith("lmstudio/"):
        preset = PROVIDER_PRESETS["lmstudio"]
        kwargs["model"] = "openai/" + model.split("/", 1)[1]
        kwargs["api_base"] = preset["base_url"]
        kwargs["api_key"] = preset["api_key"]

    if api_key:
        kwargs["api_key"] = api_key  # after the prefix blocks, so an override wins
    # An explicit key is one key; otherwise the provider's pool, which sets
    # kwargs["api_key"] per try and is a no-op below two keys.
    keys = [] if api_key else key_pool(model)

    # litellm's own (fires first if it works); _bounded is the backstop, 10 s
    # later. Tied to SILICA_LLM_TIMEOUT: a fixed 120 here made the documented
    # override (=400 for a slow reasoning model) unable to lengthen any call,
    # and four thinking judge calls on a local 9B timed out at 120 s (2026-10-08).
    kwargs["timeout"] = _LOCAL_LLM_TIMEOUT - 10

    # api_key is a credential and extra_headers is the fixed attribution block:
    # neither belongs in a file. Tool schemas collapse to names because the
    # ~8k-token toolset would otherwise be repeated on every traced call.
    request = {k: v for k, v in kwargs.items()
               if k not in ("messages", "api_key", "extra_headers")}
    if tools:
        request["tools"] = [t.get("function", {}).get("name", "?") for t in tools]
    request["stream"] = on_delta is not None

    lm = _litellm()
    _TRANSIENT = (
        lm.Timeout,
        lm.APIConnectionError,
        lm.RateLimitError,
        lm.ServiceUnavailableError,
        lm.BadGatewayError,
    )
    # The reassembled message is litellm's business and it does not always carry
    # reasoning back; on the streaming path the deltas are the only place it is
    # guaranteed to exist, so keep them for the history.
    streamed_reasoning: list[str] = []
    if on_delta is None:
        response = _without_refused_reasoning(
            model, kwargs,
            lambda: retry_transient(
                lambda: _with_keys(keys, kwargs, lambda: _bounded(
                    lambda: lm.completion(**kwargs), _LOCAL_LLM_TIMEOUT, model)),
                _TRANSIENT, cancel=cancel, on_retry=on_retry))
    else:
        def _stream_once():
            on_delta("reset", "")
            streamed_reasoning.clear()  # a retry replays the whole trace
            chunks = []
            # include_usage: without it the provider sends no usage chunk and
            # stream_chunk_builder falls back to counting tokens locally, so the
            # token meter reports an estimate and `cached_tokens` — the only way
            # to verify the breakpoints above are hitting — never arrives.
            # Providers that don't support it (ollama_chat) drop it themselves.
            _stream = _bounded_stream(
                lambda: lm.completion(
                    **kwargs, stream=True, stream_options={"include_usage": True}),
                _LOCAL_LLM_TIMEOUT, model)
            for chunk in _stream:
                chunks.append(chunk)
                try:
                    delta = chunk.choices[0].delta
                except (IndexError, AttributeError):
                    continue  # usage-only / malformed trailing chunk
                r = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                if isinstance(r, str) and r:
                    streamed_reasoning.append(r)
                    on_delta("reasoning", r)
                c = getattr(delta, "content", None)
                if isinstance(c, str) and c:
                    on_delta("text", c)
            # Reassemble the canonical response (content, tool_calls, usage) so
            # everything below is identical to the non-streaming path.
            built = lm.stream_chunk_builder(chunks, messages=messages)
            _reject_cut_stream(built, streamed_reasoning, model)
            return built

        response = _without_refused_reasoning(
            model, kwargs, lambda: retry_transient(
                lambda: _with_keys(keys, kwargs, _stream_once),
                _TRANSIENT, cancel=cancel, on_retry=on_retry))
        if response is None:
            raise RuntimeError(f"LLM stream from {model} produced no chunks")

    choice = response.choices[0]
    message = choice.message
    finish_reason = getattr(choice, "finish_reason", None)

    # Extract the model's reasoning trace
    trace = getattr(message, "reasoning_content", None)
    if not isinstance(trace, str):
        trace = getattr(message, "reasoning", None)
    if not isinstance(trace, str) and isinstance(message, dict):
        trace = message.get("reasoning_content") or message.get("reasoning")
    if not isinstance(trace, str):
        trace = None

    blocks = getattr(message, "thinking_blocks", None)
    if not trace and isinstance(blocks, list):
        trace = "\n".join(b.get("thinking", "") for b in blocks if isinstance(b, dict))
    if not trace and streamed_reasoning:
        trace = "".join(streamed_reasoning)

    # Build the assistant message dict for conversation history
    raw = ([(tc.id, tc.function.name, tc.function.arguments) for tc in message.tool_calls]
           if message.tool_calls else None)
    assistant_msg, parsed_calls = build_assistant_message(message.content, raw, trace)
    # The trace is kept under an INTERNAL key, never `reasoning_content`: that
    # name is re-sent on every later iteration of the tool loop (litellm forwards
    # it verbatim, and ollama_chat maps it to `thinking`), which re-bills a
    # multi-thousand-token trace once per iteration for nothing — no provider but
    # Anthropic consumes it, and Anthropic wants thinking_blocks. `_to_wire`
    # strips this one at the wire boundary, so it costs the provider nothing and
    # a reopened chat can still show the thinking that produced the answer.
    if trace:
        assistant_msg["silica_reasoning"] = trace
        # Sole observability for non-interactive calls (distiller, steer,
        # subagents): only the interactive loop emits ReasoningEvent, so
        # outside it the trace parked above is never read. A -v run logs it.
        logger.debug("LLM reasoning: %s", trace)
    if isinstance(blocks, list):
        assistant_msg["thinking_blocks"] = blocks

    if CONFIG.verbose:
        text_preview = (message.content or "")[:80].replace("\n", " ")
        logger.info(
            "LLM resp: finish=%s | tool_calls=%d | text=%r",
            finish_reason,
            len(parsed_calls),
            text_preview + ("…" if len(message.content or "") > 80 else ""),
        )

    return LLMResponse(
        text=message.content,
        tool_calls=parsed_calls,
        assistant_message=assistant_msg,
        usage=dict(response.usage) if response.usage else {},
        reasoning=trace,
        finish_reason=finish_reason,
        request=request,
    )
