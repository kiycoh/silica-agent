# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Rank-graduated depth for perceive() (OpenViking-informed, default OFF).

The window sweep validated 3x1000 per note and killed UNIFORM narrowing; the
recall-rank probe killed cutting k (the 9-15 tail carries gold). The untested
cell between those verdicts is rank-graduated depth: the head keeps the
validated windows, the tail keeps its slot but is served as an extractive L0
abstract. Off (deep_ranks=None) the path is byte-identical — the lever ships
default-off until the LME gate rules on it.

The same degrade doubles as cross-turn dedup: a path in `served_before` is
served at L0 whatever its rank, because the reader already has its body.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from silica.kernel.recall.perception import l0_excerpt, perceive


def _bind(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import silica.driver
    import silica.kernel.recall.cooccurrence as cooc_mod
    import silica.kernel.recall.embed as embed_mod
    from silica.config import CONFIG

    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(CONFIG, "vault_path", str(vault))
    monkeypatch.setattr(CONFIG, "memory_vault", str(vault))
    monkeypatch.setattr(CONFIG, "backend", "fs")
    monkeypatch.setattr(silica.driver, "_driver", None)
    embed_mod.clear()
    cooc_mod.clear()


def _write(rel: str, body: str) -> None:
    from silica.driver import DRIVER

    DRIVER.create(rel, f'---\ndate: "2026-01-01"\n---\n\n{body}\n')


BODY = "# Alpha\n\nintro paragraph about the topic\n\n## Beta\n\n" \
       + ("yoga filler sentence " * 200)


# ---------------------------------------------------------------- l0_excerpt

def test_l0_is_heading_tree_plus_first_paragraph():
    out = l0_excerpt(BODY)
    assert "# Alpha" in out
    assert "## Beta" in out
    assert "intro paragraph about the topic" in out
    assert "yoga filler" not in out


def test_l0_respects_the_cap():
    out = l0_excerpt("word " * 500, cap_chars=120)
    assert len(out) <= 120


def test_l0_empty_body_is_empty():
    assert l0_excerpt("") == ""
    assert l0_excerpt("   \n  ") == ""


# ------------------------------------------------------------- perceive tiers

def _blocks(tmp_path, monkeypatch, **kw):
    _bind(tmp_path / "v", monkeypatch)
    for i in range(4):
        _write(f"n{i}.md", BODY.replace("Alpha", f"Alpha{i}"))
    return perceive("yoga", now="2026-08-17", with_facts=False,
                    use_embedder=False, paths=["n0", "n1", "n2", "n3"], **kw)


def test_off_by_default_is_byte_identical(tmp_path, monkeypatch):
    base = _blocks(tmp_path, monkeypatch)
    same = perceive("yoga", now="2026-08-17", with_facts=False,
                    use_embedder=False, paths=["n0", "n1", "n2", "n3"],
                    deep_ranks=None, served_before=None)
    assert same.render() == base.render()
    assert all(not b.abstract for b in base.blocks)


def test_tail_beyond_deep_ranks_serves_l0(tmp_path, monkeypatch):
    p = _blocks(tmp_path, monkeypatch, deep_ranks=2)
    deep, tail = p.blocks[:2], p.blocks[2:]
    assert all(not b.abstract for b in deep)
    assert all(b.abstract for b in tail)
    for b in tail:
        assert b.excerpt == l0_excerpt(b.body)
        assert "yoga filler" not in b.excerpt  # windows replaced by abstract
    # the head keeps the validated query-densest windows
    assert "yoga filler" in deep[0].excerpt


def test_render_marks_abstract_blocks(tmp_path, monkeypatch):
    p = _blocks(tmp_path, monkeypatch, deep_ranks=1)
    out = p.render()
    assert "| abstract]" in out
    # rank 1 header carries no abstract marker
    head = out.split("\n")[0]
    assert "abstract" not in head


def test_served_paths_degrade_even_at_rank_one(tmp_path, monkeypatch):
    p = _blocks(tmp_path, monkeypatch, served_before={"n0"})
    assert p.blocks[0].abstract
    assert p.blocks[0].excerpt == l0_excerpt(p.blocks[0].body)
    assert not p.blocks[1].abstract


# --------------------------------------------------------------- tool wiring

def test_recall_tool_off_by_default_and_tracks_nothing(tmp_path, monkeypatch):
    from silica.tools import graph as g

    _bind(tmp_path / "v", monkeypatch)
    _write("solo.md", BODY)
    from silica.tools.graph import silica_cooccurrence_refresh
    silica_cooccurrence_refresh(force=True)
    g.reset_recall_served()
    out = g.silica_recall("yoga")
    assert "context" in out
    assert g._SERVED == set()


def test_recall_tool_degrades_repeat_serves_and_resets(tmp_path, monkeypatch):
    from silica.config import CONFIG
    from silica.tools import graph as g

    _bind(tmp_path / "v", monkeypatch)
    for i in range(3):
        _write(f"n{i}.md", BODY.replace("Alpha", f"Alpha{i}"))
    from silica.tools.graph import silica_cooccurrence_refresh
    silica_cooccurrence_refresh(force=True)
    monkeypatch.setattr(CONFIG, "recall_deep_ranks", 2, raising=False)
    g.reset_recall_served()

    first = g.silica_recall("yoga")
    assert g._SERVED  # full-tier serves are now cooling
    second = g.silica_recall("yoga")
    # every note served whole last turn arrives as an abstract this turn
    assert len(second["context"]) < len(first["context"])
    g.reset_recall_served()
    assert g._SERVED == set()
