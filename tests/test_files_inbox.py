"""silica_files answers for inbox folders even when the index cannot.

The note index used to skip the inbox, so `DRIVER.list_files("Inbox/x")` came
back empty by design. But the /nucleate folder fallback tells the agent to
resolve a folder argument with exactly that call: an empty result there is not
"the folder is empty", it is "the tool cannot see this folder", and the model
filled the gap by inventing filenames. The index now walks the inbox; these
cases pin the `list_inbox_files` fallback that still catches an empty listing.
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


def test_folder_of_pdfs_is_not_reported_as_empty():
    """A folder holding only unconverted files used to answer {"total": 0,
    "files": []} — the same payload as a folder that is not there, and the agent
    reported it as "the path does not exist". Images/ stays out: it is
    conversion output, not something to convert."""
    with patch("silica.tools.atomic.DRIVER") as drv, \
         patch("silica.kernel.vault_manifest.active_inbox_dir", return_value="Inbox"):
        drv.list_files.return_value = []
        drv.list_inbox_files.return_value = _refs(
            "Inbox/papers/2509.04664v1.pdf",
            "Inbox/papers/jaamas2000b.pdf",
            "Inbox/papers/Images/fig1.png",
        )
        res = silica_files("Inbox/papers/")
    assert res["total"] == 0
    assert res["unconverted"] == [
        "Inbox/papers/2509.04664v1.pdf",
        "Inbox/papers/jaamas2000b.pdf",
    ]
    assert "/convert" in res["hint"]


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


# --- source folders outside the inbox ---------------------------------------
#
# A theology library is nine folders of PDFs and one INDEX.md. Every one of
# them answered {"total": 0, "files": []} — the same payload as a folder that
# does not exist — because only inbox folders declared their unconverted files.

def test_a_source_folder_of_pdfs_is_not_an_empty_folder(tmp_vault):
    from pathlib import Path
    d = Path(tmp_vault.note("04-massoneria/Mackey_Encyclopedia_1914.pdf", "%PDF")).parent
    (d / "rite_of_memphis_1879.pdf").write_bytes(b"%PDF-1.4\n")
    (d / "cover.jpg").write_bytes(b"\xff\xd8\xff")

    res = silica_files("04-massoneria")

    assert res["unconverted_total"] == 3
    assert "04-massoneria/Mackey_Encyclopedia_1914.pdf" in res["unconverted"]
    assert "04-massoneria/cover.jpg" in res["unconverted"]
    assert "nucleate" in res["hint"]


def test_notes_in_a_source_folder_still_come_back_as_notes(tmp_vault):
    tmp_vault.note("04-massoneria/Mackey.md", "# Mackey\n")
    tmp_vault.note("04-massoneria/book.pdf", "%PDF")

    res = silica_files("04-massoneria")

    assert res["files"] == ["04-massoneria/Mackey.md"]
    assert res["unconverted"] == ["04-massoneria/book.pdf"]


# --- silica_exists ----------------------------------------------------------
#
# Asked to ingest a PDF sitting in the library, the agent answered "there's no
# file called Gospel-of-mary-magdelene.pdf anywhere in the vault — not in that
# folder, not in the inbox, nowhere." The file was there. silica_exists answers
# through read_note, which only opens markdown.

def test_exists_answers_for_a_non_markdown_vault_file(tmp_vault):
    from silica.tools.atomic import silica_exists

    tmp_vault.note("02-apocrifi/Gospel-of-mary.pdf", "%PDF-1.4\n")

    assert silica_exists("02-apocrifi/Gospel-of-mary.pdf") is True


def test_exists_is_still_false_for_a_missing_file(tmp_vault):
    from silica.tools.atomic import silica_exists

    assert silica_exists("02-apocrifi/not-here.pdf") is False


def test_exists_does_not_escape_the_vault(tmp_vault):
    from silica.tools.atomic import silica_exists

    assert silica_exists("../../etc/passwd") is False


def test_bare_listing_names_the_folders_that_hold_source_files(tmp_vault):
    """The agent's "list files" is a bare call. On a library of PDFs it showed
    only the notes Silica had written, so the agent concluded the user's own
    books were not in the vault. Names the folders, not the 764 files."""
    tmp_vault.note("silica/Concepts/Uriel.md", "# Uriel\n")
    tmp_vault.note("02-apocrifi/Enoch.pdf", "%PDF")
    tmp_vault.note("02-apocrifi/Mary.pdf", "%PDF")
    tmp_vault.note("04-massoneria/Mackey.pdf", "%PDF")

    res = silica_files("")

    assert res["source_folders"] == {"02-apocrifi": 2, "04-massoneria": 1}
    assert "files" in res  # notes still listed as before
