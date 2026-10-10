"""The one check behind `_int_flag`, the parser the five `--flag=N` sites share."""
from __future__ import annotations

from silica.cli import _REFRESH, _int_flag


class TestIntFlag:
    def test_parses_value(self):
        assert _int_flag(["query", "--k=7"], "--k=", 5) == 7

    def test_absent_keeps_default(self):
        assert _int_flag(["query"], "--k=", 5) == 5

    def test_garbage_keeps_default(self):
        assert _int_flag(["--k=abc"], "--k=", 5) == 5
        assert _int_flag(["--k="], "--k=", 5) == 5

    def test_longer_flag_is_not_a_prefix_match(self):
        # /report reads --top-k=; /find reads --k=. Neither may eat the other.
        assert _int_flag(["--top-k=15"], "--k=", 5) == 5
        assert _int_flag(["--top-k=15"], "--top-k=", 10) == 15


def test_refresh_commands_map_to_real_tools():
    from silica.tools import TOOLS

    for cmd, (tool, _label) in _REFRESH.items():
        assert tool in TOOLS, f"{cmd} points at a missing tool"
