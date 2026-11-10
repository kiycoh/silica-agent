# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Curation reads bypass the read-gate banner (spec-stale-triggers §4): the
loser's text the judge reads starts with one line from the peek map. Annotate
only; the model decides, nothing blocks or skips."""
from types import SimpleNamespace

import pytest

import silica.driver as driver_mod
from silica.kernel.code import codedocs
from silica.kernel.recall import duplicates


@pytest.fixture
def stub_read(monkeypatch):
    """Install a fake note reader behind the driver proxy, drop it after.

    Never `monkeypatch.setattr(DRIVER, ...)`: DRIVER is a lazy proxy whose
    __getattr__ builds the real backend, so monkeypatch's old-value probe
    caches one against the current CONFIG.vault_path and every later test in
    the session reads the wrong vault. set_driver is the seam.
    """
    def install(content="the body", exc=None):
        def read_note(path):
            if exc is not None:
                raise exc(path)
            return SimpleNamespace(content=content)

        driver_mod.set_driver(SimpleNamespace(read_note=read_note))

    yield install
    driver_mod.set_driver(None)


def test_stale_note_body_is_prefixed(monkeypatch):
    monkeypatch.setattr(codedocs, "peek", lambda v: {"a/n.md": "structural"})
    out = duplicates._loser_text("a/n.md", "the body")
    assert out == ("[stale] structural: verify against the source before "
                   "reusing claims\n\nthe body")


def test_fresh_note_is_byte_identical(monkeypatch):
    monkeypatch.setattr(codedocs, "peek", lambda v: {})
    assert duplicates._loser_text("a/n.md", "the body") == "the body"


def test_peek_failure_never_fails_the_read(monkeypatch):
    def boom(v):
        raise RuntimeError("boom")

    monkeypatch.setattr(codedocs, "peek", boom)
    assert duplicates._loser_text("a/n.md", "the body") == "the body"


def test_missing_note_reads_as_none(stub_read):
    stub_read(exc=FileNotFoundError)
    assert duplicates._read("a/n.md") is None


def test_empty_stale_note_gets_no_dangling_prefix(monkeypatch):
    """A prefix with nothing under it is noise, not a warning."""
    monkeypatch.setattr(codedocs, "peek", lambda v: {"a/n.md": "cosmetic"})
    assert duplicates._loser_text("a/n.md", "") == ""


def test_curate_hands_the_judge_the_stale_line(monkeypatch):
    bodies = {"a/n.md": "the body", "b/m.md": "the merge target, " * 20}
    monkeypatch.setattr(duplicates, "_read", bodies.get)
    monkeypatch.setattr(codedocs, "peek", lambda v: {"a/n.md": "structural"})
    [item] = duplicates.families_to_items([{"source": "a/n.md", "target": "b/m.md", "score": 0.1}])
    assert item.target_path == "b/m.md"
    assert item.context.excerpt.startswith("[stale] structural:")
