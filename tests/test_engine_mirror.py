"""silica-core reads six of this tree's keys through a CONFIG of its own, read once
from the environment with its own defaults. The engine mirrors all six into it at
every call (silica/kernel/recall/engine.py, MIRRORED), so the model, the endpoint
and the prefixes core embeds with are this tree's, at start and after a change in
Settings (entry-sources re-audit, 2026-11-10, Worth fixing 1)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import fields

from silica.kernel.recall.engine import MIRRORED

_PROBE = """
import json
import silica.sources.pdf_extract  # hands the PDF reader to the engine
from silica.config import CONFIG
from silica.kernel.recall.engine import MIRRORED, engine
from silica_core.config import CONFIG as CORE

def pairs():
    return [[getattr(CONFIG, ours), getattr(CORE, theirs)] for ours, theirs in MIRRORED]

engine()
start = pairs()
CONFIG.embedding_model = "jina-embeddings-v5-text-small"   # what the panel assigns
CONFIG.embedding_base_url = "http://127.0.0.1:9/v1"
engine()
print(json.dumps({"start": start, "changed": pairs()}))
"""


def test_every_embedder_field_core_has_is_mirrored():
    from silica_core.config import SilicaConfig as CoreConfig

    shared = {f.name for f in fields(CoreConfig) if f.name.startswith("embedding_")} | {"vault_path"}
    assert sorted(theirs for _ours, theirs in MIRRORED) == sorted(shared)


def test_after_engine_core_embeds_as_this_tree_does(tmp_path):
    # A fresh process: core's CONFIG is read from the environment at its first
    # import, so an unset embedder there is core's own default, not this tree's.
    env = {k: v for k, v in os.environ.items() if not k.startswith("SILICA_")}
    env.update(HOME=str(tmp_path), SILICA_HOME=str(tmp_path / ".silica"), SILICA_VAULT=str(tmp_path))
    out = subprocess.run([sys.executable, "-c", _PROBE], env=env, cwd=tmp_path,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    seen = json.loads(out.stdout.splitlines()[-1])
    for ours, theirs in seen["start"] + seen["changed"]:
        assert ours == theirs
    assert seen["changed"][1] == ["jina-embeddings-v5-text-small"] * 2
