"""silica_files answers for inbox folders too.

The note index skips the inbox on purpose (inbox drafts must not reach the
graph, embeddings or co-occurrence), so `DRIVER.list_files("Inbox/x")` is
always empty by design. But the /nucleate folder fallback tells the agent to
resolve a folder argument with exactly that call: an empty result there is not
"the folder is empty", it is "the tool cannot see this folder", and the model
filled the gap by inventing filenames.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from silica.tools.atomic import silica_files


def _refs(*paths):
    return [SimpleNamespace(name=p.rsplit("/", 1)[-1].removesuffix(".md"), path=p) for p in paths]


_INBOX = _refs(
    "Inbox/machine_learning/Lezione 1.md",
    "Inbox/machine_learning/Lezione 2.md",
    "Inbox/machine_learning/Lezione 8.md",
    "Inbox/machine_learning/Images/a1b2.jpg",
    "Inbox/machine_learning/paper.pdf",
    "Inbox/other/Nota.md",
    "Inbox/root.md",
)


def test_inbox_folder_lists_its_notes():
    with patch("silica.tools.atomic.DRIVER") as drv, \
         patch("silica.kernel.vault_manifest.active_inbox_dir", return_value="Inbox"):
        drv.list_files.return_value = []
        drv.list_inbox_files.return_value = _INBOX
        res = silica_files("Inbox/machine_learning")
    assert res["total"] == 3
    assert res["files"] == [
        "Inbox/machine_learning/Lezione 1.md",
        "Inbox/machine_learning/Lezione 2.md",
        "Inbox/machine_learning/Lezione 8.md",
    ]


def test_inbox_root_lists_every_nested_note():
    with patch("silica.tools.atomic.DRIVER") as drv, \
         patch("silica.kernel.vault_manifest.active_inbox_dir", return_value="Inbox"):
        drv.list_files.return_value = []
        drv.list_inbox_files.return_value = _INBOX
        res = silica_files("Inbox")
    assert res["total"] == 5


def test_listing_is_natural_sorted():
    """os.walk order is neither stable nor meaningful, and the injector ingests a
    folder in listing order: lesson 10 must not land before lesson 2 defines its
    terms. Plain lexicographic sort would do exactly that."""
    with patch("silica.tools.atomic.DRIVER") as drv, \
         patch("silica.kernel.vault_manifest.active_inbox_dir", return_value="Inbox"):
        drv.list_files.return_value = []
        drv.list_inbox_files.return_value = _refs(
            "Inbox/ml/Lezione 10.md",
            "Inbox/ml/Lezione 2.md",
            "Inbox/ml/Lezione 1.md",
        )
        res = silica_files("Inbox/ml")
    assert res["files"] == [
        "Inbox/ml/Lezione 1.md",
        "Inbox/ml/Lezione 2.md",
        "Inbox/ml/Lezione 10.md",
    ]


def test_non_inbox_folder_does_not_consult_the_inbox():
    """An empty vault folder stays empty — the fallback is scoped to the inbox,
    not a second listing every miss falls through to."""
    with patch("silica.tools.atomic.DRIVER") as drv, \
         patch("silica.kernel.vault_manifest.active_inbox_dir", return_value="Inbox"):
        drv.list_files.return_value = []
        drv.list_inbox_files.return_value = _INBOX
        res = silica_files("Informatica/Vuota")
    assert res["total"] == 0
    drv.list_inbox_files.assert_not_called()
