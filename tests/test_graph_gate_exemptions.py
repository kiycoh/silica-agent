"""Graph regression gate: staging blindness and txn-wide exemption.

The 2026-08-21 machine_learning run lost 6 chunks to gate rollbacks whose
"regressions" the chunk never introduced: pre-existing dangling links in
notes that only post-WRITE phases (HUB_UPDATE/AUTOLINK/BACKLINK) touched,
intra-note anchors, and `## Sources` backlinks into an inbox staging file
racing its own archive.
"""
from types import SimpleNamespace

from silica.driver.base import GraphSnapshot, Link, NoteRef

from silica.kernel.graph_diff import check_graph_regression
from silica.router.states.finalize import txn_touched_paths


def test_rule3_ignores_inbox_and_done_backlinks():
    # 22 -> 0 on an inbox source (its Sources pointers) must not block...
    pre = GraphSnapshot(
        backlink_counts={"Inbox/machine_learning/Lezione 4.md": 22,
                         "done/Lezione 0.md": 9},
    )
    post = GraphSnapshot(
        backlink_counts={"Inbox/machine_learning/Lezione 4.md": 0,
                         "done/Lezione 0.md": 0},
    )
    ok, errors = check_graph_regression(pre, post, [], frozenset(), frozenset())
    assert ok, errors

    # ...while the same drop on a real vault note still does.
    pre = GraphSnapshot(backlink_counts={"notes/Hub.md": 22})
    post = GraphSnapshot(backlink_counts={"notes/Hub.md": 0})
    ok, errors = check_graph_regression(pre, post, [], frozenset(), frozenset())
    assert not ok
    assert any("Broken backlinks" in e for e in errors)


def test_rule2_exempts_txn_touched_sources():
    # The hub carries a pre-existing dangling link; only HUB_UPDATE touched
    # it (no op patched it). With the hub's path in patched_paths — now fed
    # from the txn — the gate must not roll the chunk back.
    hub = NoteRef(name="Machine learning", path="silica/AI/Machine learning.md")
    pre = GraphSnapshot(
        unresolved=[],
        link_counts={"silica/AI/Machine learning.md": 3},
    )
    post = GraphSnapshot(
        unresolved=[Link(source=hub, target="Rete convolutiva")],
        link_counts={"silica/AI/Machine learning.md": 4},
    )
    blocked, errors = check_graph_regression(pre, post, [], frozenset(), frozenset())
    assert not blocked and any("unresolved" in e for e in errors)

    ok, errors = check_graph_regression(
        pre, post, [], frozenset(),
        frozenset({"silica/AI/Machine learning.md"}))
    assert ok, errors


def test_txn_touched_paths_reads_inverses_and_dict_fallback():
    txn = SimpleNamespace(inverses=[
        SimpleNamespace(path="silica/AI/Machine learning.md", to_path=None),
        {"path": "notes/A.md", "to_path": "notes/B.md"},
        SimpleNamespace(path=None, to_path=None),
    ])
    assert txn_touched_paths(txn) == {
        "silica/AI/Machine learning.md", "notes/A.md", "notes/B.md"}
    assert txn_touched_paths(None) == set()
