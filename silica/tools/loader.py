# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""silica_load_tools: the chat loop's way to reach a tool it is not offered yet.

A chat turn is offered the core (silica.agent.constraints.CHAT_CORE), not all
52 chat tools: each schema bills on every iteration, and 52 of them were ~11k
tokens of a ~13k floor while GUI and TUI sessions used 22 (ADR-0050). The rest
stay one call away. This tool's description is the catalog, built when the
schema is rendered, so a tool registered later is listed without a second
list to keep in sync.
"""
from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import Field

from silica.tools import TOOLS, Tool, _model_from_signature

# The catalog line is the first sentence of the tool's own description, cut
# here: 33 deferred tools at 90 chars is ~750 tokens, and the gist is enough to
# pick a tool whose full schema the load then delivers.
_GIST_CHARS = 90

_DESCRIPTION = (
    "Load tools that are not offered yet, by exact name; they are callable "
    "from your next step and stay loaded for the rest of the conversation. "
    "Load only what the request needs. Loadable:"
)


def _gist(description: str) -> str:
    first = (description or "").strip().split("\n\n", 1)[0].replace("\n", " ")
    # A sentence ends at a period before a capital, not at "e.g. `x`".
    cut = re.split(r"\.\s+(?=[A-Z])", first, maxsplit=1)[0].rstrip(".")
    return cut if len(cut) <= _GIST_CHARS else cut[: _GIST_CHARS - 1].rstrip() + "…"


def silica_load_tools(
    names: Annotated[list[str], Field(description="Exact tool names from the list above")],
) -> dict[str, Any]:
    from silica.agent.constraints import loadable

    pool = set(loadable())
    loaded = [n for n in names if n in pool]
    out: dict[str, Any] = {"loaded": loaded}
    unknown = [n for n in names if n not in pool]
    if unknown:
        out["unknown"] = unknown
    return out


class _LoaderTool(Tool):
    __slots__ = ()

    def json_schema(self) -> dict:
        from silica.agent.constraints import loadable

        schema = super().json_schema()
        lines = [f"- {n}: {_gist(TOOLS[n].description)}" for n in loadable() if n in TOOLS]
        schema["function"]["description"] = "\n".join([self.description, *lines])
        return schema


TOOLS["silica_load_tools"] = _LoaderTool(
    silica_load_tools, "silica_load_tools", _DESCRIPTION,
    _model_from_signature(silica_load_tools), "atomic", collapse="never",
)
