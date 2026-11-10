"""The one walk of a vault, and which walks stay wider on purpose."""
from __future__ import annotations

from pathlib import Path

import pytest

from silica.kernel.base import layout


def _vault(root: Path) -> Path:
    for rel in ("b.md", "a/x.md", "a/y/z.md", "c.md", "sources/leaf.md", "Sources/caps.md",
                ".hidden/n.md", ".dot.md", "a/.dot2.md", "private/p.md", "node_modules/m.md",
                "Inbox/i.md", "log.md", "GRAPH_REPORT.md"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
    (root / ".silicaignore").write_text("private\n", encoding="utf-8")
    (root / "folder.md").mkdir()
    (root / "a" / "pic.png").write_bytes(b"x")
    return root


def _rels(*args, **kw) -> list[str]:
    return [rel for rel, _p in layout.iter_notes(*args, **kw)]


def test_the_readers_notes_come_in_path_order(tmp_path):
    v = _vault(tmp_path)
    assert _rels(v) == ["GRAPH_REPORT.md", "Inbox/i.md", "Sources/caps.md", "a/x.md", "a/y/z.md",
                        "b.md", "c.md", "log.md"]
    assert _rels(v) == [p.relative_to(v).as_posix() for p in sorted(
        q for q in v.rglob("*.md") if q.is_file()
        and not any(s.startswith(".") for s in q.relative_to(v).parts)
        and not {"private", "node_modules", "sources"} & set(q.relative_to(v).parts))]


def test_the_path_comes_with_its_key(tmp_path):
    v = _vault(tmp_path)
    assert dict(layout.iter_notes(v))["a/y/z.md"] == v / "a/y/z.md"


def test_scope_vault_keeps_the_leaves(tmp_path):
    assert "sources/leaf.md" in _rels(_vault(tmp_path), scope="vault")


def test_ignore_false_sees_what_the_indexes_hide(tmp_path):
    rels = _rels(_vault(tmp_path), scope="vault", ignore=False)
    assert {"private/p.md", "node_modules/m.md"} <= set(rels)
    assert not any(r.startswith(".") or "/." in r for r in rels)


def test_under_starts_lower_and_a_hidden_folder_stays_hidden(tmp_path):
    v = _vault(tmp_path)
    assert _rels(v, under="a") == ["a/x.md", "a/y/z.md"]
    assert _rels(v, scope="vault", under=".hidden") == []
    assert _rels(v, under="missing") == []


def test_walk_tree_prunes_and_a_caller_may_prune_further(tmp_path):
    v = _vault(tmp_path)
    seen = []
    for dirpath, dirnames, _files in layout.walk_tree(v):
        dirnames[:] = [d for d in dirnames if d != "a"]
        seen.append(Path(dirpath).relative_to(v).as_posix())
    assert "a" not in seen and ".hidden" not in seen and "private" not in seen
    assert "Inbox" in seen


@pytest.mark.parametrize("walk", [
    lambda v: {r for r, _p in layout.iter_notes(v, scope="vault", ignore=False)},
])
def test_the_raw_walks_read_ignored_folders(tmp_path, walk):
    """check/pool, raws, the health digest and the daily notes read a name
    `.silicaignore` hides: widening any of them would change what it returns."""
    assert "private/p.md" in walk(_vault(tmp_path))


def test_the_check_pool_walk_sees_an_ignored_folder_and_no_hidden_file(tmp_path):
    from silica.kernel.check import pool

    notes, files = pool.walk(str(_vault(tmp_path)))
    assert "private/p.md" in notes and "pic.png" in files
    assert ".dot.md" not in notes and "a/.dot2.md" not in notes


def test_the_raws_walk_reads_a_dot_named_note(tmp_path):
    from silica.kernel import raws

    v = _vault(tmp_path)
    for rel in (".dot.md", "private/p.md"):
        (v / rel).write_text("---\nsource_file: x.pdf\n---\n", encoding="utf-8")
    assert {n for n, _f in raws.named(v)} == {".dot.md", "private/p.md"}


# --- a `write_dir` vault keeps its leaves under the write dir ------------------

@pytest.fixture
def confined(tmp_path, monkeypatch):
    from silica.config import CONFIG
    from silica.kernel.base import manifest

    (tmp_path / "vault.yaml").write_text("write_dir: silica\n", encoding="utf-8")
    for rel in ("A.md", "silica/Note.md", "silica/sources/Leaf.md", "sources/Old leaf.md"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("---\ndate: 2026-01-02\n---\n- [ ] a task 📅 2026-01-03\n", encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path), raising=False)
    monkeypatch.setattr(manifest, "_cached", None)
    return tmp_path


def test_a_composed_leaf_is_no_note_of_the_reader(confined):
    assert _rels(confined) == ["A.md", "silica/Note.md"]
    assert "silica/sources/Leaf.md" in _rels(confined, scope="vault")


def test_the_roots_are_read_off_the_vault_walked_and_not_the_active_one(confined, tmp_path_factory):
    other = tmp_path_factory.mktemp("other")
    (other / "vault.yaml").write_text("write_dir: mine\n", encoding="utf-8")
    for rel in ("silica/sources/Kept.md", "mine/sources/Gone.md"):
        p = other / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
    assert _rels(other) == ["silica/sources/Kept.md"]


def test_the_timeline_the_calendar_and_learn_leave_a_composed_leaf_out(confined):
    from silica.config import CONFIG  # noqa: F401
    from silica.kernel.calendar import tasks
    from silica.kernel.base import note_index
    from silica.kernel.recall.requires import written_requires
    from silica.kernel.calendar import timeline

    (confined / "silica/sources/Leaf.md").write_text(
        "---\nevent_start: 2026-02-02\nrequires: ['[[A]]']\n---\n- [ ] t 📅 2026-02-03\n", encoding="utf-8")
    assert "silica/sources/Leaf.md" not in note_index.meta(confined)
    assert "silica/sources/Leaf.md" not in {r[3] for r in timeline._scan(confined)[0]}
    assert written_requires(confined) == {}
    assert all(t.path != "silica/sources/Leaf.md" for t in tasks.scan_tasks(confined))


def test_collections_find_a_leaf_under_the_write_dir(confined):
    """Leaf links are composed with the write dir (finalize), so `[[sources/`
    alone found none on such a vault."""
    from silica.kernel.learn import sources

    (confined / "silica/sources/Book.md").write_text("---\nsource_file: book.pdf\n---\ntext\n", encoding="utf-8")
    (confined / "silica/Lesson.md").write_text(
        '---\nsection: "(outline)"\n---\n1. [[A]]: one\n\n## Sources\n- [[silica/sources/Book]]\n', encoding="utf-8")
    colls = sources.collections({"silica/Lesson.md": {"spine": True, "mtime": 1}}, {"silica/Lesson.md"})
    assert [c["leaves"] for c in colls] == [{"silica/sources/Book": "silica/Lesson.md"}]
    assert sources.note_collections(["silica/Lesson.md"], colls)["silica/Lesson.md"] == {colls[0]["key"]}
