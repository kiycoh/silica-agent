# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""`silica_learn_items` (learn-suite D19): the REPL's door into the engine.
Picked items out with cue, hints and the span's location, never the key."""
from __future__ import annotations

import json
import re
import time

import pytest

import silica.tools.atomic  # noqa: F401 — @tool registration is import-driven
from silica.tools import TOOLS
from tests.test_learn_items import LEAF, NOTE

DAY = 86400.0


@pytest.fixture
def vault(tmp_vault, tmp_path, monkeypatch):
    import silica.kernel.report.quiz as quiz

    monkeypatch.setattr(quiz, "log_path", lambda: tmp_path / "quiz.jsonl")
    old = time.time() - 400 * DAY
    p = tmp_vault.note("ML/Margine geometrico.md", NOTE.replace("AI: true\n", "date: 2025-01-01\n"))
    tmp_vault.note("sources/Lezione 11.md", LEAF)
    tmp_vault.note("ML/Iperpiano.md", "---\ndate: 2025-01-01\n---\n# Iperpiano\n\nUn piano.\n")
    import os
    for f in (p,):
        os.utime(f, (old, old))
    return tmp_vault


def test_learn_items_returns_cues_without_the_answer_key(vault, monkeypatch):
    from silica.kernel.learn import items

    monkeypatch.setattr(items, "_llm", lambda *a, **k: ("", "stop"))
    out = TOOLS["silica_learn_items"].fn(n=4, target="")
    assert out["items"], out
    payload = json.dumps(out, ensure_ascii=False)
    assert "answer" not in payload and "claim" not in payload and "criteria" not in payload
    assert not re.search(r"limita l'errore atteso|distanza dal piano", payload)  # G5
    it = out["items"][0]
    assert it["path"] == "ML/Margine geometrico.md" and it["kind"] in ("cloze", "structure")
    assert it["source"]["leaf"] == "sources/Lezione 11.md"
    assert "hints" in it and it["i"] == 1
    assert out["session"] and out["due"] >= 1


def test_learn_items_scopes_to_a_folder_target(vault, monkeypatch):
    from silica.kernel.learn import items

    monkeypatch.setattr(items, "_llm", lambda *a, **k: ("", "stop"))
    out = TOOLS["silica_learn_items"].fn(n=4, target="ML/")
    assert all(it["path"].startswith("ML/") for it in out["items"])
    none = TOOLS["silica_learn_items"].fn(n=4, target="Nowhere/")
    assert none["items"] == [] and none["short"]
