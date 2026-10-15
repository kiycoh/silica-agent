# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A source is known by its path under the inbox, not by its bare name (ADR-0114).

Since convert keeps `CorsoA/Lezione 1.md` and `CorsoB/Lezione 1.md` apart, the
bare name stopped being an identity: the ledger gave one lecture's history to
the other, `/revert --source` of one deleted the other's notes, and the leaf of
one became the citation of the other. Replayed on a real 783-file study tree
(2026-10-15): 784 false drift notes, 95 false "modified" warnings and 151
sources whose revert reached another source's notes, all 0 when keyed by path.
"""
from __future__ import annotations

import hashlib
import json
import os
import types
from pathlib import Path

from silica.kernel.progress import RunManifestEntry
from silica.kernel.vault_manifest import archive_path_for, source_key
from silica.kernel.write import provenance as P
from silica.kernel.write.ops import InverseOp, InverseOpKind
from silica.kernel.write.undo_journal import UndoJournalStore, resolve_source, revert_source
from silica.router.states import finalize


def _vault() -> str:
    from silica.config import CONFIG
    return CONFIG.vault_path


def _entry(src: str, path: str, source_file: str = "", op: str = "write") -> RunManifestEntry:
    return RunManifestEntry(title=path, path=path, parent=None, cluster_id=-1,
                            source_basename=src, op=op, source_file=source_file)


def _fsm(entries, hashes, run_id="run-1", **kw):
    return types.SimpleNamespace(
        manifest=types.SimpleNamespace(entries=entries),
        progress=types.SimpleNamespace(run_id=run_id),
        _file_content_hashes=hashes, context={}, _run_inverses=[], **kw)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- the key ---

def test_key_is_the_path_under_the_inbox_and_the_archive_alike(tmp_vault):
    assert source_key("Inbox/lec.md") == "lec.md"            # a flat inbox keeps its keys
    assert source_key("Inbox/CorsoA/Lezione 1.md") == "CorsoA/Lezione 1.md"
    assert source_key("Done/CorsoA/Lezione 1.md") == "CorsoA/Lezione 1.md"
    assert source_key(str(Path(_vault()) / "Inbox/CorsoA/Lezione 1.md")) == "CorsoA/Lezione 1.md"
    assert source_key("Lezione 1.md") == "Lezione 1.md"
    assert archive_path_for("Inbox/CorsoA/Lezione 1.md") == "Done/CorsoA/Lezione 1.md"


# ------------------------------------------------------------- the ledger ---

def test_two_namesakes_are_two_histories(tmp_vault):
    v = _vault()
    for course, sha in (("CorsoA", "sha-a"), ("CorsoB", "sha-b")):
        P.append_record(f"{course}/Lezione 1.md", sha, f"run-{course}", [f"{course}/Note"], vault_path=v)

    assert P.drifted_notes(vault_path=v) == []
    assert P.check_renucleate("CorsoB/Lezione 1.md", "sha-b", vault_path=v) == (False, 0)
    assert P.check_renucleate("CorsoC/Lezione 1.md", "sha-c", vault_path=v) == (False, 0)
    assert P.sources_of("CorsoA/Note", vault_path=v) == ["CorsoA/Lezione 1.md"]
    # The patch executor holds only the bare name: namesakes count as one there.
    assert P.note_authored_by("CorsoB/Note", "Lezione 1.md", vault_path=v)


def test_a_real_edit_of_one_namesake_still_drifts(tmp_vault):
    v = _vault()
    P.append_record("CorsoA/Lezione 1.md", "a1", "r1", ["CorsoA/Old", "CorsoA/Kept"], vault_path=v)
    P.append_record("CorsoB/Lezione 1.md", "b1", "r2", ["CorsoB/Note"], vault_path=v)
    P.append_record("CorsoA/Lezione 1.md", "a2", "r3", ["CorsoA/Kept"], vault_path=v)

    assert P.drifted_notes(vault_path=v) == [("CorsoA/Old", "CorsoA/Lezione 1.md")]
    assert P.check_renucleate("CorsoA/Lezione 1.md", "a3", vault_path=v) == (True, 1)


def test_a_record_from_before_the_key_is_the_one_file_of_its_name(tmp_vault):
    v = _vault()
    tmp_vault.note("Done/CorsoA/Lezione 1.md", "A")
    P.append_record("Lezione 1.md", "a1", "r1", ["CorsoA/Note"], vault_path=v)   # legacy, bare
    P.append_record("CorsoA/Lezione 1.md", "a2", "r2", [], vault_path=v)

    # One history: the edit is seen, the legacy record's note drifts.
    assert P.drifted_notes(vault_path=v) == [("CorsoA/Note", "CorsoA/Lezione 1.md")]
    assert P.sources_of("CorsoA/Note", vault_path=v) == ["CorsoA/Lezione 1.md"]


def test_a_record_from_before_the_key_abstains_when_the_name_is_shared(tmp_vault):
    v = _vault()
    tmp_vault.note("Done/CorsoA/Lezione 1.md", "A")
    tmp_vault.note("Inbox/CorsoB/Lezione 1.md", "B")
    P.append_record("Lezione 1.md", "a1", "r1", ["CorsoA/Note"], vault_path=v)

    assert P.read_records("CorsoA/Lezione 1.md", vault_path=v) == []
    assert P.check_renucleate("CorsoB/Lezione 1.md", "b1", vault_path=v) == (False, 0)
    assert P.read_records("Lezione 1.md", vault_path=v)   # the bare key still reaches it


def test_citation_is_not_borrowed_from_a_namesake_chunk(tmp_vault):
    tmp_vault.note("Inbox/CorsoA/notes.md", "---\ndoi: 10.1/a\n---\nA\n")
    tmp_vault.note("Inbox/CorsoB/notes.md", "---\ndoi: 10.1/b\n---\nB\n")
    P._citation_memo.clear()
    assert P.citation_of("notes.md") == {}
    tmp_vault.note("Inbox/Solo/paper.md", "---\ndoi: 10.1/c\n---\nC\n")
    assert P.citation_of("paper.md") == {"doi": "10.1/c"}


# ---------------------------------------------------------------- CLEANUP ---

def test_cleanup_attributes_each_namesake_its_own_notes_in_one_run(tmp_vault):
    entries = [
        _entry("Lezione 1.md", "CorsoA/Limiti", "Inbox/CorsoA/Lezione 1.md"),
        _entry("Lezione 1.md", "CorsoB/Perceptron", "Inbox/CorsoB/Lezione 1.md"),
    ]
    fsm = _fsm(entries, ["sha-a", "sha-b"])
    finalize._record_provenance(fsm, 0, "Inbox/CorsoA/Lezione 1.md")
    finalize._record_provenance(fsm, 1, "Inbox/CorsoB/Lezione 1.md")

    recs = P.read_records(vault_path=_vault())
    assert [(r["source"], r["notes"]) for r in recs] == [
        ("CorsoA/Lezione 1.md", ["CorsoA/Limiti"]),
        ("CorsoB/Lezione 1.md", ["CorsoB/Perceptron"]),
    ]


def test_cleanup_without_the_field_falls_back_to_the_name(tmp_vault):
    fsm = _fsm([_entry("lec.md", "Concepts/A")], ["sha"])
    finalize._record_provenance(fsm, 0, "Inbox/lec.md")
    assert P.read_records("lec.md", vault_path=_vault())[0]["notes"] == ["Concepts/A"]


def test_each_namesake_gets_its_own_leaf(tmp_vault):
    v = Path(_vault())
    tmp_vault.note("Inbox/CorsoA/Lezione 1.md", "---\ndate: 2026-03-01\n---\nANALISI limiti\n")
    tmp_vault.note("Inbox/CorsoB/Lezione 1.md", "---\ndate: 2026-09-20\n---\nRETI perceptron\n")
    tmp_vault.note("CorsoA/Limiti.md", "# Limiti\nbody\n")
    tmp_vault.note("CorsoB/Perceptron.md", "# Perceptron\nbody\n")
    for course, note in (("CorsoA", "CorsoA/Limiti"), ("CorsoB", "CorsoB/Perceptron")):
        src = f"Inbox/{course}/Lezione 1.md"
        finalize._write_source_leaf(_fsm([_entry("Lezione 1.md", note, src)], [], keep_sources=True,
                                         seen_override=None), src)

    leaf_b = (v / "sources/CorsoB/Lezione 1.md").read_text(encoding="utf-8")
    assert "RETI perceptron" in leaf_b and "date: 2026-09-20" in leaf_b
    assert "source_id: CorsoB/Lezione 1.md" in leaf_b
    assert "[[sources/CorsoB/Lezione 1]]" in (v / "CorsoB/Perceptron.md").read_text(encoding="utf-8")
    assert "[[sources/CorsoA/Lezione 1]]" in (v / "CorsoA/Limiti.md").read_text(encoding="utf-8")
    assert not (v / "sources/Lezione 1.md").exists()


def test_a_flat_leaf_of_the_same_file_is_reused(tmp_vault):
    v = Path(_vault())
    tmp_vault.note("sources/Lezione 1.md", '---\nsource_id: Lezione 1.md\nsource_file: "/x/A.pdf"\n---\n\ntext\n')
    tmp_vault.note("Inbox/CorsoA/Lezione 1.md", '---\nsource_file: "/x/A.pdf"\n---\ntext\n')
    tmp_vault.note("CorsoA/Limiti.md", "# Limiti\nbody\n")
    src = "Inbox/CorsoA/Lezione 1.md"
    finalize._write_source_leaf(_fsm([_entry("Lezione 1.md", "CorsoA/Limiti", src)], [],
                                     keep_sources=True, seen_override=None), src)
    assert "[[sources/Lezione 1]]" in (v / "CorsoA/Limiti.md").read_text(encoding="utf-8")
    assert not (v / "sources/CorsoA/Lezione 1.md").exists()


# ----------------------------------------------------------------- revert ---

def _two_courses(tmp_vault, tmp_path):
    v = _vault()
    a = tmp_vault.note("CorsoA/Entropia.md", "from A")
    b = tmp_vault.note("CorsoB/Gradiente.md", "from B")
    store = UndoJournalStore(tmp_path / "j.db")
    for course, path, text in (("CorsoA", a, "from A"), ("CorsoB", b, "from B")):
        run = store.start_run(f"Inbox/{course}/Lezione 1.md", vault=v, ledger_run_id=f"fsm-{course}")
        store.record(run, InverseOp(kind=InverseOpKind.delete_created, path=path), post_hash=_h(text))
        P.append_record(f"{course}/Lezione 1.md", f"sha-{course}", f"fsm-{course}",
                        [f"{course}/{Path(path).stem}"], vault_path=v)
    return a, b, store


def test_revert_by_a_shared_name_touches_nothing_and_names_the_sources(tmp_vault, tmp_path):
    a, b, store = _two_courses(tmp_vault, tmp_path)
    res = revert_source("Lezione 1.md", vault=_vault(), store=store)
    assert res["ambiguous"] == ["CorsoA/Lezione 1.md", "CorsoB/Lezione 1.md"]
    assert os.path.exists(a) and os.path.exists(b)


def test_revert_by_key_or_by_path_reverts_only_that_source(tmp_vault, tmp_path):
    a, b, store = _two_courses(tmp_vault, tmp_path)
    assert resolve_source("Inbox/CorsoB/Lezione 1.md", vault=_vault())[0] == "CorsoB/Lezione 1.md"
    revert_source("CorsoB/Lezione 1.md", vault=_vault(), store=store)
    assert os.path.exists(a) and not os.path.exists(b)


def test_revert_source_takes_the_rest_of_the_line(monkeypatch):
    from silica import cli

    seen = []
    monkeypatch.setattr(cli, "_revert_by_source", lambda source, vault: seen.append(source) or True)
    cli._dc_revert([], raw_input="/revert --source CorsoA/Lezione 1.md")
    cli._dc_revert([], raw_input='/revert --source="Lezione 1.md"')
    assert seen == ["CorsoA/Lezione 1.md", "Lezione 1.md"]


# ------------------------------------------------------------------ dedup ---

def test_merge_relation_compares_keys(tmp_vault):
    from silica.capabilities.dedup import _pair_provenance

    P.append_record("CorsoA/Lezione 1.md", "a", "r1", ["Concepts/X"], vault_path=_vault())
    assert _pair_provenance("Concepts/X", "CorsoB/Lezione 1.md")["relation"] == "cross-source"
    assert _pair_provenance("Concepts/X", "CorsoA/Lezione 1.md")["relation"] == "same-source"
    assert _pair_provenance("Concepts/X", "Lezione 1.md")["relation"] == "same-source"


def test_ledger_file_stays_a_json_array(tmp_vault):
    P.append_record("CorsoA/Lezione 1.md", "a", "r1", ["N"], vault_path=_vault())
    raw = json.loads((Path(_vault()) / P.DEFAULT_PROVENANCE_FILENAME).read_text(encoding="utf-8"))
    assert raw[0]["source"] == "CorsoA/Lezione 1.md"
