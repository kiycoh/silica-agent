from __future__ import annotations

import unittest

from silica.model.wire import to_wire, SILICA_CLI_OPEN, SILICA_CLI_CLOSE


class ToWireTest(unittest.TestCase):
    def test_cli_origin_is_stripped_and_wrapped(self):
        msg = {"role": "user", "content": "Run a vault audit.", "origin": "cli"}
        wire = to_wire(msg)
        self.assertNotIn("origin", wire)
        self.assertEqual(
            wire["content"],
            f"{SILICA_CLI_OPEN}Run a vault audit.{SILICA_CLI_CLOSE}",
        )
        self.assertEqual(wire["role"], "user")

    def test_human_turn_passes_through_unchanged(self):
        msg = {"role": "user", "content": "hello"}
        wire = to_wire(msg)
        self.assertIs(wire, msg)  # same object, untouched

    def test_cli_origin_with_empty_content_is_not_wrapped(self):
        msg = {"role": "user", "content": "", "origin": "cli"}
        wire = to_wire(msg)
        self.assertNotIn("origin", wire)
        self.assertEqual(wire["content"], "")  # no marker around empty body

    def test_non_user_roles_with_origin_are_only_stripped(self):
        # defensive: a stray origin on a non-cli value is just removed
        msg = {"role": "user", "content": "x", "origin": "other"}
        wire = to_wire(msg)
        self.assertNotIn("origin", wire)
        self.assertEqual(wire["content"], "x")

    def test_kept_thinking_never_reaches_the_provider(self):
        # the trace is kept for the transcript; re-sending it re-bills it once
        # per iteration of the tool loop, which is why it lives behind to_wire
        msg = {"role": "assistant", "content": "done", "silica_reasoning": "long trace"}
        wire = to_wire(msg)
        self.assertNotIn("silica_reasoning", wire)
        self.assertEqual(wire, {"role": "assistant", "content": "done"})
        self.assertIn("silica_reasoning", msg)  # the history keeps its copy

    def test_context_meter_counts_the_wire_form(self):
        from silica.session.meter import count_context_tokens

        plain = [{"role": "assistant", "content": "done"}]
        kept = [{"role": "assistant", "content": "done", "silica_reasoning": "word " * 500}]
        self.assertEqual(count_context_tokens(plain), count_context_tokens(kept))


if __name__ == "__main__":
    unittest.main()


def test_a_key_nobody_registered_never_reaches_the_provider():
    """The wire is an allow-list: `silica_story` rode to the provider for missing
    from a list of keys to strip."""
    wire = to_wire({"role": "user", "content": "q", "silica_story": {"title": "t"}})
    assert wire == {"role": "user", "content": "q"}


def test_the_reasoning_a_route_reads_back_survives():
    msg = {"role": "assistant", "content": "a", "reasoning_content": "r", "thinking_blocks": [],
           "tool_calls": [], "silica_reasoning": "r"}
    assert set(to_wire(msg)) == {"role", "content", "reasoning_content", "thinking_blocks", "tool_calls"}
