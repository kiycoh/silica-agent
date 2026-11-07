# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The agentic loop — the core of Silica.

This is the 'while True' from SILICA.md §8.1:
  loop:
    response = LLM(system_prompt, message_history, tool_schemas)
    if response has tool_calls:
        for each tool_call:
            result = execute_tool(name, args)
            append tool_result to history
        continue  (re-call LLM with results)
    else:
        return response text to user

Everything else (streaming, TUI, context compression) is ergonomics
around this nucleus. Build this first, then ergonomics.
"""
from __future__ import annotations

from typing import Callable, Any
import concurrent.futures as _cf
import threading
import time
import logging
import json

from silica.obs.events import (
    ToolStartEvent,
    ToolCompleteEvent,
    ToolErrorEvent,
    ReasoningEvent,
    RenderEvent,
    ThinkingStartEvent,
    ThinkingEndEvent,
    LLMStreamEvent,
)
from silica.model.errors import context_overflow
from silica.model.llm import call_llm, run_with_deadline
from silica.obs import narration as _narr_mod
from silica.agent.compaction import (
    COMPACT_FLOOR_TURNS,
    COMPACT_FRACTION,
    compact_read_history,
    eager_stub,
    fit_window,
)
from silica.model.limits import ROUGH_CHARS_PER_TOKEN, TEXT_CHARS_PER_TOKEN
from silica.agent import memory_gate, read_scope
from silica.agent.relevance import compact_by_relevance
from silica.agent.run_context import RunContext
from silica.agent.sentinels import CANCELLED, EMPTY_COMPLETION, MAX_ITERATIONS, is_sentinel
from silica.config import CONFIG
from silica.model.concurrency import worker_slot
from silica.agent.constraints import AgentConstraints
from silica.tools import MODEL_CALL, TOOLS, Tool, found_nothing, tool_failed
from contextlib import nullcontext

logger = logging.getLogger(__name__)


ToolProgressCallback = Callable[[RenderEvent], None] | None


# How many steps from the wall the model is told about it. Silent above this:
# a budget line on every iteration is noise the model learns to skip, and the
# number only changes the right move once finishing IS the right move.
BUDGET_NOTICE_AT = 2
# Iterations of a turn no constraint capped: the hard safety net, lowered from 50.
# The lanes that hold a turn tighter name their own beside their reasons
# (constraints, connectors.mcp_tools, profiles, ANNEAL_STEER_ITERATIONS).
DEFAULT_MAX_ITERATIONS = 20
# Prompt tokens one turn may spend across its LLM calls before it lands with
# the tool-less final turn. The iteration cap alone let a factual question
# spend 300k tokens re-reading one lecture (12 of 26 live runs, 2026-09-22);
# the number is the probe's cap that caught them, with headroom. Config
# promotion declined until a real vault needs another value.
TURN_PROMPT_BUDGET = 400_000
# The request goes out at most this full: the rest is the answer's room and the
# estimate's slack. llama.cpp rejects a prompt over n_ctx outright.
WINDOW_FILL = 0.9
# That rejection names both counts (errors.context_overflow). The loop collapses
# reads to fit the real count and resends once, so an estimate that came in low
# (text under 2 chars a token, limits.TEXT_CHARS_PER_TOKEN) costs a stub instead
# of the turn.
# A note read this turn that will not fit is served as its passages densest in
# the words of the read's query, or of the request when it gave none, instead
# of a stub naming a re-read that cannot fit either (compaction.fit_window).
# The probe's `fitnarrow` arm (Tool.shrink); off until it is measured against the
# stub. Not refuted: the re-read loop it answers did not reproduce (2026-10-15).
# Delete it by 2026-12-31 unless n>=12 per arm moves the return turns.
FIT_NARROWS = False


def _fit_shrink(name: str, arguments: str, body: str) -> str | None:
    shrink = getattr(TOOLS.get(name), "shrink", None)
    if not FIT_NARROWS or shrink is None:
        return None
    try:
        args = json.loads(arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(args, dict):
        return None
    requests = read_scope.REQUESTS.get()
    try:
        return shrink(args, requests[-1] if requests else "")
    except Exception as exc:
        # The note moved or the driver failed between the read and this fit:
        # the stub the caller falls back to is what the fit wrote before.
        logger.warning("fit narrowing failed for %s (%s): stubbed", name, exc)
        return None

_FINAL_TURN_INSTRUCTION = (
    "The tool phase of this turn is over and no tools are available now. "
    "Answer from what you already have: report what you found, and name any "
    "note you created or edited. Do not request, announce, or simulate another "
    "tool call. If the work is incomplete, say plainly what is missing."
)



def _budget_notice(iteration: int, max_iterations: int,
                   token_steps: int | None = None) -> dict | None:
    """Tell the model how much of the loop is left, at the point where the
    answer changes.

    The loop used to stop dead at the cap having never told the model a cap
    existed, so it could spend its last steps opening work it had no room to
    close. Silent while there is room; a system notice near the wall, on the
    same channel as the convergence guard so tool results stay contiguous.

    `token_steps` is how many more calls of the last one's size fit in
    TURN_PROMPT_BUDGET: that wall stopped hosted t06's turn (2026-10-22) at 19
    of its iterations with no notice, so the nearer of the two is the one told
    (ADR-0113, amendment 2026-10-23).
    """
    remaining = max_iterations - iteration
    if token_steps is not None:
        remaining = min(remaining, token_steps)
    if remaining > BUDGET_NOTICE_AT:
        return None
    return {
        "role": "system",
        "content": (
            f"Budget: {remaining} tool step(s) remain in this turn. Prefer to "
            f"finish now and report what you have, including anything already "
            f"written. Use another tool only when it is strictly required to "
            f"complete work you have already started."
        ),
    }


def repair_tool_call_history(messages: list[dict]) -> int:
    """Ensure every assistant `tool_calls` block is answered by a tool result.

    An interrupt (KeyboardInterrupt is a BaseException, uncaught by the dispatch
    loop) or the convergence abort can exit mid-dispatch, leaving an assistant
    message whose tool_calls have no matching `tool` responses. The next
    `call_llm` then rejects the orphaned block with a 400 and the session is dead
    until /clear. Insert a synthetic error result for each unanswered id, right
    after that block's existing tool responses. Idempotent. Returns count inserted.
    """
    inserted = 0
    i = 0
    while i < len(messages):
        m = messages[i]
        calls = m.get("tool_calls") if m.get("role") == "assistant" else None
        if calls:
            j = i + 1
            answered: set = set()
            while j < len(messages) and messages[j].get("role") == "tool":
                answered.add(messages[j].get("tool_call_id"))
                j += 1
            missing = [c.get("id") for c in calls if c.get("id") not in answered]
            for k, cid in enumerate(missing):
                messages.insert(j + k, {
                    "role": "tool", "tool_call_id": cid,
                    "content": '{"error": "tool call interrupted before it produced a result"}',
                })
            inserted += len(missing)
            i = j + len(missing)
        else:
            i += 1
    return inserted


def identical_prior_calls(messages: list[dict], name: str, args_str: str) -> int:
    """How many times (name, args) already appears as an assistant tool_call.

    Reuse probe: a result the chat computed, spent, and now asks for again. Reads
    the history instead of a counter so it spans turns (the caller accumulates
    `messages`), and `args_str` must be the canonical json.dumps(sort_keys=True)
    form since providers echo their own spacing back.
    """
    n = 0
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if fn.get("name") != name:
                continue
            try:
                prior = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if json.dumps(prior, sort_keys=True) == args_str:
                n += 1
    return n


# What an image part costs the window, in characters of the chars/4 estimate:
# about 1,000 tokens, what Qwen-VL asks for at grounding resolution (ADR-0077).
# Counted as its base64, a 200 KB PNG read as 70,000 tokens and collapsed the
# history for nothing.
_IMAGE_CHARS = 4000


def _content_chars(content) -> int:
    """Characters a message's content stands for in the window estimate."""
    if isinstance(content, list):
        return sum(len(p.get("text") or "") if p.get("type") == "text" else _IMAGE_CHARS
                   for p in content if isinstance(p, dict))
    return len(str(content or ""))


