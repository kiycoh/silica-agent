# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""silica_recall(notes=[...]): the question names its notes.

On the 2026-09-23 prompt A/B the model reached for recall in 8 of 185 tool
calls; for "according to Lecture 32" it ran search, exists and read instead.
Naming the notes makes recall that chain in one call, and `missing` answers
the existence question the chain was asked to settle.
"""
from __future__ import annotations

from silica.config import CONFIG
from silica.tools import graph


def _vault(tmp_path, monkeypatch):
    import silica.driver

    v = tmp_path / "v"
    (v / "Corso").mkdir(parents=True)
    (v / "vault.yaml").write_text("write_dir: ''\n", encoding="utf-8")
    (v / "Corso" / "Lezione 30.md").write_text("# Lezione 30\n\nLe reti ricorrenti processano sequenze.\n",
                                               encoding="utf-8")
    (v / "Corso" / "Lezione 31.md").write_text("# Lezione 31\n\nIl boosting pesa gli errori.\n",
                                               encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(v))
    monkeypatch.setattr(CONFIG, "memory_vault", str(tmp_path / "no-such-memory"))
    monkeypatch.setattr(silica.driver, "_driver", None)
    return v


def test_named_notes_are_the_context_and_the_absent_ones_are_named(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)

    def no_retrieval(*a, **kw):
        raise AssertionError("named notes must not go through retrieval")
    monkeypatch.setattr("silica.kernel.recall.perception.facade_retrieve", no_retrieval)

    r = graph.silica_recall("riassumi", notes=["Lezione 30", "Lezione 99", "Corso/Lezione 31.md"])

    assert r["notes"] == ["Corso/Lezione 30", "Corso/Lezione 31"]
    assert r["missing"] == ["Lezione 99"]
    assert "reti ricorrenti" in r["context"] and "boosting" in r["context"]
    assert r["partial"] == []  # both arrived whole


def test_every_named_note_missing_is_an_empty_context_not_a_search(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    monkeypatch.setattr("silica.kernel.recall.perception.facade_retrieve",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("searched")))

    r = graph.silica_recall("riassumi", notes=["Lezione 99"])

    assert r["notes"] == [] and r["missing"] == ["Lezione 99"]


def test_notes_with_a_peek_is_refused(tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    monkeypatch.setattr(graph, "_peek_target", lambda vault: "/elsewhere")

    r = graph.silica_recall("q", notes=["Lezione 30"], vault="/elsewhere")

    assert "error" in r and "active vault" in r["error"]
