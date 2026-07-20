# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Checks for the multi-agent shared-memory primitives:

  1. path_lease holds a real OS-level flock for its whole scope (cross-process
     serialisation), and releases it on exit.
  2. ensure_system_floor stamps a YAML-safe `agent:` field iff SILICA_AGENT_ID
     is set, and never touches single-user writes otherwise.
"""
import os

import pytest

fcntl = pytest.importorskip("fcntl")  # POSIX-only; point 1 no-ops elsewhere

from silica.config import CONFIG
from silica.kernel import paths, templates, workqueue


def test_flock_held_during_lease_and_released_after(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "_SILICA_HOME", tmp_path)
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path), raising=False)

    key = workqueue._lease_key("Foo/Bar.md")
    with workqueue.path_lease("Foo/Bar.md"):
        lf = workqueue._lock_file(key)
        assert lf is not None and lf.exists()
        # A separate open file description must NOT be able to grab it now.
        fd = os.open(str(lf), os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    # Released: a non-blocking acquire now succeeds.
    fd = os.open(str(lf), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_agent_stamp_opt_in(monkeypatch):
    body = "some text"

    # Unset → no agent field, single-user writes unchanged.
    monkeypatch.delenv("SILICA_AGENT_ID", raising=False)
    assert "agent:" not in templates.ensure_system_floor(body)

    # Set → field present and refreshed (last-writer-wins, like last modified).
    monkeypatch.setenv("SILICA_AGENT_ID", "coder-7")
    out = templates.ensure_system_floor(body)
    assert 'agent: "coder-7"' in out
    monkeypatch.setenv("SILICA_AGENT_ID", "coder-9")
    assert 'agent: "coder-9"' in templates.ensure_system_floor(out)
    assert out.count("agent:") == 1  # refreshed, not duplicated

    # A malicious value cannot break or inject YAML.
    monkeypatch.setenv("SILICA_AGENT_ID", 'x"\ninjected: true')
    hostile = templates.ensure_system_floor(body)
    assert "injected: true" not in hostile
    assert 'agent: "x\\""' in hostile


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
