# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Curation reads bypass the read-gate banner (spec-stale-triggers §4): the
direct-disk helper prefixes one line from the peek map. Annotate only; the
model decides, nothing blocks or skips."""
from types import SimpleNamespace

import pytest

import silica.driver as driver_mod
from silica.kernel.code import codedocs
from silica.tools import curate


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


def test_stale_note_body_is_prefixed(stub_read, monkeypatch):
    stub_read()
    monkeypatch.setattr(codedocs, "peek", lambda v: {"a/n.md": "structural"})
    out = curate._read_body("a/n.md")
    assert out == ("[stale] structural: verify against the source before "
                   "reusing claims\n\nthe body")


def test_fresh_note_is_byte_identical(stub_read, monkeypatch):
    stub_read()
    monkeypatch.setattr(codedocs, "peek", lambda v: {})
    assert curate._read_body("a/n.md") == "the body"


def test_peek_failure_never_fails_the_read(stub_read, monkeypatch):
    stub_read()

    def boom(v):
        raise RuntimeError("boom")

    monkeypatch.setattr(codedocs, "peek", boom)
    assert curate._read_body("a/n.md") == "the body"


def test_missing_note_still_yields_empty(stub_read, monkeypatch):
    stub_read(exc=FileNotFoundError)
    monkeypatch.setattr(codedocs, "peek", lambda v: {"a/n.md": "structural"})
    assert curate._read_body("a/n.md") == ""


def test_empty_stale_note_gets_no_dangling_prefix(stub_read, monkeypatch):
    """A prefix with nothing under it is noise, not a warning."""
    stub_read(content="")
    monkeypatch.setattr(codedocs, "peek", lambda v: {"a/n.md": "cosmetic"})
    assert curate._read_body("a/n.md") == ""
