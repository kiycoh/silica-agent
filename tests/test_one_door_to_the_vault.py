# SPDX-License-Identifier: AGPL-3.0-or-later
"""`adopt_vault` is the only way to change the vault. A bare `CONFIG.vault_path = x`
leaves the driver, the overlay, the manifest and the index caches answering for the old
folder, and was safe only by when it ran."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from silica.config import CONFIG
from silica.onboarding.adopt import adopt_vault

ROOT = Path(__file__).resolve().parents[1]
# Not this process's vault: the engine's own CONFIG, mirrored on every call (ADR-0043).
MIRROR = "silica/kernel/recall/engine.py"


def _assigns_the_vault(tree: ast.AST) -> list[int]:
    return [n.lineno for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign))
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
            if isinstance(t, ast.Attribute) and t.attr == "vault_path"
            and isinstance(t.value, ast.Name) and t.value.id == "CONFIG"]


@pytest.mark.parametrize("folder", ["silica", "evals", "scripts"])
def test_nothing_assigns_the_vault_bare(folder):
    bare = {}
    for p in (ROOT / folder).rglob("*.py"):
        rel = str(p.relative_to(ROOT))
        if rel not in ("silica/onboarding/adopt.py", MIRROR):
            if lines := _assigns_the_vault(ast.parse(p.read_text(encoding="utf-8"))):
                bare[rel] = lines
    assert not bare


def test_a_look_writes_nothing_and_leaves_a_missing_folder_unadopted(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG, "vault_path", "/before")
    code = tmp_path / "repo"
    (code / "pkg").mkdir(parents=True)
    for i in range(6):
        (code / "pkg" / f"m{i}.py").write_text("x = 1\n", encoding="utf-8")
    assert adopt_vault(code, writes=False).vault == str(code)
    assert sorted(p.name for p in code.iterdir()) == ["pkg"]
    monkeypatch.setattr(CONFIG, "vault_path", "/before")
    missing = adopt_vault(tmp_path / "no", writes=False)
    assert missing.created and not (tmp_path / "no").exists() and CONFIG.vault_path == "/before"


def test_an_adoption_resets_what_answers_for_the_old_folder(tmp_path, monkeypatch):
    from silica.kernel.base import manifest

    calls = []
    monkeypatch.setattr("silica.driver.reset_driver", lambda: calls.append("driver"))
    monkeypatch.setattr("silica.kernel.text.overlay.reset_overlay_cache", lambda: calls.append("overlay"))
    monkeypatch.setattr(manifest, "reset_manifest_cache", lambda: calls.append("manifest"))
    monkeypatch.setattr("silica.kernel.recall.relatedness.reset_vault_caches", lambda: calls.append("caches"))
    monkeypatch.setattr(CONFIG, "vault_path", "/before")
    adopt_vault(tmp_path)
    assert calls == ["driver", "overlay", "manifest", "caches"]
