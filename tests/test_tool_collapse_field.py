from __future__ import annotations

from pydantic import BaseModel

from silica.tools import tool, TOOLS


def test_tool_defaults_to_lazy_collapse():
    class _A(BaseModel):
        x: int = 0

    try:
        @tool(_A, cls="atomic")
        def silica_dummy_lazy(x: int = 0):
            "doc"
            return {"x": x}

        t = TOOLS["silica_dummy_lazy"]
        assert t.collapse == "lazy"
    finally:
        TOOLS.pop("silica_dummy_lazy", None)


def test_tool_accepts_eager_collapse():
    class _B(BaseModel):
        x: int = 0

    try:
        @tool(_B, cls="composed", collapse="eager")
        def silica_dummy_eager(x: int = 0):
            "doc"
            return {"x": x}

        t = TOOLS["silica_dummy_eager"]
        assert t.collapse == "eager"
    finally:
        TOOLS.pop("silica_dummy_eager", None)
