from __future__ import annotations

import unittest

from silica.agent.providers import _to_wire, SILICA_CLI_OPEN, SILICA_CLI_CLOSE


class ToWireTest(unittest.TestCase):
    def test_cli_origin_is_stripped_and_wrapped(self):
        msg = {"role": "user", "content": "Run a vault audit.", "origin": "cli"}
        wire = _to_wire(msg)
        self.assertNotIn("origin", wire)
        self.assertEqual(
            wire["content"],
            f"{SILICA_CLI_OPEN}Run a vault audit.{SILICA_CLI_CLOSE}",
        )
        self.assertEqual(wire["role"], "user")

    def test_human_turn_passes_through_unchanged(self):
        msg = {"role": "user", "content": "hello"}
        wire = _to_wire(msg)
        self.assertIs(wire, msg)  # same object, untouched

    def test_cli_origin_with_empty_content_is_not_wrapped(self):
        msg = {"role": "user", "content": "", "origin": "cli"}
        wire = _to_wire(msg)
        self.assertNotIn("origin", wire)
        self.assertEqual(wire["content"], "")  # no marker around empty body

    def test_non_user_roles_with_origin_are_only_stripped(self):
        # defensive: a stray origin on a non-cli value is just removed
        msg = {"role": "user", "content": "x", "origin": "other"}
        wire = _to_wire(msg)
        self.assertNotIn("origin", wire)
        self.assertEqual(wire["content"], "x")

    def test_kept_thinking_never_reaches_the_provider(self):
        # the trace is kept for the transcript; re-sending it re-bills it once
        # per iteration of the tool loop, which is why it lives behind _to_wire
        msg = {"role": "assistant", "content": "done", "silica_reasoning": "long trace"}
        wire = _to_wire(msg)
        self.assertNotIn("silica_reasoning", wire)
        self.assertEqual(wire, {"role": "assistant", "content": "done"})
        self.assertIn("silica_reasoning", msg)  # the history keeps its copy

    def test_context_meter_counts_the_wire_form(self):
        from silica.cli import _count_context_tokens

        plain = [{"role": "assistant", "content": "done"}]
        kept = [{"role": "assistant", "content": "done", "silica_reasoning": "word " * 500}]
        self.assertEqual(_count_context_tokens(plain), _count_context_tokens(kept))


if __name__ == "__main__":
    unittest.main()
