# tests/test_notebook_adapter.py
"""NotebookAdapter — .ipynb first-class citizen (spec-code-lane §3)."""
import json
import subprocess
from pathlib import Path

import pytest

from silica.config import CONFIG
from silica.sources.notebook import NOTEBOOK
from silica.sources.registry import ALL_ADAPTERS, adapter_for


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "seed").write_text("s", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=path, check=True)


NB = json.dumps({
    "nbformat": 4,
    "metadata": {"kernelspec": {"language": "python"}},
    "cells": [
        {"cell_type": "markdown", "source": "# Analysis\nWhat this notebook shows."},
        {"cell_type": "code", "source": "%matplotlib inline\nimport numpy\n\ndef load(path: str) -> list:\n    return []\n",
         "outputs": [{"data": {"image/png": "QUFBQQ=="}}]},
    ],
})


def test_matches_and_dispatch():
    assert NOTEBOOK.matches("nb/analysis.ipynb")
    assert not NOTEBOOK.matches("m.py") and not NOTEBOOK.matches("a.md")
    assert adapter_for("x.ipynb", enabled=["prose", "code", "notebook"]) is NOTEBOOK
    assert NOTEBOOK in ALL_ADAPTERS


def test_stub_has_narrative_and_skeleton_no_outputs(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    (tmp_path / "analysis.ipynb").write_text(NB, encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    item = NOTEBOOK.read("analysis.ipynb")
    stub = NOTEBOOK.to_stub(item)
    assert stub.lane == "terminal"
    assert stub.note_path.endswith("analysis.md")
    assert "documents:" in stub.body and "analysis.ipynb" in stub.body
    assert "code_ref:" in stub.body
    assert "## Narrative" in stub.body and "What this notebook shows." in stub.body
    assert "def load(path: str) -> list" in stub.body   # skeleton signature
    assert "numpy" in stub.body                          # external import listed
    assert "QUFBQQ" not in stub.body                     # outputs ignored
    assert "%matplotlib" not in stub.body                # magics stripped


def test_malformed_notebook_read_raises(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    (tmp_path / "bad.ipynb").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path / "vault"))
    (tmp_path / "vault").mkdir()
    with pytest.raises(ValueError):
        NOTEBOOK.read("bad.ipynb")


def test_default_sources_includes_notebook_in_git_repo(tmp_path):
    from silica.kernel.vault_manifest import default_sources
    _init_repo(tmp_path)
    assert set(default_sources(tmp_path)) == {"prose", "code", "notebook"}
