# SPDX-License-Identifier: AGPL-3.0-or-later

"""Doctor names the memory lane when it points somewhere else (ADR-0019).

With SILICA_MEMORY_VAULT set to a different tree, `silica_recall` fuses that
vault's notes while `silica_search`/`silica_exists`/`silica_read_note` resolve
only inside the active vault. An agent then reads a note in a recall context,
asks whether it exists, is told `false`, and reports it missing. Doctor listed
one vault and never mentioned the second, so the split had no surface.
"""
from __future__ import annotations

import pytest

from silica.onboarding.checks import check_memory_lane


class _Cfg:
    def __init__(self, vault: str, memory: str) -> None:
        self.vault_path = vault
        self.memory_vault = memory


@pytest.fixture
def two_vaults(tmp_path):
    active = tmp_path / "active"
    other = tmp_path / "other"
    for v in (active, other):
        (v / "sub").mkdir(parents=True)
        (v / "sub" / "Nota.md").write_text("# nota", encoding="utf-8")
    return active, other


def test_a_divergent_memory_vault_is_named(two_vaults, monkeypatch):
    active, other = two_vaults
    from silica.config import CONFIG

    monkeypatch.setattr(CONFIG, "vault_path", str(active))
    monkeypatch.setattr(CONFIG, "memory_vault", str(other))

    r = check_memory_lane(_Cfg(str(active), str(other)))

    assert r.status == "warn"
    assert str(other) in r.detail
    assert r.hint


def test_the_lane_abstains_when_it_is_the_active_vault(two_vaults, monkeypatch):
    active, _ = two_vaults
    from silica.config import CONFIG

    monkeypatch.setattr(CONFIG, "vault_path", str(active))
    monkeypatch.setattr(CONFIG, "memory_vault", str(active))

    r = check_memory_lane(_Cfg(str(active), str(active)))

    assert r.status == "ok"


def test_a_memory_vault_that_does_not_exist_reads_as_off(tmp_path, monkeypatch):
    active = tmp_path / "active"
    active.mkdir()
    from silica.config import CONFIG

    monkeypatch.setattr(CONFIG, "vault_path", str(active))
    monkeypatch.setattr(CONFIG, "memory_vault", str(tmp_path / "nope"))

    r = check_memory_lane(_Cfg(str(active), str(tmp_path / "nope")))

    assert r.status == "ok"


def test_the_check_runs_in_the_doctor_report(tmp_path, monkeypatch):
    from silica.config import CONFIG
    from silica.onboarding.checks import run_checks

    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    names = [r.name for r in run_checks(CONFIG)]

    assert "memory lane" in names
