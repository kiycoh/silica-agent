# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Invariants the whole tool registry has to hold, not one member of it.

`cls` and `collapse` are free `str` on `Tool`, so `cls="atmoic"` registers
silently, and a missing docstring ships an empty description to the model on
every request. Zero violations across the registry today: this is a regression
guard, not a bug hunt.
"""
from __future__ import annotations


def _registry() -> dict:
    # Registration is an import side effect — same module set cli.py loads.
    import silica.tools.atomic  # noqa: F401
    import silica.tools.composed  # noqa: F401
    import silica.tools.wrapped  # noqa: F401
    import silica.tools.codedocs_tool  # noqa: F401
    import silica.tools.delegate_tool  # noqa: F401
    import silica.sources.web_research  # noqa: F401
    from silica.tools import TOOLS

    return TOOLS


def test_tools_registry_contract():
    tools = _registry()
    assert tools, "registry must not be empty"
    for name, t in tools.items():
        assert name == t.name, f"key {name!r} drifted from Tool.name {t.name!r}"
        assert t.description.strip(), f"{name} has no description to send the model"
        assert t.cls in {"atomic", "composed", "wrapped"}, f"{name}: cls={t.cls!r}"
        assert t.collapse in {"lazy", "eager", "never"}, f"{name}: collapse={t.collapse!r}"
        t.json_schema()  # params_model must serialize


def test_no_test_defined_tool_survives_in_the_registry():
    """The conftest fixture drops the fakes a test registers into TOOLS. A
    survivor here means it stopped doing that, and the next exact-set assertion
    is order-dependent again."""
    stray = {n for n, t in _registry().items()
             if not getattr(t.fn, "__module__", "").startswith("silica")}
    assert not stray, f"test-defined tools left in the registry: {stray}"


# Duplicate names cannot be asserted from the dict: by the time it is readable
# the collision already overwrote one of the two. That check belongs in the
# decorator (`assert tool_name not in TOOLS`) and is deferred, no instance yet.
