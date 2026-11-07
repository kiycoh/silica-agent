# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A bench run never goes on in the vault that happened to be active."""
from __future__ import annotations

import pytest


def test_a_bench_vault_that_is_not_there_stops_the_run(tmp_vault, tmp_path):
    from evals import _shared
    from silica.config import CONFIG

    before = CONFIG.vault_path
    with pytest.raises(RuntimeError, match="no vault at"):
        _shared.adopt_bench_vault(tmp_path / "typo")
    assert CONFIG.vault_path == before


@pytest.mark.parametrize("runner", ["evals.longmemeval.runner", "evals.musique.runner"])
def test_binding_a_new_bench_vault_moves_the_process_there(tmp_vault, tmp_path, runner):
    import importlib

    from silica.config import CONFIG

    fresh = tmp_path / "fresh" / "vault"
    importlib.import_module(runner).bind_vault(fresh)
    assert CONFIG.vault_path == str(fresh.resolve())
