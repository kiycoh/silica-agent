# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Whether the ```mermaid fences of a Markdown text parse, asked of the same
mermaid build the web UI draws them with (ui/web/static/mermaid.min.js), run
by Node. A check in Python could only guess at mermaid's grammar: the keyword
check in health/lint.py passed the mind map that the UI could not draw on
2026-10-28."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from markdown_it import MarkdownIt

logger = logging.getLogger(__name__)

BUNDLE = Path(__file__).resolve().parents[2] / "ui" / "web" / "static" / "mermaid.min.js"

# mermaid's DOMPurify defines its API only on a window whose document has
# nodeType 9, and with no createHTMLDocument it stays unsupported and its
# sanitize() hands the text back, which is all parse() needs. Measured on
# mermaid 11.16.0: a bump that reaches further into the DOM fails the load, and
# test_mermaid_gate's broken diagram then stops being reported.
_RUNNER = r"""
const fs = require("fs"), vm = require("vm");
// Node's own report of a failed load quotes the source line: the whole bundle.
process.on("uncaughtException", (e) => { process.stderr.write(String((e && e.stack) || e)); process.exit(2); });
globalThis.window = {document: {nodeType: 9, implementation: {}, createElement: () => ({})},
                     Element: function () {}, addEventListener() {}};
globalThis.document = window.document;
vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));
(async () => {
  const out = [];
  for (const text of JSON.parse(fs.readFileSync(0, "utf8"))) {
    try { await mermaid.parse(text); out.push(null); }
    catch (e) { out.push(String((e && e.message) || e)); }
  }
  process.stdout.write("\n" + JSON.stringify(out) + "\n");
})();
"""


def parse_errors(text: str) -> list[str]:
    """One message per mermaid fence in `text` that does not parse, numbered
    by its place among the fences. [] when every fence parses, and also when
    Node cannot answer: a note is never refused because the host has no Node
    (the Dockerfile installs it; a local install may not have it)."""
    # The fences the renderer draws (server._ofm_blocks), a blockquoted one
    # included, where a regex would hand mermaid the `> ` of every line.
    blocks = [t.content for t in MarkdownIt().parse(text)
              if t.type == "fence" and t.info.strip() == "mermaid"]
    node = shutil.which("node")
    if not blocks or node is None:
        return []
    # Unchecked is what every note was before this check existed: on a failure
    # below the write goes on, and the log says why its diagrams were not read.
    try:
        proc = subprocess.run([node, "-e", _RUNNER, str(BUNDLE)], input=json.dumps(blocks),
                              capture_output=True, encoding="utf-8", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("mermaid check skipped: %s", e)
        return []
    try:
        verdicts = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        logger.warning("mermaid check skipped, node exited %s: %s", proc.returncode, proc.stderr[:400])
        return []
    return [f"diagram {i}: {msg}" for i, msg in enumerate(verdicts, 1) if msg]
