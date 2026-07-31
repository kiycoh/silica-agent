# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Shared fixtures for the benchmark tests moved out of tests/eval/.

These files live outside the `tests/` tree (they are slow, not in the default
`testpaths`), so pytest's upward conftest discovery no longer reaches
tests/conftest.py — re-export the fixtures they still use.
"""
import pytest

from tests.conftest import tmp_vault  # noqa: F401


@pytest.fixture(autouse=True)
def _isolated_oracle_cache(monkeypatch, tmp_path):
    """Every eval test gets its own oracle cache dir. Without this, a stub
    reply frozen by one test is served to the next identical request, and a
    call_llm monkeypatch silently never fires."""
    import evals.oracle as oracle

    monkeypatch.setattr(oracle, "_CACHE_DIR", tmp_path / "oracle")
