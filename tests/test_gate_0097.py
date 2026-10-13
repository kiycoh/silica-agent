# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""scripts/gate_0097.py on a hand-made narration: which turns count, which
are left out, and why a turn fails (ADR-0097, Gate (point 2))."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "gate_0097", Path(__file__).resolve().parent.parent / "scripts" / "gate_0097.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

ALPHA = "The kiln at the north studio fires stoneware at cone ten every second Tuesday."
BETA = "Marta keeps the spare glaze buckets under the bench beside the slab roller."


class Session:
    def __init__(self, vault: Path, sid: str):
        self.sid, self.beats = sid, []
        self.add("session", payload={"vault": str(vault)})

    def add(self, kind, status="done", id=None, payload=None):
        self.beats.append({"seq": len(self.beats) + 1, "ts": float(len(self.beats)), "sid": self.sid,
                           "id": id, "kind": kind, "status": status, "payload": payload or {}})

    def user(self, text, named=()):
        if named:
            text += ("\n\n<named-notes>\nNotes the user named (read one before answering about "
                     "its content): " + " · ".join(named) + "\n</named-notes>")
        self.add("turn", payload={"message": {"role": "user", "content": text}})
        self.add("call", "done", payload={"model": "custom/m"})

    def tool(self, name, args, result, status="done"):
        cid = f"c{len(self.beats)}"
        self.add("tool", "running", cid, {"name": name, "args": args})
        self.add("tool", status, cid, {"name": name, "result": result})

    def answer(self):
        self.add("turn", payload={"message": {"role": "assistant", "content": "answer"}})

    def write(self, path: Path):
        path.write_text("".join(json.dumps(b) + "\n" for b in self.beats))


def test_counts_strict_and_lenient_and_names_each_miss(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Notes").mkdir(parents=True)
    (vault / "Notes/Alpha.md").write_text(f"# Alpha\n\n{ALPHA}\n")
    (vault / "Notes/Beta.md").write_text(f"# Beta\n\n{BETA}\n")
    (vault / "Notes/Gamma.md").write_text("---\ntags: [x]\n---\n# Gamma\n\nShort.\n")
    s = Session(vault, "s1")
    s.user("what about [[Alpha]]?", ["Notes/Alpha.md"])                       # pass
    s.tool("silica_read_note", {"name": "Alpha"}, f"# Alpha\n\n{ALPHA}")
    s.answer()
    s.user("compare [[Alpha]] and [[Beta]]", ["Notes/Alpha.md", "Notes/Beta.md"])  # lenient only
    s.tool("silica_read_note", {"name": "Notes/Alpha.md"}, f"# Alpha\n\n{ALPHA}")
    s.tool("silica_search_context", {"query": "glaze"},
           json.dumps({"hits": [{"path": "Notes/Beta.md", "snippet": BETA[:60] + "..."}]}))
    s.answer()
    s.user("add a line to [[Gamma]]", ["Notes/Gamma.md"])                     # excluded
    s.tool("silica_patch_note", {"name": "Gamma", "heading": "x", "snippet": "y",
                                 "source_basename": "z"}, "{}")
    s.answer()
    s.user("hello, no link here")                                             # not counted
    s.answer()
    s.user("and [[Beta]]?", ["Notes/Beta.md"])                                # read failed
    s.tool("silica_read_note", {"name": "Beta"}, '{"error": "FileNotFoundError: Beta"}')
    s.answer()
    s.user("summarise [[Gamma]]", ["Notes/Gamma.md"])                         # not read
    s.tool("silica_search", {"query": "short"}, '{"paths": [], "hint": "short"}')
    s.answer()
    s.user("what is in [[Gamma]]?", ["Notes/Gamma.md"])                       # lenient only
    s.tool("silica_recall", {"query": "gamma", "notes": ["Gamma"]},
           json.dumps({"context": "[#1 | Notes/Gamma]\n# Gamma\n\nShort.\n"}))
    s.answer()
    s.user("and [[Alpha]] again", ["Notes/Alpha.md"])                       # no answer yet
    s.write(tmp_path / "s1.jsonl")

    r = gate.count([tmp_path], first=50)["custom/m"]

    assert (r["n"], r["writes"], r["unanswered"], r["strict"], r["lenient"]) == (5, 1, 1, 1, 3)
    assert [(tid, total, missed) for tid, total, missed in r["failures"]] == [
        ("s1#7", 2, [("Notes/Beta.md", "via silica_search_context")]),
        ("s1#22", 1, [("Notes/Beta.md", "read failed")]),
        ("s1#27", 1, [("Notes/Gamma.md", "not read")]),
        ("s1#32", 1, [("Notes/Gamma.md", "via silica_recall")]),
    ]
    # The ADR counts the first 50 per model; --first caps the same way.
    assert gate.count([tmp_path], first=2)["custom/m"]["n"] == 2