class _Stopped(Exception):
    """The turn's cancel token was set while the model was still writing."""


# ADR-0150: how many reads of one tool block run at once (Tool.parallel_safe).
# The Obsidian bridge correlates concurrent RPCs by id. A block with anything
# else in it, a write above all, runs one call at a time as before.
_READ_WORKERS = 4


def _run_read(tool: Tool, tc: Any, cancel_token: Any) -> tuple[Any, BaseException | None, float, list[str]]:
    """One read of a parallel block, in its own copy of the turn's context:
    (result, the exception it raised, seconds, the paths it returned)."""
    if cancel_token is not None and cancel_token.is_set():
        # Stop came before this read began: it never runs, and the loop has
        # already left the block (ADR-0121 point 7, ADR-0150 point 5).
        return '{"error": "stopped before it ran"}', None, 0.0, []
    returned = memory_gate.own_returns()
    MODEL_CALL.set(tc.name)
    t0 = time.perf_counter()
    try:
        return tool.run(_cancel_token=cancel_token, **tc.args), None, time.perf_counter() - t0, returned
    except Exception as e:  # handed to the loop, which reports it as the serial path does
        return None, e, time.perf_counter() - t0, returned


def _prefetch_reads(tool_calls: list, allowed: dict[str, Tool], cancel_token: Any) -> dict[int, _cf.Future]:
    """Start every call of a block of two or more parallel reads at once:
    {id(call): future}, or {} and the block runs serially. The loop still
    takes the calls in order, so events, narration and history read exactly as
    before, only sooner.

    Tool.once_per_block is silica_recall: its mark_used re-reads and saves the
    episodic file, and two in flight would lose one's TTL refresh.
    """
    names = [tc.name for tc in tool_calls]
    tools = [allowed[n] for n in names if n in allowed]
    if (len(names) < 2 or len(tools) < len(names) or not all(t.parallel_safe for t in tools)
            or any(t.once_per_block and names.count(n) > 1 for n, t in zip(names, tools))):
        return {}
    pool = _cf.ThreadPoolExecutor(max_workers=min(len(names), _READ_WORKERS),
                                  thread_name_prefix="silica-read")
    read = RunContext.here(narrated=True).spawn(_run_read)
    futures = {id(tc): pool.submit(read, allowed[tc.name], tc, cancel_token) for tc in tool_calls}
    pool.shutdown(wait=False)  # the threads end with their call; nothing waits on the pool
    return futures


