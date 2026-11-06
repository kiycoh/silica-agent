# SPDX-License-Identifier: AGPL-3.0-or-later

"""A run interrupted before the typed run state resumes after it.

tests/fixtures/old_run is a real run, not a hand-written one: the code at
009e974, the last before the phase table, stopped by Ctrl+C in its second
chunk with the first one committed. <HOME> and <VAULT> stand for the paths it
ran under. Nothing of the in-memory state survives a stop, so what has to hold
is the disk: the ledgers keep their shape, the task ids still match, and the
content-addressed checkpoint still skips the chunk the old run paid for.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from silica.config import silica_home
from silica.kernel.progress import run_dir_for
from silica.router.orchestrator import InjectorFSM
from silica.router.phases import InjectorState

FIXTURE = Path(__file__).parent / "fixtures" / "old_run"
RUN_ID = "ac58c2470758451a8a8f61553d62f2d4"
CONCEPTS = [f"Step {c}" for c in "ABCDEFGHIJ"]


class _WordHashEmbedder:
    """The embedder of the run that wrote the fixture: the checkpoint key hashes
    the chunk after the near-duplicate collapse, so the vectors have to agree."""
    def embed(self, texts):
        out = []
        for text in texts:
            vec = [0.0] * 64
            for word in re.findall(r"\w+", text.lower()):
                vec[int(hashlib.sha256(word.encode()).hexdigest(), 16) % 64] += 1.0
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            out.append([x / norm for x in vec])
        return out


def _distill(payload, target, **_):
    names = [c["name"] for b in payload["batches"] for c in b["concepts"]]
    src = payload["batches"][0]["inbox_file"].rsplit("/", 1)[-1]
    body = "The source develops this idea with care and detail. " * 8
    return {"updates": [{"op": "write", "path": f"{target}/{n}.md", "heading": n,
                         "source_basename": src, "snippet": f"{n}. {body}"} for n in names]}


@pytest.fixture
def old_run(tmp_vault, monkeypatch):
    from silica.config import CONFIG

    monkeypatch.setenv("SILICA_MIN_WRITE_SNIPPET_CHARS", "100")
    vault = Path(CONFIG.vault_path)
    shutil.copytree(FIXTURE / "vault", vault, dirs_exist_ok=True)
    # Asked of the code that reads it, not derived from the home: a run's
    # checkpoint path is absolute, and the run has to find it where it looks.
    run_dir = run_dir_for(RUN_ID)
    for src in (FIXTURE / "run").rglob("*"):
        if src.is_file():
            dst = run_dir / src.relative_to(FIXTURE / "run")
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(src.read_text(encoding="utf-8").replace("<VAULT>", str(vault))
                           .replace("<HOME>/runs", str(run_dir.parent))
                           .replace("<HOME>", str(silica_home())), encoding="utf-8")
    yield run_dir
    shutil.rmtree(run_dir, ignore_errors=True)


def test_the_new_code_loads_what_the_old_code_wrote(old_run):
    before = (old_run / "task_ledger.json").read_bytes()

    fsm = InjectorFSM("Inbox/r.md", "ML", resume_run_id=RUN_ID)

    assert fsm.progress.run_id == RUN_ID
    assert (old_run / "task_ledger.json").read_bytes() == before
    # The plan a fresh run writes today is the plan the old run wrote.
    fresh = InjectorFSM("Inbox/r.md", "ML")
    assert fresh.task_ledger.checkpoints == fsm.task_ledger.checkpoints
    # And so are the ids the chunk phases are filed under.
    stored = {t.id for t in fsm.progress.tasks if t.id.startswith("f0_c0_")}
    assert stored == {f"f0_c0_{p.id}" for p in InjectorState.phases() if p.scope == "chunk"}


def test_the_chunk_the_old_run_paid_for_is_not_distilled_again(old_run):
    distilled: list[list[str]] = []

    def distill(payload, target, **kw):
        distilled.append([c["name"] for b in payload["batches"] for c in b["concepts"]])
        return _distill(payload, target, **kw)

    recon = {"file": "Inbox/r.md", "collisions": [], "new_concepts": CONCEPTS}
    with patch("silica.router.states.setup.recon_file", return_value=recon), \
         patch("silica.router.states.distill.run_distiller", side_effect=distill), \
         patch("silica.router.states.distill.CONFIG.distill_concurrency", 1), \
         patch("silica.model.providers.get_embedder", return_value=_WordHashEmbedder()):
        fsm = InjectorFSM("Inbox/r.md", "ML", resume_run_id=RUN_ID)
        from silica.kernel.recall.embed import get_store, refresh_note
        refresh_note(_WordHashEmbedder(), "ML/Unrelated", "Unrelated",
                     (Path(fsm.progress.vault) / "ML/Unrelated.md").read_text(encoding="utf-8"),
                     store=get_store(), save=True)
        result = fsm.run()

    assert fsm.state == InjectorState.DONE
    assert result["committed_chunks"] == 2
    # One call, for the second chunk: the first came back from its checkpoint.
    assert len(distilled) == 1 and "Step I" in distilled[0] and "Step A" not in distilled[0]
    ledger = json.loads((old_run / "ledger.json").read_text(encoding="utf-8"))
    assert {t["id"]: t["status"] for t in ledger["tasks"]}["f0_c1_cleanup"] == "done"
