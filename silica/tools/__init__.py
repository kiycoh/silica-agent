# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Tool registry — the @tool decorator, TOOLS dict, and JSON-schema generation.

This is the contract layer between the LLM and Silica's toolset.
Every tool is a function decorated with @tool(ParamsModel, cls="atomic|composed|wrapped").
The decorator auto-registers the tool in the global TOOLS dict, and silica.tools.catalog
is the one list of the modules that hold them: `catalog.load()` is what fills it.
The LLM receives the JSON-schema of each tool's ParamsModel as its function definition.

What a tool is, past its schema, is declared where the tool is (Tool.__init__): the
agent loop, the compaction, the taint gate and the surfaces read these facts and hold
no list of names of their own.

Design (from SILICA.md §8.4):
  - Pydantic BaseModel for params → validates input AND generates JSON-schema
  - Three tool classes: atomic (1:1 CLI), composed (promoted scripts), wrapped (Golden Rule enforced)
  - TOOLS dict is the single source of truth for tool dispatch
"""
from __future__ import annotations

import inspect
import json
import logging
import re
from collections.abc import Container, Iterable
from contextvars import ContextVar
from typing import Any, Callable, NamedTuple, get_type_hints

from pydantic import BaseModel, create_model

logger = logging.getLogger(__name__)

# The tool the agent loop is running for a model, set around that one call. A
# tool reads it to tell the model's own call, whose error the model reads and
# can act on, from the same function run by code or inside another tool, where
# an error only loses the work (silica_write_note's mermaid check).
MODEL_CALL: ContextVar[str | None] = ContextVar("silica_model_call", default=None)


# Schema keys whose VALUE is a name -> subschema map, not a schema node. Their
# keys are field names the model has to see; everything else is an annotation.
_SCHEMA_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions"})


def _strip_titles(node: Any, *, in_map: bool = False) -> Any:
    """Compact a JSON Schema for the wire: the whole toolset is re-sent on
    every iteration of the agent loop, so annotation-only keys are paid per
    request. Three lossless passes, applied at any depth:

    - drop every annotation `title` (restates the field name in Title Case,
      ~600 tokens on the chat toolset). A `title` sitting inside `properties`
      is a real PARAMETER, not an annotation, and survives: dropping it left
      `required: ["title", ...]` naming a field the model could not see, so a
      strict validator rejected the call and a lenient one omitted the value
      (silica_event_create, silica_write_note, silica_graph_export);
    - collapse the Optional pattern `anyOf: [X, {"type": "null"}]` to X —
      omitting an optional field already means null, so the null branch says
      nothing. A real union (two live branches) is left alone;
    - drop `"default": null` for the same reason; informative defaults
      (5, "", false) survive.
    """
    if isinstance(node, dict):
        if in_map:
            return {k: _strip_titles(v) for k, v in node.items()}
        out = {}
        for k, v in node.items():
            if k == "title" or (k == "default" and v is None):
                continue
            out[k] = _strip_titles(v, in_map=k in _SCHEMA_MAPS)
        any_of = out.get("anyOf")
        if isinstance(any_of, list) and len(any_of) == 2:
            live = [m for m in any_of if m != {"type": "null"}]
            if len(live) == 1 and isinstance(live[0], dict):
                del out["anyOf"]
                # Parent keys (description, default) win over the branch's.
                out = {**live[0], **out}
        return out
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


# A clause that sends the model to another tool is marked <<like this>> where
# it is written, in a docstring or a parameter's description. A turn that can
# reach every tool the clause names reads it without the marks, the text it
# always read; a turn that cannot reads the description without the clause. A
# model calls what a description names, and a call to a tool the turn lacks
# spends one of its steps on an error: a demo question has 8 (ADR-0139).
_POINTER = re.compile(r"<<(.*?)>>", re.S)


def pointed(text: str, reach: Container[str] | None = None) -> str:
    """`text` as a turn that can reach the tools in `reach` reads it; None
    reaches every tool. A clause naming two tools goes when either is out."""

    def clause(m: re.Match[str]) -> str:
        # Namespaced names only: plan and remember are tools and English words.
        named = [w for w in re.findall(r"\w+", m[1]) if "_" in w and w in TOOLS]
        return m[1] if reach is None or all(n in reach for n in named) else ""

    return _POINTER.sub(clause, text)


def _pointed(node: Any, reach: Container[str] | None) -> Any:
    if isinstance(node, str):
        return pointed(node, reach)
    if isinstance(node, dict):
        return {k: _pointed(v, reach) for k, v in node.items()}
    if isinstance(node, list):
        return [_pointed(v, reach) for v in node]
    return node


class Reads(NamedTuple):
    """Where a call that hands the model a note's own words names that note:
    the arguments that hold it, and the result key that does."""

    args: tuple[str, ...] = ()
    result: str = ""


class Tool:
    """Metadata and executor for a single registered tool."""

    __slots__ = ("fn", "name", "description", "params_model", "cls", "collapse", "sensitive",
                 "ingress", "mutates", "reads", "parallel_safe", "once_per_block", "note_args", "label",
                 "found_nothing", "found", "loads", "history_view", "reply_flag", "shrink")

    def __init__(
        self,
        fn: Callable,
        name: str,
        description: str,
        params_model: type[BaseModel],
        cls: str,
        collapse: str = "lazy",
        sensitive: bool = False,
        *,
        ingress: bool | None = None,
        mutates: str = "",
        reads: Reads | None = None,
        parallel_safe: bool = False,
        once_per_block: bool = False,
        note_args: tuple[str, ...] = (),
        label: tuple[str, str | None] | None = None,
        found_nothing: str = "",
        found: str = "",
        loads: Callable[[dict, Any], Iterable[Any]] | None = None,
        history_view: Callable[[str, set[str]], str] | None = None,
        reply_flag: str = "",
        shrink: Callable[[dict, str], str | None] | None = None,
    ):
        self.fn = fn
        self.name = name
        self.description = description
        self.params_model = params_model
        self.cls = cls  # "atomic" | "composed" | "wrapped"
        self.collapse = collapse  # "lazy" | "eager" | "never"
        # classified once at definition; the default toolset filters
        # on this so a sensitive tool can never leak into the main agent by
        # mere registration. New sensitive tools are covered automatically.
        self.sensitive = sensitive
        # Does a result carry text from outside the user's own words and notes?
        # The taint gate (ADR-0090) reads it. None is "never said", which a
        # test refuses for a registered tool: a tool that brought a page in and
        # defaulted to False would pass the gate without a word.
        self.ingress = ingress
        # What the call does to the note its arguments name: "written",
        # "deleted", "moved", "" for none. A batch tool that names an ops file,
        # or writes notes it does not name, says "": it voids no read of a note
        # and gets no chip.
        self.mutates = mutates
        # Set on the tools that hand the model a note's own words. A title
        # search lists candidates and reads none.
        self.reads = reads
        # A read one tool block may run beside others (ADR-0150): it takes its
        # own locks and no progress callback. False, the default, runs serially.
        self.parallel_safe = parallel_safe
        # Two of it in one block lose a write of a shared file: the block runs
        # serially instead.
        self.once_per_block = once_per_block
        # The arguments that name a note, in the order to try them.
        self.note_args = note_args
        # (verb, the argument shown as its target): `verb "target"` on screen.
        self.label = label
        # The result key that is empty when a vault search matched nothing.
        self.found_nothing = found_nothing
        # The result key listing the notes a vault search found, best first:
        # paths or `{path}` items. Not found_nothing's key, which for a body
        # search is a count.
        self.found = found
        # (args, result) -> tool names this call brings in: the loop offers each
        # one it may. Only ever a successful call's.
        self.loads = loads
        # (result, the turn's memo) -> what the history keeps of a successful
        # result. The loop empties the memo when compaction collapses one.
        self.history_view = history_view
        # The argument that asks for this result to stand as the turn's reply.
        self.reply_flag = reply_flag
        # (args, the request) -> the part of this read to serve when the whole
        # does not fit the window, None for the stub (loop.FIT_NARROWS).
        self.shrink = shrink

    def json_schema(self, reach: Container[str] | None = None) -> dict:
        """Return the OpenAI-compatible function schema for this tool, as a
        turn that can reach the tools in `reach` reads it (pointed())."""
        return _pointed({
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _strip_titles(self.params_model.model_json_schema()),
            },
        }, reach)

    def run(self, _cancel_token: Any = None, _progress: Any = None, **kwargs: Any) -> str:
        """Validate args via pydantic, then execute the tool function.

        `_cancel_token` and `_progress` are injected by the agent loop and
        forwarded to the underlying function only when it declares the matching
        parameter (`cancel_token` / `progress`). Neither is part of the params
        model / JSON schema. `progress` is what lets a tool running a loop of its
        own — silica_web_answer — draw its steps into the UI the outer loop is
        already streaming to, instead of showing one opaque spinning row.
        Always returns a JSON string — either the result or an error.
        """
        try:
            validated = self.params_model(**kwargs)
            call_kwargs = validated.model_dump()
            injected = {"cancel_token": _cancel_token, "progress": _progress}
            if any(v is not None for v in injected.values()):
                sig = inspect.signature(self.fn)
                for param, value in injected.items():
                    if value is not None and param in sig.parameters:
                        call_kwargs[param] = value
            result = self.fn(**call_kwargs)
            if isinstance(result, str):
                return result
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            logger.exception("Tool %s execution error: %s", self.name, e)
            return json.dumps(
                {"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False
            )


# Global tool registry — the single source of truth
TOOLS: dict[str, Tool] = {}

# Run before the chat's tool set is read, for what joins TOOLS from above this
# layer (catalog.load adds the connectors): the reader, agent/constraints.py,
# sits below them and may not import them.
REGISTRARS: list[Callable[[], None]] = []


def note_keys(tools: dict[str, Any] | None = None) -> tuple[str, ...]:
    """Every argument any tool declares as naming a note. Read off the arguments
    and not off one tool's own: a reader also meets calls to a tool no longer
    registered, in a stored chat."""
    held = TOOLS if tools is None else tools
    return tuple(dict.fromkeys(k for t in held.values() for k in getattr(t, "note_args", ())))


def tool_failed(result: Any) -> bool:
    """Did this tool result report a failure? A result is one only by an `error`
    key: a plain string is output, and substring-sniffing it for "error" or
    "failed" misclassifies grep hits and "0 errors" reports."""
    if not result:
        return False
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (ValueError, RecursionError):
            return False  # not JSON, or nested past the parser: plain-text output
        return isinstance(parsed, dict) and "error" in parsed
    return isinstance(result, dict) and "error" in result


def found_nothing(name: str | None, result: object) -> bool:
    """A vault search that matched nothing: the only call a `final` web answer
    may stand beside and still end the turn (loop.py). An error is not nothing:
    the chat model may owe the user a word about it."""
    key = getattr(TOOLS.get(name or ""), "found_nothing", "")
    if not key:
        return False
    try:
        data = json.loads(result)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and "error" not in data and not data.get(key)


def model_from_signature(fn: Callable) -> type[BaseModel]:
    """Build the tool input model from its annotated public parameters."""
    hints = get_type_hints(fn, include_extras=True)
    # Any, not tuple[Any, Any]: a typed splat would also bind create_model's
    # own keyword parameters (__config__, __base__) and match no overload.
    fields: dict[str, Any] = {}
    for name, param in inspect.signature(fn).parameters.items():
        if name in {"cancel_token", "progress"}:
            continue
        if param.kind in {param.VAR_POSITIONAL, param.VAR_KEYWORD}:
            raise TypeError(f"Tool {fn.__name__} cannot expose variadic parameter {name!r}")
        annotation = hints.get(name, param.annotation)
        if annotation is inspect.Parameter.empty:
            raise TypeError(f"Tool {fn.__name__} parameter {name!r} needs a type annotation")
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (annotation, default)
    return create_model(f"{fn.__name__}Args", **fields)


def make_tool(
    fn: Callable,
    params_model: type[BaseModel] | None = None,
    cls: str = "atomic",
    collapse: str = "lazy",
    sensitive: bool = False,
    *,
    ingress: bool,
    **facts: Any,
) -> Tool:
    """`fn` as a Tool, named after it and described by its docstring, without
    registering it: what @tool registers, and what a loop that holds its own
    tools hands run_agent as AgentConstraints.registry."""
    # cleandoc, not strip: a docstring's continuation lines carry the source
    # indentation, and every "\n    " is its own token in a description sent
    # on every request.
    return Tool(fn, fn.__name__, inspect.cleandoc(fn.__doc__ or ""), params_model or model_from_signature(fn), cls,
                collapse=collapse, sensitive=sensitive, ingress=ingress, **facts)


def tool(
    params_model: type[BaseModel] | None = None,
    cls: str = "atomic",
    collapse: str = "lazy",
    sensitive: bool = False,
    *,
    ingress: bool,
    **facts: Any,
):
    """Decorator that registers a function as a Silica tool. `facts` are Tool's
    other keyword-only arguments: what the tool is, said where it is.

    `ingress` has no default so that a tool bringing outside text in cannot be
    registered by saying nothing: the taint gate reads it (ADR-0090), and a
    forgotten one is a TypeError at import and not a page past the gate.

    Usage:
        @tool(cls="atomic", ingress=False, note_args=("name",))
        def silica_read_note(name: str):
            '''Read a vault note by name (wikilink-style resolution).'''
            return DRIVER.read_note(name)
    """

    def decorator(fn: Callable) -> Callable:
        TOOLS[fn.__name__] = make_tool(fn, params_model, cls, collapse, sensitive, ingress=ingress, **facts)
        logger.debug("Registered tool: %s (class=%s, collapse=%s)", fn.__name__, cls, collapse)
        return fn

    return decorator
