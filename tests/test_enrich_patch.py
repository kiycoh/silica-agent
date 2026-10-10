# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""An enrich lands as an appended section and leaves the note's own text alone.

The whole-body rewrite it replaced could return a shorter note that still
passed a link-and-length guard; an addition has nothing to lose, so the check
is the original body surviving byte for byte inside the result (S2 of the
2026-09-17 audit, 2026-09-19).
"""
from __future__ import annotations

import pytest

from silica.config import CONFIG
from silica.driver import DRIVER
from silica.kernel.workqueue import WorkItem

ORIGINAL = "---\ntitle: Target\n---\n\n# Target\n\nOld body with [[Link]] and the number 42.\n"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    (vault_dir / "Notes").mkdir(parents=True)
    monkeypatch.setattr("silica.config.CONFIG.vault_path", str(vault_dir))
    monkeypatch.setattr("silica.driver._driver", None)
    yield vault_dir
    monkeypatch.setattr("silica.driver._driver", None)


def test_enrich_appends_and_keeps_every_original_line(vault, monkeypatch):
    from silica.capabilities.enrich import run_enrich

    DRIVER.create("Notes/Target.md", ORIGINAL)
    monkeypatch.setattr("silica.capabilities.enrich._enrich_note",
                        lambda *a, **k: "## Proof sketch\n\nThe missing step, with 3.14.")

    res = run_enrich(WorkItem(kind="enrich", target_path="Notes/Target.md", context={}), CONFIG)

    assert res["status"] == "committed", res
    final = DRIVER.read_note("Notes/Target.md").content
    for line in ORIGINAL.strip().splitlines()[3:]:      # every body line survives verbatim
        assert line in final, line
    assert "The missing step, with 3.14." in final
    assert final.index("number 42") < final.index("missing step")   # appended, not inserted


def test_enrich_with_nothing_to_add_writes_nothing(vault, monkeypatch):
    from silica.capabilities.enrich import run_enrich

    DRIVER.create("Notes/Target.md", ORIGINAL)
    on_disk = DRIVER.read_note("Notes/Target.md").content   # create() stamps `type:`
    monkeypatch.setattr("silica.capabilities.enrich._enrich_note", lambda *a, **k: "")

    res = run_enrich(WorkItem(kind="enrich", target_path="Notes/Target.md", context={}), CONFIG)

    assert res["status"] == "no_change"
    assert DRIVER.read_note("Notes/Target.md").content == on_disk


def test_enrich_lands_beside_a_violation_the_note_already_had(vault, monkeypatch):
    """The addition cannot repair what the note already carried (an unknown
    callout type here) and the gate must not hold it hostage to that: only
    what the patch introduces rolls it back. Review finding, 2026-09-19."""
    from silica.capabilities.enrich import run_enrich

    DRIVER.create("Notes/Target.md", ORIGINAL + "\n> [!definizione]\n> An existing definition.\n")
    monkeypatch.setattr("silica.capabilities.enrich._enrich_note",
                        lambda *a, **k: "## Example\n\nA newly supplied example.")

    res = run_enrich(WorkItem(kind="enrich", target_path="Notes/Target.md", context={}), CONFIG)

    assert res["status"] == "committed", res
    final = DRIVER.read_note("Notes/Target.md").content
    assert "[!definizione]" in final
    assert "A newly supplied example." in final
