# tests/test_live_preview.py
"""The drawer editor's Live Preview rule, run under node on the real bundle.

Obsidian's rule, and the whole of what makes the editor a preview: markup is
hidden and its effect drawn wherever the selection is not, and shown as typed
wherever it is. Checked on EditorState alone, which needs no DOM, so the test
runs the shipped codemirror.min.js and app-editor.js unmodified.
"""
import json
import shutil
import subprocess

import pytest

from tests.webassets import WEB

NOTE = (
    "---\ntags: [x]\n---\n"
    "# Head *em*\n\n"
    "- [ ] task **b**\n- item ==hi==\n\n"
    "> [!note] T\n> body\n\n"
    "| a | b |\n|---|---|\n| 1 | 2 |\n\n"
    "see [[Wiki|al]] and [x](https://e.com) #tag $x^2$\n"
)

PROBE = """
const vm = require("vm"), fs = require("fs");
globalThis.window = globalThis;
vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
vm.runInThisContext(fs.readFileSync(process.argv[3], "utf8"));
const L = window.LiveEditor, doc = process.argv[4], out = {};
const missing = JSON.parse(process.argv[6] || "[]");
for (const at of JSON.parse(process.argv[5])) {
  let st = CM.EditorState.create({ doc, extensions: L.extensions() });
  st = st.update({ effects: L.brokenEffect.of(missing) }).state;
  if (at >= 0) st = st.update({ selection: { anchor: at }, effects: L.focusEffect.of(true) }).state;
  const rows = [];
  L.buildDecorations(st).between(0, doc.length, (from, to, v) => {
    const s = v.spec;
    const kind = s.widget ? s.widget.constructor.name + (s.block ? ":block" : "")
      : (s.tagName && s.class ? s.tagName + "." + s.class.trim().split(" ").filter(Boolean).join(".") : s.tagName)
        || s.class || (from === to ? "line" : "hide");
    rows.push([kind, doc.slice(from, to)].concat(s.widget && s.widget.n !== undefined ? [s.widget.n]
      : s.widget && s.widget.text !== undefined ? [s.widget.text] : []));
  });
  out[at] = rows;
}
console.log(JSON.stringify(out));
"""


def _decorations(tmp_path, carets: list[int], note: str = NOTE,
                 missing: tuple[str, ...] = ()) -> dict[str, list[list[str]]]:
    probe = tmp_path / "probe.js"
    probe.write_text(PROBE)
    run = subprocess.run(
        ["node", str(probe), str(WEB / "codemirror.min.js"), str(WEB / "app-editor.js"),
         note, json.dumps(carets), json.dumps(list(missing))],
        capture_output=True, text=True, timeout=60,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    return json.loads(run.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the editor bundle")
def test_markup_hides_away_from_the_caret_and_shows_under_it(tmp_path):
    bold = NOTE.index("**b**") + 2
    table = NOTE.index("| 1 |")
    wiki = NOTE.index("[[Wiki") + 3
    got = _decorations(tmp_path, [-1, bold, table, wiki])

    # Unfocused, everything is drawn and no markup shows.
    rendered = got["-1"]
    assert ["Rendered:block", "---\ntags: [x]\n---"] in rendered  # properties
    assert ["hide", "# "] in rendered and ["em", "*em*"] in rendered
    assert ["Checkbox", "[ ]"] in rendered and ["hide", "- "] in rendered
    assert ["hide", "**"] in rendered and ["strong", "**b**"] in rendered
    assert ["Bullet", "-"] in rendered
    assert ["mark", "==hi=="] in rendered and ["hide", "=="] in rendered
    assert ["Rendered:block", "> [!note] T\n> body"] in rendered  # callout
    assert ["Rendered:block", "| a | b |\n|---|---|\n| 1 | 2 |"] in rendered
    assert ["hide", "[[Wiki|"] in rendered and ["a.cm-link.cm-link-live.note-link", "al"] in rendered
    assert ["hide", "](https://e.com)"] in rendered and ["a.cm-link.cm-link-live", "x"] in rendered
    assert ["tag", "#tag"] in rendered
    assert ["Rendered", "$x^2$"] in rendered

    # The caret in the bold opens the bold, and only the bold.
    at_bold = got[str(bold)]
    assert ["hide", "**"] not in at_bold and ["strong", "**b**"] in at_bold
    assert ["Checkbox", "[ ]"] in at_bold  # same line, but not touched
    assert ["hide", "=="] in at_bold

    # The caret in the table turns the block back into source lines.
    at_table = got[str(table)]
    assert not any(k == "Rendered:block" and v.startswith("| a") for k, v in at_table)
    assert at_table.count(["cm-table", ""]) == 3

    # The caret in the wikilink shows its brackets and target.
    at_wiki = got[str(wiki)]
    assert ["hide", "[[Wiki|"] not in at_wiki and ["hide", "]]"] not in at_wiki


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the editor bundle")
def test_the_editor_draws_what_the_reader_drew(tmp_path):
    """ADR-0059: the reader left the drawer only once Live Preview drew what it
    drew. A link the vault does not resolve is `note-link broken` and opens
    nothing; footnotes are numbered by first reference, as markdown-it numbers
    them, and a reference with no definition stays text."""
    note = ("See [[Here]] and [[Gone#h|gone]], a claim[^b] and[^a], again[^b], [^none].\n\n"
            "[^a]: First [[Here]].\n[^b]: Second.\n")
    got = _decorations(tmp_path, [-1, note.index("[^a]:") + 1], note=note, missing=("Gone",))
    rendered = got["-1"]
    assert ["a.cm-link.cm-link-live.note-link", "Here"] in rendered
    assert ["a.cm-link.note-link.broken", "gone"] in rendered  # never live: a press is a caret
    refs = [r for r in rendered if r[0] == "FootnoteRef"]
    assert refs == [["FootnoteRef", "[^b]", 1], ["FootnoteRef", "[^a]", 2], ["FootnoteRef", "[^b]", 1]]
    assert ["FootnoteLabel", "[^a]: ", "2"] in rendered and ["FootnoteLabel", "[^b]: ", "1"] in rendered
    assert not any("[^none]" == r[1] for r in rendered)
    # each definition is its own line block, and the wikilink in it is drawn
    assert rendered.count(["cm-footnote", ""]) == 2
    assert rendered.count(["a.cm-link.cm-link-live.note-link", "Here"]) == 2
    # the caret on a definition shows its marker as typed
    assert ["FootnoteLabel", "[^a]: ", "2"] not in got[str(note.index("[^a]:") + 1)]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the editor bundle")
def test_a_claim_stamp_is_hidden_away_from_the_caret(tmp_path):
    """The drawer showed `<!-- silica: valid_from=… -->` as a line of text,
    which the reader's renderer drops (2026-10-26). The whole line goes, and
    only that line; under the caret it shows as typed."""
    stamp = "<!-- silica: valid_from=2024-04-10 -->"
    note = f"# Spine\n\n{stamp}\n> The claim\n\nsee <!-- inline --> here\n"
    got = _decorations(tmp_path, [-1, note.index(stamp) + 5], note=note)

    assert ["hide", stamp] in got["-1"]
    assert ["hide", "<!-- inline -->"] in got["-1"]
    assert not any(r[1].startswith("> The claim") and r[0] == "hide" for r in got["-1"])
    assert ["hide", stamp] not in got[str(note.index(stamp) + 5)]
