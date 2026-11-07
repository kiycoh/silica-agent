# SPDX-License-Identifier: AGPL-3.0-or-later
"""A late system line goes before the seed's closing language rule, on every path."""
from __future__ import annotations

from silica.kernel import system_lines


def _seed():
    return [{"role": "system", "content": "prompt"}, {"role": "system", "content": "language rule"}]


def test_a_late_line_goes_before_the_language_rule():
    messages = _seed() + [{"role": "user", "content": "hi"}]
    system_lines.insert(messages, {"role": "system", "content": "late", "origin": "x"})
    assert [m["content"] for m in messages] == ["prompt", "late", "language rule", "hi"]
    assert system_lines.index_of(messages, "x") == 1 and system_lines.index_of(messages, "y") is None


def test_a_seed_with_no_turn_yet_keeps_the_rule_last():
    messages = _seed()
    system_lines.insert(messages, {"role": "system", "content": "late"})
    assert [m["content"] for m in messages] == ["prompt", "late", "language rule"]


def test_a_list_with_no_seed_takes_the_line_first():
    messages = [{"role": "user", "content": "hi"}]
    system_lines.insert(messages, {"role": "system", "content": "late"})
    assert messages[0]["content"] == "late"