def run_agent(
    messages: list[dict],
    model: str,
    tool_progress_callback: ToolProgressCallback = None,
    cancel_token: "threading.Event | None" = None,
    constraints: "AgentConstraints | None" = None,
    temperature: float | None = None,
) -> str:
    """Execute the agentic loop until the model produces a text response.

    The loop calls the LLM, dispatches any tool calls, appends results,
    and re-calls until the model responds with text (no tool calls).

    Args:
        messages: mutable conversation history (modified in-place)
        model: litellm model string
        tool_progress_callback: callback for tool progress events

    Returns:
        The model's final text response
    """
    # A chat turn: no constraints at all, or a constrained caller that says so.
    # Everything a chat turn has and a worker has not reads this one fact.
    _interactive = constraints is None or constraints.interactive

    if constraints is not None:
        source = TOOLS if constraints.registry is None else constraints.registry
        allowed: dict[str, "Tool"] = {
            name: source[name] for name in constraints.tools if name in source
        }
    else:
        # Non-ambient authority: the main agent's default toolset excludes
        # sensitive tools (ADR-0009 / ADR-0015) and pipeline internals the FSM
        # drives programmatically. Both are reachable only when a caller names
        # them in AgentConstraints.tools.
        allowed = {n: t for n, t in TOOLS.items() if not t.sensitive and not t.internal}

    # Permitted, not offered (ADR-0050): a tool joins `allowed` and `schemas`
    # when silica_load_tools names it or the model calls it directly. Only
    # ever appended, so the part of the tool block the last request sent is
    # still the head of this one and a prefix cache keeps it.
    deferred: dict[str, "Tool"] = (
        {n: TOOLS[n] for n in constraints.deferred if n in TOOLS and n not in allowed}
        if constraints is not None and allowed else {})
    # What the turn can reach, offered or one load away: a schema's clause
    # that points at a tool outside it is cut (tools.pointed). Taken once, so
    # a load never changes a schema the last request already sent.
    reach = frozenset(allowed) | frozenset(deferred)

    schemas = [t.json_schema(reach) for t in allowed.values()] if allowed else None

    def _load(name: str) -> None:
        tool = deferred.pop(name, None)
        if tool is not None:
            assert schemas is not None  # deferred is empty unless `allowed` built schemas
            allowed[name] = tool
            schemas.append(tool.json_schema(reach))

    effective_model = (
        constraints.model if (constraints is not None and constraints.model) else model
    )

    # A33: a prior turn interrupted (Ctrl+C, BaseException) or convergence-aborted
    # mid-dispatch can leave an assistant tool_calls block with unanswered ids;
    # the first call_llm below would then 400 the whole session. Self-heal on entry.
    repair_tool_call_history(messages)
    # Once per user turn, before the first request of it: the new request is
    # what puts an earlier read out of scope, and a rewrite here costs one
    # cache break for the turn instead of one per iteration (relevance.py).
    # A batch run has a single user message and nothing to judge.
    if _interactive:
        compact_by_relevance(messages, TOOLS)
    # What a tool may need to know about the turn it serves (read_scope.py).
    read_scope.set_requests(messages)
    if _interactive:
        # A chat turn opens here: a create at a path the chat deleted since is
        # judged against the deleted body (ADR-0093). A batch sub-loop opens
        # none, so it cannot shrink the window of the chat turn it runs in.
        from silica.kernel.write import preserve

        preserve.begin_turn()
    # Not read_scope's REQUESTS for the user-memory turn (ADR-0076): those
    # strings drop `origin`, and the forget-only check must not read a
    # directive's own words as the user's.
    memory_turn = memory_gate.begin_turn(messages, chat=_interactive)
    # ponytail: a single meter for every tool. Split retrieval from writes if a
    # turn is ever seen spending its whole allowance on reads, or vice versa.

    iteration = 0
    max_iterations = (
        constraints.max_iterations
        if (constraints is not None and constraints.max_iterations is not None)
        else DEFAULT_MAX_ITERATIONS
    )

    consecutive_failures: dict[tuple[str, str], int] = {}
    # Excerpts recall already delivered this turn (compaction.dedup_recall_excerpts).
    recalled_excerpts: set[str] = set()
    # Set when the convergence guard stops the loop: the run lands with the
    # same tool-less final turn as the iteration cap, instead of discarding
    # everything it found behind a RuntimeError.
    convergence_landing = False
    spent_prompt = 0  # cumulative prompt tokens of this turn (TURN_PROMPT_BUDGET)

    # Message indices already elided by this run's compaction sweeps. The callers
    # keep their own set across turns; the two never conflict because a stub is
    # shorter than MIN_COLLAPSE_CHARS, so a second sweep skips it.
    collapsed: set[int] = set()
    # What the provider counted for the last request and how long the history
    # was then: the next request is that count plus what was appended since.
    sent_tokens, sent_len = 0, 0

    def _emit(event: RenderEvent) -> None:
        """Best-effort event emission to the callback and the narration."""
        if tool_progress_callback is not None:
            try:
                tool_progress_callback(event)
            except Exception as exc:
                logger.debug("tool_progress_callback error (swallowed): %s", exc)
        # Durable record: one adapter call, all render-event kinds (spec §4).
        # Best-effort like the callback above — narration must never kill a turn.
        try:
            _narr_mod.NARRATOR.on_render_event(event)
        except Exception as exc:
            logger.debug("narration adapter error (swallowed): %s", exc)

    # Set once the main thread stops waiting on the LLM (Ctrl+C or normal return).
    # The LLM runs on a detached daemon thread that keeps streaming deltas after an
    # interrupt; this gate stops those late deltas from re-opening the live region
    # and printing thinking below "(interrupted)". Also passed as `cancel` to abort
    # retries. Cleared at the top of each iteration.
    _abandon = threading.Event()

    def _stream_delta(chunk_type: str, content: str) -> None:
        # Called from the LLM worker thread; `iteration` reads the current loop pass.
        if _abandon.is_set():
            return
        _emit(LLMStreamEvent(chunk_type=chunk_type, content=content, iteration=iteration))

    # Streaming is a TUI ergonomic: only interactive turns get it, and a sub-loop
    # whose text can become the reply (constraints.stream, the web door's lane);
    # worker and batch runs stay on the plain non-streaming call. A constrained
    # toolset no longer implies "worker": an interactive caller that wants the
    # chat_tools cut says so with constraints.interactive.
    # The kwarg is only passed when active, so call_llm test doubles only need
    # the bare signature plus cancel=None.
    _llm_kwargs: dict = {"tools": None}
    if temperature is not None:
        # None keeps the provider default (product behavior). Eval agent arms
        # pin 0.0 so a single-run A/B measures the lever, not sampling noise.
        _llm_kwargs["temperature"] = temperature
    if tool_progress_callback is not None and (
            _interactive or (constraints is not None and constraints.stream)):
        _llm_kwargs["on_delta"] = _stream_delta

    def _interruptible_llm(kwargs: dict):
        """One call_llm off the loop's thread, so a Ctrl+C reaches the turn and a
        Stop is seen within 0.2 s and not when the model finishes. Caller clears
        `_abandon` first; leaving sets it, which ends the stream at its next
        chunk and stops an orphaned worker retrying. sync litellm can't abort
        the in-flight HTTP request: best we can do is stop waiting and stop
        retrying."""
        # A named frame and not a lambda: it is the token meter's call-site label
        # for the loop, and the hosted llm_call lines carry it. Narrated, or a
        # loop run by a tool leaves its `call` beats outside the tool's span.
        def _llm_worker(k=dict(kwargs)):
            return call_llm(effective_model, messages, **k)

        return run_with_deadline(
            RunContext.here(narrated=True).spawn(_llm_worker), None, catch=BaseException, abandon=_abandon,
            stop=cancel_token if cancel_token is not None else threading.Event(), on_stop=_Stopped)

    def _reask_without_reasoning(reply, kwargs: dict):
        """`reply`, or when the answer went into the reasoning and the reply came
        back empty, the same call once more with reasoning off, the knob V4
        honours (llm.py). Seen on hosted t06's landing (2026-10-22), 2.8k chars
        of answer there under "maximum iterations", and in a chapter rework
        probe's last step at 64k the same night. A tool call counts only where
        tools were on offer: the landing turn has none to run."""
        if ((reply.tool_calls and kwargs.get("tools")) or (reply.text or "").strip()
                or not (reply.reasoning or "").strip()):
            return reply
        _abandon.clear()
        return _interruptible_llm({**kwargs, "reasoning": False})

    while iteration < max_iterations:
        if cancel_token is not None and cancel_token.is_set():
            logger.info("Agent loop cancelled at iteration %d", iteration)
            return CANCELLED
        iteration += 1
        logger.debug("Agent loop iteration %d", iteration)

        if sent_tokens:
            grown = sum(_content_chars(m.get("content")) + len(json.dumps(m.get("tool_calls") or ""))
                        for m in messages[sent_len:])
            _pre_collapsed = set(collapsed)
            collapsed, _ = fit_window(
                messages, collapsed,
                estimate=sent_tokens + grown // TEXT_CHARS_PER_TOKEN,
                limit=int(WINDOW_FILL * CONFIG.max_context_tokens),
                tools=TOOLS, shrink=_fit_shrink, unsent_from=sent_len,
            )
            _newly = sorted(set(collapsed) - _pre_collapsed)
            if _newly:
                recalled_excerpts.clear()
                _narr_mod.NARRATOR.narrate("compaction", "done",
                                  f"compacted {len(_newly)} message(s) to fit the window",
                                  {"indices": _newly, "prompt_tokens": sent_tokens},
                                  parent=None)

        _emit(ThinkingStartEvent(iteration=iteration))
        try:
            slot = nullcontext() if _interactive else worker_slot()
            with slot:
                _abandon.clear()
                _llm_kwargs["tools"] = schemas
                _llm_kwargs["cancel"] = _abandon
                # A request the server rejects shows the model nothing: what it
                # has seen is what the last accepted one carried.
                seen_len, sent_len = sent_len, len(messages)
                try:
                    resp = _interruptible_llm(_llm_kwargs)
                except Exception as e:
                    over = context_overflow(e)
                    if over is None:
                        raise
                    sent, window = over
                    _pre_collapsed = set(collapsed)
                    collapsed, _ = fit_window(
                        messages, collapsed, estimate=sent,
                        limit=int(WINDOW_FILL * window), tools=TOOLS,
                        shrink=_fit_shrink, unsent_from=seen_len,
                    )
                    if collapsed == _pre_collapsed:
                        raise
                    recalled_excerpts.clear()
                    _narr_mod.NARRATOR.narrate(
                        "compaction", "done",
                        f"server rejected {sent} tokens on {window}: compacted "
                        f"{len(collapsed - _pre_collapsed)} message(s) and resent",
                        {"indices": sorted(collapsed - _pre_collapsed)}, parent=None)
                    _abandon.clear()
                    resp = _interruptible_llm(_llm_kwargs)
        except _Stopped:
            logger.info("Agent loop stopped during the model call at iteration %d", iteration)
            _narr_mod.NARRATOR.thought_close("")   # the thought this call opened ends here
            return CANCELLED
        except BaseException:
            # A call that raises never reaches thought_close below, and a thought
            # left open reads as a run still going to every projection of the
            # narration until the next turn opens one: the drawer's Chats mark
            # stayed live after a refused endpoint (2026-10-21, ADR-0122).
            _narr_mod.NARRATOR.thought_close("", status="failed")
            raise
        finally:
            _emit(ThinkingEndEvent(iteration=iteration))
        thought = resp.reasoning or ""
        try:
            resp = _reask_without_reasoning(resp, _llm_kwargs)
        except _Stopped:
            _narr_mod.NARRATOR.thought_close(thought)
            return CANCELLED
        except Exception as e:
            # The empty reply stands, and the caller names it.
            logger.warning("re-asking an answer left in the reasoning failed: %s", e)
        # Closed here and not in the adapter: the reasoning text only exists
        # once resp does, and the durable thought carries it whole (spec §4).
        _narr_mod.NARRATOR.thought_close(thought or resp.reasoning or "")
        messages.append(resp.assistant_message)
        # Only intermediate (tool-calling) assistant messages narrate here.
        # The FINAL one is narrated by the caller after attribution mutates
        # messages[-1] in place (WebTurn.attribute appends the Sources block):
        # narrating it now would freeze the pre-citation text into the replay.
        if resp.tool_calls:
            _narr_mod.NARRATOR.turn(resp.assistant_message)

        if resp.reasoning:
            _emit(ReasoningEvent(text=resp.reasoning, iteration=iteration))

        # No tool calls → model produced a final text response, whatever its
        # reasoning names. A nudge that resent a first step whose reasoning
        # named an offered tool was REMOVED 2026-10-15: matching names in free
        # text cannot tell "I will call X now" from "I will call X after the
        # reader answers", and /quiz, whose prompt names silica_record_quiz for
        # the next turn, had three finished questions retracted on hosted t01
        # (2026-10-14). The stop it targeted was sampling (2026-09-27,
        # qwen3.5-9b: the replay called a tool 110/110). The Anthropic and
        # OpenAI loops end on the protocol signal alone too; if that stop comes
        # back, answer it in the prompt, not by reading the model's text.
        if not resp.tool_calls:
            # ...or produced NOTHING: measured 2026-08-23 on
            # openrouter/stealth/ox-alpha, finish=stop with tool_calls=0 and
            # content='' after 21s. `or ""` sent that straight into the REPL's
            # `if answer:`, which prints nothing — a failed turn rendered
            # exactly like no turn at all. Same shape as the cancelled and
            # max-iterations sentinels: the caller always gets text back.
            # strip(): glm-5.3-flash answered " " on 2026-10-13, and a truthy
            # space walked past `or` into an empty bubble.
            answer = resp.text if (resp.text or "").strip() else EMPTY_COMPLETION
            return _declare_links(answer) if _interactive else answer

        # Tool calls, so whatever text this iteration streamed was a preamble and
        # not the answer — but its deltas are already painted. Retract them with
        # the signal the stream contract already carries (llm.py sends the same
        # event when a transient retry replays an attempt); content names the
        # scope, so a renderer keeps the reasoning that produced the call.
        # Must precede the first ToolStartEvent below: the TUI's live region is
        # shared, and a reset arriving after a tool line would clear a region
        # that has already moved on. Guarded on on_delta because streaming is
        # only wired for the interactive loop — a worker run must not emit an
        # event nobody subscribed to. _emit and not _stream_delta: _abandon is
        # already set by the time the call returns.
        if _llm_kwargs.get("on_delta") is not None:
            _emit(LLMStreamEvent(chunk_type="reset", content="text", iteration=iteration))

        # The loop is where the history explodes — a single fat read can add
        # thousands of tokens, and every later iteration re-sends it. The
        # callers only sweep once run_agent has already returned, so on a local
        # backend a long turn could overrun the pinned window mid-flight and get
        # truncated in silence. `prompt_tokens` is the provider's own count of
        # what we just sent, so this costs no tokenizer pass; when a provider
        # reports no usage, fall back to the same rough estimate the meter uses.
        prompt_tokens = resp.usage.get("prompt_tokens") or sum(
            _content_chars(m.get("content")) for m in messages
        ) // ROUGH_CHARS_PER_TOKEN
        spent_prompt += prompt_tokens
        sent_tokens = prompt_tokens
        _pre_collapsed = set(collapsed)
        collapsed = compact_read_history(
            messages,
            collapsed,
            prompt_tokens=prompt_tokens,
            budget=int(COMPACT_FRACTION * CONFIG.max_context_tokens),
            floor_turns=COMPACT_FLOOR_TURNS,
            tools=TOOLS,
        )
        _newly = sorted(set(collapsed) - _pre_collapsed)
        if _newly:
            # A collapsed recall result no longer holds its excerpts, so the
            # turn memo must forget them: a repeat after this point arrives
            # verbatim instead of as a stub pointing at elided text.
            recalled_excerpts.clear()
            # Without this beat the turn beats alone cannot reconstruct the
            # live context (ticket 05): the elision would be invisible.
            _narr_mod.NARRATOR.narrate("compaction", "done",
                              f"compacted {len(_newly)} message(s)",
                              {"indices": _newly, "prompt_tokens": prompt_tokens},
                              parent=None)

        final_answer: dict | None = None  # a `final` web answer's tool message
        pending_notices: list[dict] = []  # A34: convergence warnings, flushed AFTER
        for tc in resp.tool_calls:
            _load(tc.name)
        prefetched = _prefetch_reads(resp.tool_calls, allowed, cancel_token)
        for tc in resp.tool_calls:        # the tool block, never between sibling results
            if cancel_token is not None and cancel_token.is_set():
                # No further tool of the batch once Stop is pressed (ADR-0121
                # point 7); the ids left unanswered are healed on the next
                # entry (repair_tool_call_history), and the loop's top returns.
                break
            logger.info("Tool call: %s(%s)", tc.name, tc.args)

            args_str = json.dumps(tc.args, sort_keys=True)
            tool_key = (tc.name, args_str)

            # Reuse probe. The current call is already in `messages`, so
            # >1 means this exact result was produced earlier in the chat and is
            # being recomputed — either a plain re-ask or compaction's read_stub
            # telling the model to re-call. Grep "reuse-probe" over a session log
            # to size the waste before caching anything.
            _seen = identical_prior_calls(messages, tc.name, args_str)
            if _seen > 1:
                logger.info("reuse-probe: %s recomputed (call #%d) with identical args: %s",
                            tc.name, _seen, args_str[:200])

            failed = False
            _load(tc.name)
            if tc.name not in allowed:
                failed = True
                result = f'{{"error": "Unknown or forbidden tool: {tc.name}"}}'
                _emit(
                    ToolErrorEvent(
                        name=tc.name,
                        call_id=tc.id,
                        error=f"Unknown or forbidden tool: {tc.name}",
                        iteration=iteration,
                    )
                )
            else:
                _emit(
                    ToolStartEvent(
                        name=tc.name,
                        args=tc.args,
                        call_id=tc.id,
                        iteration=iteration,
                    )
                )
                start_time = time.perf_counter()
                progress_cb = tool_progress_callback
                # A web answer streams into the reply only when it will be the
                # reply (ADR-0124 amendment): `final`, the last call of its
                # block, and nothing found so far for the chat model to add.
                # A call after it could still find something, and its row,
                # drawn under the streamed text, would put that text beyond a
                # reset. If the turn goes on anyway (the door failed, a
                # convergence landing), the next model call opens with a reset
                # that takes the text back (llm.py).
                reply_flag = allowed[tc.name].reply_flag
                if progress_cb is not None and reply_flag and not (
                        _interactive and (tc.args or {}).get(reply_flag)
                        and tc is resp.tool_calls[-1] and _stands_alone(messages, None)):
                    progress_cb = _without_stream(progress_cb)
                try:
                    # The frontend callback, deliberately not `_emit`: `_emit`
                    # also writes the narration, and a tool that runs a loop of
                    # its own has its own `_emit` writing there already, so
                    # forwarding that one would record every inner event twice.
                    ahead = prefetched.get(id(tc))
                    if ahead is not None:
                        # Started with its siblings (ADR-0150): its own run
                        # time, and its paths credited to it, not the block.
                        result, raised, duration, paths = ahead.result()
                        if memory_turn is not None:
                            memory_turn.returned.extend(paths)
                        if raised is not None:
                            raise raised
                    else:
                        model_call = MODEL_CALL.set(tc.name)
                        try:
                            result = allowed[tc.name].run(
                                _cancel_token=cancel_token,
                                _progress=progress_cb,
                                **tc.args,
                            )
                        finally:
                            MODEL_CALL.reset(model_call)
                        duration = time.perf_counter() - start_time
                    _emit(
                        ToolCompleteEvent(
                            name=tc.name,
                            args=tc.args,
                            call_id=tc.id,
                            result=result,
                            duration_s=duration,
                            iteration=iteration,
                        )
                    )
                    if tool_failed(result):
                        failed = True
                except Exception as e:
                    duration = time.perf_counter() - start_time
                    _emit(
                        ToolErrorEvent(
                            name=tc.name,
                            call_id=tc.id,
                            error=str(e),
                            iteration=iteration,
                        )
                    )
                    failed = True
                    result = f'{{"error": "{type(e).__name__}: {str(e)}"}}'
            # Failed or not: a fetch that errored halfway may still have put
            # page text in its error, and the gate errs toward refusing.
            taint = memory_gate.after_tool(memory_turn, tc.name)

            # Eager projection: a write/gate tool's fat JSON never enters the
            # history — the TUI already got the full result via the event above.
            # Errors stay verbatim so the model can react to them.
            ran = None if failed else allowed.get(tc.name)
            if ran is not None and ran.collapse == "eager":
                result = eager_stub(ran, result)
            if ran is not None and ran.history_view is not None:
                result = ran.history_view(result, recalled_excerpts)
            if ran is not None and ran.loads is not None:
                for name in ran.loads(tc.args or {}, result):
                    if isinstance(name, str):
                        _load(name)

            tool_msg = {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            }
            if taint:
                # A taint learnt from a returned path (ADR-0090 point 8) has
                # no tool name to find again at the next turn's start.
                tool_msg[memory_gate.TAINT_KEY] = taint
            messages.append(tool_msg)
            _narr_mod.NARRATOR.turn(tool_msg)
            if (ran is not None and ran.reply_flag and (tc.args or {}).get(ran.reply_flag)
                    and not is_sentinel(str(result))):
                final_answer = tool_msg

            if failed:
                consecutive_failures[tool_key] = consecutive_failures.get(tool_key, 0) + 1
                failures_count = consecutive_failures[tool_key]
                if failures_count >= 3:
                    logger.error("Convergence guard: tool '%s' with args %s failed %d times consecutively. Aborting agent run.", tc.name, tc.args, failures_count)
                    # Stop the loop, keep the run: a deep research turn that
                    # stubs its toes on one tool (measured live: a re-pasted
                    # non-verbatim `remember` quote, three times) still holds
                    # 40+ steps of findings worth answering from.
                    convergence_landing = True
                    break
                elif failures_count == 2:
                    logger.warning("Convergence guard: tool '%s' with args %s failed consecutively. Injecting warning message.", tc.name, tc.args)
                    pending_notices.append(
                        {
                            "role": "system",
                            "content": f"IMPORTANT: Tool '{tc.name}' failed consecutively with these parameters. DO NOT call this tool again with the exact same arguments."
                        }
                    )
            else:
                consecutive_failures[tool_key] = 0

        # A34: convergence notices go after ALL tool results for this assistant
        # block, never interleaved between sibling results (strict tool protocols
        # require the tool messages contiguous immediately after the assistant).
        messages.extend(pending_notices)

        if (final_answer is not None and not convergence_landing
                and not (cancel_token is not None and cancel_token.is_set())
                and _stands_alone(messages, final_answer)):
            # The web answer is the reply (ADR-0124): the call that would
            # follow only rewrites it. On hosted t06 (2026-10-21) that call took 16 s, re-read
            # 9.4k prompt tokens to condense a finished answer, and dropped its
            # citation markers. The tool message keeps a pointer, not a second
            # copy every later turn would pay for.
            answer = final_answer["content"]
            final_answer["content"] = "(shown to the user as this turn's reply, below)"
            messages.append({"role": "assistant", "content": answer})
            return _declare_links(answer) if _interactive else answer

        if convergence_landing:
            # The guard broke out mid-dispatch: siblings of the aborted call
            # may be unanswered, and the landing call below rejects orphaned
            # tool_calls blocks.
            repair_tool_call_history(messages)
            break

        if spent_prompt > TURN_PROMPT_BUDGET:
            logger.warning("Agent loop spent %d prompt tokens this turn; landing", spent_prompt)
            convergence_landing = True
            break

        notice = _budget_notice(iteration, max_iterations,
                                (TURN_PROMPT_BUDGET - spent_prompt) // max(prompt_tokens, 1))
        if notice is not None:
            messages.append(notice)
            _narr_mod.NARRATOR.turn(notice)

    if convergence_landing:
        logger.warning("Agent loop stopped by the convergence guard; landing")
    else:
        logger.warning("Agent loop hit max iterations (%d)", max_iterations)
    # One last turn with the tools removed, rather than discarding the whole
    # turn. Everything the model found or wrote is still in `messages`; asking
    # for it back costs one call and turns a completed write reported as
    # "maximum iterations reached" into an actual answer. Same discipline as
    # every other call this function makes: cancellation honoured, the
    # worker-slot cap held, the daemon-thread pattern kept (a bare in-thread
    # call here would trap the turn's LAST Ctrl+C in a C-level recv()), and
    # `_abandon` cleared so the TUI still streams this closing answer.
    if cancel_token is not None and cancel_token.is_set():
        logger.info("Agent loop cancelled before the final turn")
        return CANCELLED
    messages.append({"role": "system", "content": _FINAL_TURN_INSTRUCTION})
    _narr_mod.NARRATOR.turn(messages[-1])
    _emit(ThinkingStartEvent(iteration=iteration))
    try:
        slot = nullcontext() if _interactive else worker_slot()
        with slot:
            _abandon.clear()
            final_kwargs = dict(_llm_kwargs)
            final_kwargs["tools"] = None   # removed, not merely discouraged
            final_kwargs["cancel"] = _abandon
            final = _interruptible_llm(final_kwargs)
            thought = final.reasoning or ""
            final = _reask_without_reasoning(final, final_kwargs)
    except Exception as e:
        from silica.model.usage_limits import UsageLimited

        _narr_mod.NARRATOR.thought_close("", status="failed")  # as above: no open thought outlives its call
        if isinstance(e, UsageLimited):
            raise  # the student is owed the reset time, not "maximum iterations" (ADR-0102)
        logger.warning("Forced final turn failed (%s)", e)
        return MAX_ITERATIONS
    finally:
        _emit(ThinkingEndEvent(iteration=iteration))
    _narr_mod.NARRATOR.thought_close(thought or final.reasoning or "")
    text = (final.text or "").strip() or MAX_ITERATIONS
    text = _declare_links(text) if _interactive else text
    # Into the history and not narrated here, as the normal path does: the
    # caller's final beat records it after attribution, figures and the turn
    # notes (ADR-0094 point 8). Narrated here as a detached dict, it was missing
    # from /messages and left the turn notes no message to ride on.
    messages.append({"role": "assistant", "content": text})
    return text


def _without_stream(progress):
    """*progress* minus stream deltas: a web answer that will not be the reply
    keeps its rows and paints no text where the reply goes."""
    def forward(event) -> None:
        if not isinstance(event, LLMStreamEvent):
            progress(event)
    return forward


def _stands_alone(messages: list[dict], answer_msg: dict | None) -> bool:
    """Every other call of this turn was a vault search that found nothing.

    Then a `final` web answer leaves the chat model nothing to combine: a note it
    read, a write it made, anything a tool returned is the chat model's to weave
    in, and the turn goes on. The turn is everything after the last user message.
    """
    start = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=0)
    names = {tc.get("id"): (tc.get("function") or {}).get("name")
             for m in messages[start:] for tc in m.get("tool_calls") or ()}
    return all(found_nothing(names.get(m.get("tool_call_id")), m.get("content"))
               for m in messages[start:]
               if m.get("role") == "tool" and m is not answer_msg)


def _declare_links(answer: str) -> str:
    """Chat answers only: a worker's answer can be a note body."""
    try:
        from silica.kernel.recall.perception import declare_missing_links
        return declare_missing_links(answer)
    except Exception:  # the answer must reach the user whatever the resolver does
        return answer
