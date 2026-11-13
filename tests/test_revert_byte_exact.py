"""A write taken back leaves the note's bytes as they were before it, whichever
way it is taken back: Examine's revert of an accepted batch, /revert, a bare
/undo. A browser pass on 2026-11-13 found `type: Note` left behind by all three:
the restore went through the write seam, which stamps the OKF type on any note
whose frontmatter has none."""
from __future__ import annotations

import pytest

from silica.config import CONFIG
from tests.run_command import direct

BEFORE = {
    "lf": b"---\ntitle: Galileo\n---\n\n# Galileo\n\nTried in 1633.\n",
    "crlf": b"---\r\ntitle: Galileo\r\n---\r\n\r\n# Galileo\r\n\r\nTried in 1633.\r\n",
    "no final newline": b"---\ntitle: Galileo\n---\n\n# Galileo\n\nTried in 1633.",
}


@pytest.fixture
def vault(tmp_path, monkeypatch):
    import silica.driver
    import silica.kernel.write.checkpoints as cp
    import silica.kernel.write.undo_journal as uj

    v = tmp_path / "v"
    (v / "Science").mkdir(parents=True)
    (v / "vault.yaml").write_text("write_dir: ''\n", encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(v))
    monkeypatch.setattr(silica.driver, "_driver", None)
    monkeypatch.setattr(cp, "_store", cp.CheckpointStore(tmp_path / "cp.db"))
    monkeypatch.setattr(uj, "_store", uj.UndoJournalStore(tmp_path / "uj.db"))
    yield v
    silica.driver._driver = None


def _note(vault, raw: bytes):
    path = vault / "Science" / "Galileo.md"
    path.write_bytes(raw)
    return path


def _added(text: str) -> str:
    return text.rstrip("\n") + "\n\nThe trial ended in house arrest.\n"


def _examine(vault, path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.kernel import review
    from silica.kernel.review import Kind, Proposal
    from silica.kernel.write import gate
    from silica.ui.web import server

    def apply(_vault, props):
        for p in props:
            gate.journaled_write(p.notes[0], _added, source="review", run=gate.current_run() or "")
        return {p.id: None for p in props}

    kinds = {**review.KINDS, "toy": Kind("toy", "toy", pending=lambda _v: [], apply=apply)}
    prop = Proposal(id="toy-1", kind="toy", title="Add the verdict", why="a test", notes=("Science/Galileo.md",))
    try:
        review.KINDS.clear()
        review.KINDS.update(kinds)
        landed = review.accept(str(vault), [prop])
        assert landed["run"] and b"house arrest" in path.read_bytes()
        said = TestClient(server.app).post("/metrics/review/revert", json={"run": landed["run"]}).json()
        assert said["restored"] == ["Science/Galileo.md"]
    finally:
        review.KINDS.clear()
        review.KINDS.update({k: v for k, v in kinds.items() if k != "toy"})


def _revert(vault, path):
    from silica.kernel.write import gate

    assert gate.journaled_write("Science/Galileo.md", _added, source="chat")
    assert b"house arrest" in path.read_bytes()
    assert direct("/revert", []) is True


def _undo(vault, path):
    from silica.tools.notes import silica_patch_note

    silica_patch_note("Galileo", "Verdict", "The trial ended in house arrest.", "chat")
    assert b"house arrest" in path.read_bytes()
    assert direct("/undo", []) is True


@pytest.mark.parametrize("take_back", [_examine, _revert, _undo], ids=["examine", "revert", "undo"])
@pytest.mark.parametrize("shape", sorted(BEFORE))
def test_a_write_taken_back_leaves_the_bytes_it_found(vault, take_back, shape):
    path = _note(vault, BEFORE[shape])
    take_back(vault, path)
    assert path.read_bytes() == BEFORE[shape]
