"""The chat card of a refused delete (ADR-0092 point 2).

A tool that returns `{"error": ...}` still ends in a ToolCompleteEvent, and the
live frame said nothing about it: the GUI claimed the refused delete as
"deleted" while a reloaded chat, which reads the stored result, drew it as
failed. The frame now says the call failed and, for a delete refused because
other notes link the note, carries the card's line and the path `/delete`
takes; the replay carries the same.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from silica.agent.events import ToolCompleteEvent
from silica.ui.web.callback import event_to_json, tool_calls_to_json

REFUSAL = {
    "error": "not deleted: 3 notes link to Autoencoders",
    "linked_from": ["A.md", "B.md", "C.md"],
    "path": "Corso/Autoencoders.md",
    "next": "Deleting a note other notes link to is the user's act. ...",
}


def _done(name: str, result) -> dict:
    return event_to_json(ToolCompleteEvent(name, {"ref": "Autoencoders"}, "c1",
                                           json.dumps(result), 0.1, 0))


def test_a_refused_delete_frame_is_failed_and_carries_the_card_line():
    out = _done("silica_delete", REFUSAL)
    assert out["failed"] is True
    assert out["refused"] == {"text": "3 notes link to it",
                              "path": "Corso/Autoencoders.md"}


def test_one_referrer_reads_in_the_singular():
    out = _done("silica_delete", {**REFUSAL, "linked_from": ["A.md"]})
    assert out["refused"]["text"] == "1 note links to it"


def test_any_error_result_is_failed_but_only_a_linked_delete_is_refused():
    out = _done("silica_write_note", {"error": "Note already exists"})
    assert out["failed"] is True and "refused" not in out
    out = _done("silica_delete", {"error": "File not found"})
    assert out["failed"] is True and "refused" not in out


def test_a_success_frame_carries_neither():
    out = _done("silica_delete", {"success": True, "deleted": "Corso/Lonely.md", "orphaned": []})
    assert "failed" not in out and "refused" not in out


def test_the_refusal_has_its_own_sentence_even_beside_a_path_that_reads_as_another():
    from silica.ui.web.failures import sentence

    raw = json.dumps({**REFUSAL, "linked_from": ["Reti/Connection timeout.md"]})
    assert sentence(raw).startswith("Not deleted: other notes link to this note.")


def test_the_replay_carries_the_same_refusal():
    msg = {"role": "assistant", "tool_calls": [{"id": "c1", "function": {
        "name": "silica_delete", "arguments": json.dumps({"ref": "Autoencoders"})}}]}
    [line] = tool_calls_to_json(msg, {"c1"}, {"c1": json.dumps(REFUSAL)})
    assert line["error"] is True
    assert line["refused"] == {"text": "3 notes link to it",
                               "path": "Corso/Autoencoders.md"}


STATIC = Path(__file__).resolve().parent.parent / "silica" / "ui" / "web" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_claim_refs_keeps_the_refusal_for_the_footer(tmp_path):
    src = (STATIC / "app-chat.js").read_text(encoding="utf-8")
    start = src.index("function claimRefs(")
    fn = src[start:src.index("\n}\n", start) + 3]
    script = tmp_path / "claim.js"
    script.write_text(fn + """
const touched = new Map(), failed = new Map();
claimRefs({notes: ["Autoencoders"], effect: "written", error: false}, touched, failed);
claimRefs({notes: ["Other"], effect: "written", error: false}, touched, failed);
claimRefs({notes: ["Autoencoders"], effect: "deleted", error: true, name: "delete",
           refused: {text: "3 notes link to it", path: "Corso/Autoencoders.md"}},
          touched, failed);
claimRefs({notes: ["Other"], effect: "written", error: true, name: "write"}, touched, failed);
claimRefs({notes: ["Third"], effect: "written", error: true, name: "write"}, touched, failed);
console.log(JSON.stringify({touched: [...touched], failed: [...failed]}));
""", encoding="utf-8")
    run = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    got = json.loads(run.stdout)
    # The turn flagged both notes first: a failed write of a written note
    # adds no card, a refused delete always does, since it is where the user
    # deletes the note.
    assert dict(got["touched"]) == {"Autoencoders": "written", "Other": "written"}
    assert dict(got["failed"]) == {
        "Autoencoders": {"verb": "delete",
                         "refused": {"text": "3 notes link to it",
                                     "path": "Corso/Autoencoders.md"}},
        "Third": {"verb": "write", "refused": None},
    }
