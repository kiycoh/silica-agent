# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A name several notes share is never resolved silently (ADR-0114).

Reads by bare name took the first note the index met, the filesystem's order:
copying the drug vault from ext4 to tmpfs changed the note read for 30 of its
31 shared names. In a live A/B the model asked for `Proprietà farmacocinetiche`
about the Lanoxin and read the Mepral's in 4 of 16 runs, and in none once the
read listed the candidates instead.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from silica.kernel.write.ops import Op, OpType


def _vault() -> Path:
    from silica.config import CONFIG
    return Path(CONFIG.vault_path)


# --------------------------------------------------------- lever 3: order ---

def test_a_bare_name_reads_the_note_a_link_to_it_opens(tmp_vault):
    from silica.driver.fs_backend import ObsidianFSBackend

    tmp_vault.note("Zeta/Deep/Controindicazioni.md", "deep")
    tmp_vault.note("Farmaci/Mepral/Controindicazioni.md", "mepral")
    tmp_vault.note("Farmaci/Aldactone/Controindicazioni.md", "aldactone")
    d = ObsidianFSBackend(str(_vault()))
    d._ensure_index()
    # Whatever order the walk met them in, the pick is the link resolver's.
    for refs in (d._notes_by_name["controindicazioni"], list(reversed(d._notes_by_name["controindicazioni"]))):
        d._notes_by_name["controindicazioni"] = refs
        assert d.read_note("Controindicazioni").content == "aldactone"
        assert d._resolve_target("Controindicazioni").path == "Farmaci/Aldactone/Controindicazioni.md"
    assert d.notes_named("Controindicazioni") == [
        "Farmaci/Aldactone/Controindicazioni.md", "Farmaci/Mepral/Controindicazioni.md",
        "Zeta/Deep/Controindicazioni.md"]


# ------------------------------------------------------- lever 4: abstain ---

def test_read_note_names_the_candidates_instead_of_picking(tmp_vault):
    from silica.tools.atomic import silica_read_note

    tmp_vault.note("Farmaci/Mepral/Sovradosaggio.md", "omeprazolo")
    tmp_vault.note("Farmaci/Zoloft/Sovradosaggio.md", "sertralina")
    out = silica_read_note(name="Sovradosaggio")
    assert out.startswith("Nothing read: 2 notes are named 'Sovradosaggio'")
    assert "Farmaci/Mepral/Sovradosaggio; Farmaci/Zoloft/Sovradosaggio" in out
    assert "sertralina" not in out and "omeprazolo" not in out
    assert "sertralina" in silica_read_note(name="Farmaci/Zoloft/Sovradosaggio")


def test_a_name_one_note_carries_reads_as_before(tmp_vault):
    from silica.tools.atomic import silica_read_note

    tmp_vault.note("Farmaci/Zoloft/Sindrome serotoninergica.md", "body")
    assert silica_read_note(name="Sindrome serotoninergica").endswith("body")


# ------------------------------------------------ lever 6: report, keep ---

def test_validate_reports_a_link_a_shared_name_sends_elsewhere(tmp_vault):
    from silica.kernel.write.validate import _record_ambiguous_links

    tmp_vault.note("Farmaci/Brufen/Gravidanza e allattamento.md", "b")
    tmp_vault.note("Farmaci/Klacid/Gravidanza e allattamento.md", "k")
    tmp_vault.note("Farmaci/Zocor/Note.md", "z")
    ops = [
        Op(op=OpType.patch, path="Farmaci/Zocor/Note.md", heading="H",
           snippet="vedi [[Gravidanza e allattamento|gravidanza]] e [[Note]]", source_basename="s.md"),
        Op(op=OpType.patch, path="Farmaci/Klacid/Altro.md", heading="H",
           snippet="vedi [[Gravidanza e allattamento]]", source_basename="s.md"),
    ]
    out: list = []
    _record_ambiguous_links(ops, out)
    assert out == [{"link": "Gravidanza e allattamento", "note_path": "Farmaci/Zocor/Note.md",
                    "resolves_to": "Farmaci/Brufen/Gravidanza e allattamento.md",
                    "candidates": ["Farmaci/Brufen/Gravidanza e allattamento.md",
                                   "Farmaci/Klacid/Gravidanza e allattamento.md"]}]
    assert "[[Gravidanza e allattamento|gravidanza]]" in ops[0].snippet   # kept as written


# ------------------------------------------------- lever 7: Java calls ---

def _java_repo(root: Path, files: dict[str, str]) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)


def test_java_call_links_only_to_a_unique_first_party_definition(tmp_path):
    from silica.kernel.code import codegraph

    _java_repo(tmp_path, {
        "src/Player.java": "class Player { void pickUpItem() {} boolean isEmpty() { return true; } }",
        "src/Enemy.java": "class Enemy { void attack() {} }",
        "src/Boss.java": "class Boss { void attack() {} }",
        "src/Game.java": ("class Game { void tick(Player p, Enemy e, java.util.List l) {"
                          " p.pickUpItem(); e.attack(); l.isEmpty(); } }"),
    })
    calls = codegraph.build_codegraph(tmp_path).files["src/Game.java"]["calls"]
    assert {"target": "src/Player.java", "callee": "Player.pickUpItem", "caller": "Game"} in calls
    assert not any(c["callee"].endswith("attack") for c in calls)    # two definitions: no link
    assert not any(c["callee"].endswith("isEmpty") for c in calls)   # a JDK name: no link


# --------------------------------------------- lever 5: hub link target ---

def test_a_hub_bullet_names_the_folder_only_when_the_name_is_shared(tmp_vault):
    from silica.kernel.write.moc import moc_line

    tmp_vault.note("Corsi/Machine Learning/Lezione 24.md", "outline")
    tmp_vault.note("sources/Lezione 24.md", "leaf")
    tmp_vault.note("Corsi/Machine Learning/Pooling.md", "p")
    assert (moc_line("Corsi/Machine Learning/Lezione 24.md", "Reti convolutive")
            == "- [[Corsi/Machine Learning/Lezione 24|Lezione 24]] — Reti convolutive")
    assert moc_line("Corsi/Machine Learning/Pooling.md") == "- [[Pooling]]"
