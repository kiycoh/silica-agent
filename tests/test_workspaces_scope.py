# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A workspace's turn lists and ranks its members only (ADR-0112 point 4),
through every tool that enumerates notes: the 2026-10-15 review found six
that ignored the scope, and a live turn reads what a listing shows it."""
from __future__ import annotations

import pytest

# the write gate registers on import
import silica.tools.notes  # noqa: F401
from silica.kernel import workspaces as wsk

WS = """---
type: workspace
notes: []
folders:
- Inside
where: []
---
# Focus
"""

BODY = "Kalman filter covariance prediction update gain innovation residual smoothing.\n" * 3


@pytest.fixture
def vault(tmp_vault):
    tmp_vault.note("Inside/Member.md", "---\ndate: 2026-01-02\n---\n# Member\n\n" + BODY)
    tmp_vault.note("Outside/Secret.md", "---\ndate: 2026-01-03\n---\n# Secret\n\n" + BODY + "[[Member]]\n")
    tmp_vault.note("Outside/Lonely orphan.md", "# Lonely\n\nKalman orphan text.\n")
    tmp_vault.note("Workspaces/Focus.md", WS)
    return tmp_vault


def _scoped(fn, *a, **kw):
    tok = wsk.open_scope(wsk.find_workspace("Focus"))
    try:
        assert wsk.allow_keys() == frozenset({"Inside/Member"})
        return fn(*a, **kw)
    finally:
        wsk.close_scope(tok)


def test_timeline(vault):
    from silica.tools.graph import silica_timeline

    out = _scoped(silica_timeline)
    assert "Secret" not in out["timeline"] and "Member" in out["timeline"]


def test_orphans(vault):
    from silica.tools.atomic import silica_orphans

    assert not any("Outside/" in p for p in _scoped(silica_orphans)["orphans"])


def test_concepts_by_term(vault):
    from silica.tools.graph import silica_concepts, silica_cooccurrence_refresh

    silica_cooccurrence_refresh()
    out = _scoped(silica_concepts, term="kalman")
    assert out["notes"] and all(not n["path"].startswith("Outside/") for n in out["notes"])


def test_related(vault):
    from silica.tools.graph import silica_cooccurrence_refresh, silica_related

    silica_cooccurrence_refresh()
    out = _scoped(silica_related, note="Member", memory=False)
    assert not any(r["path"].startswith("Outside/") for r in out.get("results", []))


def test_review_queue(vault):
    from silica.tools.atomic import silica_review_queue

    assert not any("Outside/" in r["path"] for r in _scoped(silica_review_queue, limit=10))


def test_learn_items(vault, monkeypatch):
    from silica.kernel.learn import items
    from silica.tools.atomic import silica_learn_items

    monkeypatch.setattr(items, "build_session", lambda picked, backfill, n: list(picked))
    monkeypatch.setattr(items, "cue_payload", lambda built: [str(b) for b in built])
    assert "Outside/" not in str(_scoped(silica_learn_items, n=8)["items"])


def test_recall_semantic_and_bm25(vault, monkeypatch):
    import silica.agent.providers as prov
    from silica.tools.atomic import silica_search_context
    from silica.tools.graph import silica_cooccurrence_refresh, silica_recall, silica_semantic_search

    monkeypatch.setattr(prov, "get_reranker", lambda *a, **k: None)
    silica_cooccurrence_refresh()
    r = _scoped(silica_recall, "kalman filter covariance", memory=False)
    s = _scoped(silica_semantic_search, "kalman filter covariance", memory=False)
    c = _scoped(silica_search_context, "kalman covariance")
    assert "Outside" not in str(r.get("notes")) and "Outside" not in str(s.get("results"))
    assert all(h["path"].startswith("Inside/") for h in c.get("hits", []))


def test_the_instructions_line_names_the_vault_map_as_the_whole_vault(vault):
    # The seed's vault map names hub notes vault-wide; the scoped turn's line
    # says so (a mitigation: the map itself is the seed every chat shares).
    line = wsk.instructions_message(wsk.find_workspace("Focus"), 1)
    assert "vault map above describes the whole vault" in line
