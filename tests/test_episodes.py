# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""`/episodes` — the file-level transparency valve on episodic memory.

Read-only: what the store holds, dated, grouped by key, live chains only.
Nothing is written unless the user names a path with `--save=`.
"""
from __future__ import annotations

import re

import pytest

# Rich style codes. Assertions on printed paths have to drop these: a wrapped
# line closes and reopens its style at the seam, landing escapes inside the
# very string under test.
_PLAIN = re.compile(r"\x1b\[[0-9;]*m")


def test_episodes_is_advertised_as_a_direct_command():
    """Read-only render of a local store: no LLM, so it belongs with /contested
    and /keep, not with the verbs that spend a turn."""
    from silica.ui.commands import COMMANDS

    cmd = next((c for c in COMMANDS if c.name == "/episodes"), None)
    assert cmd is not None and not cmd.repl_only
    assert cmd.group == "direct"


@pytest.fixture
def store(tmp_path, monkeypatch):
    from silica.kernel.recall import episodic

    path = tmp_path / "episodic.json"
    monkeypatch.setattr(episodic, "store_path", lambda: path)
    return path


def _episodes(line: str = "/episodes") -> bool:
    from silica.cli import _handle_direct_shortcut

    return _handle_direct_shortcut(line, [])


def _seed(path, key="user.dog.name", texts=("Rex", "Tom")):
    from silica.kernel.recall.episodic import EpisodicStore

    s = EpisodicStore(path=path)
    for i, text in enumerate(texts, start=1):
        s.capture([{"key": key, "text": text}], run_id=f"r{i}", seen=f"2026-06-1{i}")
    return s


class TestRender:
    def test_a_live_chain_is_printed_under_its_key_with_its_history(self, store, capsys):
        _seed(store)

        assert _episodes() is True  # handled inline, no LLM round-trip

        out = capsys.readouterr().out
        assert "user.dog.name" in out
        assert "Tom" in out          # the current value
        assert "Rex" in out          # the superseded one, dated
        assert "2026-06-11" in out

    def test_only_the_live_head_of_a_chain_gets_its_own_entry(self, store, capsys):
        """Superseded ancestors belong to their chain's history, not beside it."""
        _seed(store)

        assert _episodes() is True

        out = capsys.readouterr().out
        assert out.count("- [since") == 1
        assert "previously: Rex" in out

    def test_an_empty_store_says_so(self, store, capsys):
        assert _episodes() is True

        assert "no episodic" in capsys.readouterr().out.lower()


class TestSave:
    def test_nothing_is_written_unless_a_path_is_named(self, store, tmp_path):
        """No automatic file, no vault write, and the store itself is not even
        rewritten — a read is a read."""
        _seed(store)
        files_before = sorted(tmp_path.rglob("*"))
        touched_before = store.stat().st_mtime_ns

        assert _episodes() is True

        assert sorted(tmp_path.rglob("*")) == files_before
        assert store.stat().st_mtime_ns == touched_before  # not even re-saved

    def test_save_refuses_to_write_inside_the_vault(
        self, store, tmp_path, monkeypatch, capsys
    ):
        """Machine memory enters the vault by promotion only. `--save` is a
        valve out of Silica, not a second door into the vault around the gate."""
        from silica.config import CONFIG

        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(CONFIG, "vault_path", str(vault))
        _seed(store)

        assert _episodes(f"/episodes --save={vault / 'Concepts' / 'x.md'}") is True

        assert list(vault.rglob("*")) == []
        assert "/promote" in capsys.readouterr().out  # names the door that is open

    def test_no_vault_configured_is_not_the_working_directory(
        self, store, tmp_path, monkeypatch
    ):
        """`vault_path` is empty until a vault is adopted, and an empty path
        resolves to the cwd — which must not turn every relative save into a
        vault write."""
        from silica.config import CONFIG

        monkeypatch.setattr(CONFIG, "vault_path", "")
        monkeypatch.chdir(tmp_path)
        _seed(store)

        assert _episodes("/episodes --save=episodes.md") is True

        assert (tmp_path / "episodes.md").exists()

    def test_an_unwritable_save_path_is_reported_not_raised(
        self, store, tmp_path, capsys
    ):
        """The path comes straight from the user and the REPL calls this handler
        with no try/except: an OSError here would take the session down."""
        _seed(store)
        target = tmp_path / "a-directory"
        target.mkdir()

        assert _episodes(f"/episodes --save={target}") is True

        assert "save failed" in capsys.readouterr().out.lower()

    def test_save_writes_the_render_where_the_user_said(self, store, tmp_path, capsys):
        _seed(store)
        out = tmp_path / "elsewhere" / "episodes.md"  # a folder that does not exist yet

        assert _episodes(f"/episodes --save={out}") is True

        text = out.read_text(encoding="utf-8")
        assert "user.dog.name" in text
        assert "Tom" in text and "Rex" in text
        # Unwrapped AND de-styled: the console hard-wraps a long path at the
        # terminal width, and rich closes then reopens the bold run across the
        # seam, so the path arrives as "…episod\x1b[0m\x1b[1mes.md". Stripping
        # only the newline left the escapes in the middle of the path and the
        # assertion failed at 80 columns while passing at 200 — a test that
        # depended on the width of whoever ran it.
        printed = _PLAIN.sub("", capsys.readouterr().out).replace("\n", "")
        assert str(out) in printed  # says where it went
