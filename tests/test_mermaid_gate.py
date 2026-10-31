"""The mermaid check on the model's own note writes: a diagram that does not
parse goes back to the model with the parser's message, at most four times,
and then is written as sent. Code calling the same functions is never refused.

The broken diagram is the line the 2026-10-28 mind map failed on."""
from __future__ import annotations

import json
import shutil
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import silica.kernel.write.checkpoints as checkpoints
from silica.kernel.link import mermaid
from silica.kernel.link.mermaid import parse_errors
from silica.tools import MODEL_CALL, TOOLS
from silica.tools.notes import silica_write_note

BROKEN = "```mermaid\nmindmap\n  root((ML))\n    i.i.d. ([[Lezione 5]], [[Lezione 6]])\n```\n"
FIXED = '```mermaid\nmindmap\n  root((ML))\n    A["i.i.d. (Lezione 5, 6)"]\n```\n'

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="the check runs mermaid under Node")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    monkeypatch.setattr("silica.config.CONFIG.vault_path", str(vault_dir))
    monkeypatch.setattr("silica.driver._driver", None)
    monkeypatch.setattr("silica.kernel.write.checkpoints._store", None)
    checkpoints.get_checkpoint_store(tmp_path / "checkpoints.db")
    yield vault_dir
    monkeypatch.setattr("silica.driver._driver", None)
    monkeypatch.setattr("silica.kernel.write.checkpoints._store", None)


def _as_model(tool: str, **args) -> dict:
    """The call as the agent loop makes it."""
    token = MODEL_CALL.set(tool)
    try:
        return json.loads(TOOLS[tool].run(**args))
    finally:
        MODEL_CALL.reset(token)


@needs_node
def test_the_error_names_the_line_mermaid_stops_at():
    [err] = parse_errors("# M\n\n" + BROKEN + "\n" + FIXED)
    assert err.startswith("diagram 1: Parse error on line 3") and "got 'NODE_ID'" in err
    assert parse_errors(FIXED) == []


@needs_node
def test_a_blockquoted_fence_reaches_mermaid_without_its_quote_marks():
    assert parse_errors("> [!note]\n> ```mermaid\n> graph LR\n>   A --> B\n> ```\n") == []


def test_without_node_nothing_is_checked(monkeypatch):
    monkeypatch.setattr(mermaid.shutil, "which", lambda _name: None)
    assert parse_errors(BROKEN) == []


@needs_node
def test_the_model_gets_the_error_and_its_fix_is_written(vault):
    out = _as_model("silica_write_note", path="M.md", body=BROKEN)
    assert "correction 1 of 4" in out["error"] and "Parse error on line 3" in out["error"]
    assert not (vault / "M.md").exists()
    out = _as_model("silica_write_note", path="M.md", body=FIXED)
    assert out["success"] is True and "warning" not in out
    assert 'A["i.i.d. (Lezione 5, 6)"]' in (vault / "M.md").read_text(encoding="utf-8")


@needs_node
def test_the_fifth_call_is_written_as_sent_with_a_warning_the_model_reads(vault):
    for n in range(1, 5):
        assert f"correction {n} of 4" in _as_model("silica_write_note", path="M.md", body=BROKEN)["error"]
    out = _as_model("silica_write_note", path="M.md", body=BROKEN)
    assert out["success"] is True
    assert len(out["warning"]) <= 80   # what the eager projection keeps
    assert "Lezione 5" in (vault / "M.md").read_text(encoding="utf-8")


@needs_node
def test_code_and_other_tools_writing_a_note_are_never_refused(vault):
    """A reading card or a rework has no one to fix the diagram."""
    assert silica_write_note(path="A.md", body=BROKEN)["success"] is True
    token = MODEL_CALL.set("silica_rework")
    try:
        assert silica_write_note(path="B.md", body=BROKEN)["success"] is True
    finally:
        MODEL_CALL.reset(token)


@needs_node
def test_a_patch_is_checked_on_its_snippet_not_the_note(vault):
    (vault / "N.md").write_text("# N\n\n" + BROKEN, encoding="utf-8")   # the person's own diagram
    out = _as_model("silica_patch_note", name="N.md", heading="N", snippet="More text.", source_basename="chat")
    assert out["success"] is True
    out = _as_model("silica_patch_note", name="N.md", heading="Map", snippet=BROKEN, source_basename="chat")
    assert "correction 1 of 4" in out["error"]


def test_the_loop_marks_the_call_it_runs_for_the_model():
    seen = []

    class _Recorder:
        name, collapse, sensitive, internal, summarize = "fake_write", "eager", False, False, None

        def json_schema(self, reach=None):
            return {"type": "function", "function": {"name": self.name, "description": "",
                                                     "parameters": {"type": "object", "properties": {}}}}

        def run(self, _cancel_token=None, **kw):
            seen.append(MODEL_CALL.get())
            return '{"success": true}'

    replies = iter([[SimpleNamespace(id="c1", name="fake_write", args={})], []])

    def fake_call_llm(*a, **k):
        calls = next(replies)
        message = {"role": "assistant", "content": "" if calls else "done"}
        if calls:
            message["tool_calls"] = [{"id": "c1", "type": "function",
                                      "function": {"name": "fake_write", "arguments": "{}"}}]
        return SimpleNamespace(assistant_message=message, tool_calls=calls,
                               text="" if calls else "done", reasoning=None, usage={})

    from silica.agent.loop import run_agent

    with patch.dict("silica.agent.loop.TOOLS", {"fake_write": _Recorder()}, clear=True), \
         patch("silica.agent.loop.call_llm", fake_call_llm):
        run_agent([{"role": "user", "content": "go"}], model="test")
    assert seen == ["fake_write"] and MODEL_CALL.get() is None
