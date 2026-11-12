# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0159: the editor's formula and molecule vocabulary is facts the page can draw.
Every `\\ce` reads through the mhchem grammar, every formula through latex2mathml with
no command left as text, and every SMILES through the smiles-drawer the page ships."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from silica.kernel.text.mhchem import ce_tex
from silica.kernel.text.render import mathml
from tests.webassets import WEB

VOCAB = json.loads((WEB / "science-vocab.json").read_text(encoding="utf-8"))


def _names(e) -> list[str]:
    return [*e["en"], *e["it"]]


def test_each_entry_has_names_in_both_languages_and_one_display_name():
    seen: set[str] = set()
    for e in VOCAB["molecules"] + VOCAB["formulas"]:
        assert e["en"] and e["it"] and all(isinstance(n, str) and n.strip() for n in _names(e)), e
        assert e["en"][0] not in seen, e["en"][0]
        seen.add(e["en"][0])
    assert len(VOCAB["molecules"]) >= 100 and len(VOCAB["formulas"]) >= 50


@pytest.mark.parametrize("name", ["water", "acqua", "glucose", "glucosio", "sulfuric acid", "acido solforico",
                                  "quadratic formula", "formula quadratica", "pythagoras", "pitagora",
                                  "ideal gas", "gas perfetti"])
def test_the_words_people_type_find_an_entry(name):
    # the editor's match: accent-folded, lowercase, anywhere in a name
    assert any(name in n.lower() for e in VOCAB["molecules"] + VOCAB["formulas"] for n in _names(e)), name


@pytest.mark.parametrize("e", VOCAB["molecules"], ids=lambda e: e["en"][0])
def test_every_molecule_formula_reads_as_mhchem(e):
    assert ce_tex(e["ce"]) is not None, e["ce"]


@pytest.mark.parametrize("e", VOCAB["formulas"], ids=lambda e: e["en"][0])
def test_every_formula_converts_with_nothing_left_as_source(e):
    out = mathml(e["tex"], display=True)
    assert "math-err" not in out and "\\" not in out, e["tex"]


PARSE = """
const vm = require("vm"), fs = require("fs");
globalThis.window = globalThis; globalThis.document = { createElement: () => ({}) };
vm.runInThisContext(fs.readFileSync(process.argv[2], "utf8"));
const bad = [];
for (const m of JSON.parse(fs.readFileSync(process.argv[3], "utf8")).molecules) {
  let ok = false;
  SmilesDrawer.parse(m.smiles, () => { ok = true; }, () => {});
  if (!ok) bad.push(m.smiles);
}
console.log(JSON.stringify(bad));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run smiles-drawer")
def test_every_smiles_parses_in_the_drawer_the_page_ships(tmp_path):
    probe = tmp_path / "parse.js"
    probe.write_text(PARSE)
    run = subprocess.run(["node", str(probe), str(WEB / "smiles-drawer.min.js"), str(WEB / "science-vocab.json")],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-2000:]
    assert json.loads(run.stdout) == []
    # the parser does not pair ring bonds, so an unclosed ring would pass it: count them here
    for m in VOCAB["molecules"]:
        bare = "".join(part.split("]")[-1] for part in m["smiles"].split("["))  # atoms in brackets hold no ring bond
        for digit in set(c for c in bare if c.isdigit()):
            assert bare.count(digit) % 2 == 0, m["smiles"]
