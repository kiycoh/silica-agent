# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Reuse probe — count the results a chat recomputes with identical args.

`identical_prior_calls` reads the accumulated history so it spans turns, and
must see through provider spacing: the same arguments serialized differently are
still the same call, and a different key/value is not.
"""
from silica.agent.loop import identical_prior_calls


def _assistant(name, arguments):
    return {"role": "assistant", "tool_calls": [
        {"id": "x", "type": "function", "function": {"name": name, "arguments": arguments}}
    ]}


def test_counts_repeat_across_turns_ignoring_spacing_and_key_order():
    msgs = [
        {"role": "user", "content": "hi"},
        _assistant("silica_read_note", '{"name": "Alpha"}'),
        {"role": "tool", "tool_call_id": "x", "content": "..."},
        {"role": "user", "content": "and again"},
        _assistant("silica_read_note", '{"name":"Alpha"}'),   # same call, tighter JSON
    ]
    assert identical_prior_calls(msgs, "silica_read_note", '{"name": "Alpha"}') == 2


def test_distinct_args_and_names_are_not_repeats():
    msgs = [
        _assistant("silica_read_note", '{"name": "Alpha"}'),
        _assistant("silica_read_note", '{"name": "Beta"}'),
        _assistant("silica_search_context", '{"query": "Alpha"}'),
    ]
    assert identical_prior_calls(msgs, "silica_read_note", '{"name": "Beta"}') == 1


def test_malformed_arguments_do_not_raise():
    msgs = [_assistant("silica_read_note", "{not json")]
    assert identical_prior_calls(msgs, "silica_read_note", '{"name": "Alpha"}') == 0
