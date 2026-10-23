# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-of-run dangling-link sweep (silica/kernel/link/sweep.py).

Contract: after a nucleate run, a wikilink whose target never materialized is
unlinked back to plain text, and unresolved `related:` frontmatter entries are
dropped. Links that resolve are untouched; embeds and non-note targets are
never stripped.
"""
import pytest

from silica.driver.fs_backend import ObsidianFSBackend


@pytest.fixture
def vault(tmp_path, monkeypatch):
    backend = ObsidianFSBackend(str(tmp_path))
    monkeypatch.setattr("silica.driver.DRIVER", backend)
    return tmp_path


def _write(vault, rel, text):
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_dangling_link_is_unlinked_resolved_kept(vault):
    _write(vault, "Hub.md", "---\ntitle: Hub\n---\n\nHub body.\n")
    _write(
        vault, "A.md",
        "---\ntitle: A\n---\n\nSee [[Hub]] and [[Never Written]] for more.\n",
    )
    from silica.kernel.link.sweep import sweep_dangling_links

    summary = sweep_dangling_links(["A"])
    assert summary["links_stripped"] == 1
    text = (vault / "A.md").read_text(encoding="utf-8")
    assert "[[Hub]]" in text
    assert "[[Never Written]]" not in text
    assert "Never Written" in text  # display text survives


def test_alias_keeps_alias_text(vault):
    _write(vault, "A.md", "---\ntitle: A\n---\n\nPer [[Ghost Note|the alias]].\n")
    from silica.kernel.link.sweep import sweep_dangling_links

    sweep_dangling_links(["A"])
    text = (vault / "A.md").read_text(encoding="utf-8")
    assert "the alias" in text and "[[" not in text.split("---\n")[-1]


def test_related_frontmatter_pruned(vault):
    _write(vault, "Hub.md", "---\ntitle: Hub\n---\n\nHub.\n")
    _write(
        vault, "A.md",
        '---\nparent note: "[[Hub]]"\nrelated:\n  - "[[Hub]]"\n  - "[[Missing One]]"\n'
        "tags:\n  - t\n---\n\n[[Hub]] body.\n",
    )
    from silica.kernel.link.sweep import sweep_dangling_links

    summary = sweep_dangling_links(["A"])
    text = (vault / "A.md").read_text(encoding="utf-8")
    assert '"[[Hub]]"' in text
    assert "Missing One" not in text
    assert summary["notes_edited"] == 1


def test_embeds_and_files_untouched(vault):
    _write(
        vault, "A.md",
        "---\ntitle: A\n---\n\n![[diagram.png]] and [[paper.pdf]] stay.\n",
    )
    from silica.kernel.link.sweep import sweep_dangling_links

    summary = sweep_dangling_links(["A"])
    assert summary["links_stripped"] == 0
    text = (vault / "A.md").read_text(encoding="utf-8")
    assert "![[diagram.png]]" in text and "[[paper.pdf]]" in text


def test_untouched_note_not_rewritten(vault):
    _write(vault, "Hub.md", "---\ntitle: Hub\n---\n\nHub.\n")
    _write(vault, "A.md", "---\ntitle: A\n---\n\nOnly [[Hub]] here.\n")
    before = (vault / "A.md").stat().st_mtime_ns
    from silica.kernel.link.sweep import sweep_dangling_links

    summary = sweep_dangling_links(["A"])
    assert summary["notes_edited"] == 0
    assert (vault / "A.md").stat().st_mtime_ns == before


def test_path_form_links_resolve(vault):
    _write(vault, "Sub/Deep.md", "---\ntitle: Deep\n---\n\nBody.\n")
    _write(vault, "A.md", "---\ntitle: A\n---\n\nSee [[Sub/Deep]] here.\n")
    from silica.kernel.link.sweep import sweep_dangling_links

    summary = sweep_dangling_links(["A"])
    assert summary["links_stripped"] == 0
    assert "[[Sub/Deep]]" in (vault / "A.md").read_text(encoding="utf-8")


def test_related_single_quoted_entries_pruned(vault):
    _write(vault, "Hub.md", "---\ntitle: Hub\n---\n\nHub.\n")
    _write(
        vault, "A.md",
        "---\nparent note: \"[[Hub]]\"\nrelated:\n- '[[Hub]]'\n- '[[Ghost Entry]]'\n"
        "tags:\n  - t\n---\n\n[[Hub]] body.\n",
    )
    from silica.kernel.link.sweep import sweep_dangling_links

    sweep_dangling_links(["A"])
    text = (vault / "A.md").read_text(encoding="utf-8")
    assert "'[[Hub]]'" in text and "Ghost Entry" not in text


@pytest.mark.parametrize("entry", [
    # PyYAML's own dump of an apostrophe title: the quote is doubled.
    "- '[[Motivation for Bessel''s Correction]]'",
    # A spelling the fold relinks to the apostrophe title (typographic quote).
    "- '[[motivation for Bessel’s correction]]'",
    '  - "[[motivation for Bessel’s correction]]"',
])
def test_related_entry_with_an_apostrophe_keeps_the_frontmatter(vault, entry):
    """Seen 2026-10-21 nucleating a statistics lecture: the sweep wrote the
    resolved name into the single-quoted entry as it was, `'[[Motivation for
    Bessel's Correction]]'`, and the frontmatter stopped parsing."""
    import yaml

    _write(vault, "Motivation for Bessel's Correction.md", "---\ntitle: M\n---\n\nBody.\n")
    _write(vault, "A.md", f"---\ntitle: A\nrelated:\n{entry}\ntags:\n  - t\n---\n\nBody.\n")
    from silica.kernel.link.sweep import sweep_dangling_links

    sweep_dangling_links(["A"])
    fm = (vault / "A.md").read_text(encoding="utf-8").split("---\n")[1]
    assert yaml.safe_load(fm)["related"] == ["[[Motivation for Bessel's Correction]]"]


class TestPunctuationVariantsAreRelinked:
    """The run that writes `[[Ahura Mazda]]` in one segment writes
    `Ahura-Mazda.md` in another: the note is there, the spelling drifted. Exact
    resolution called that dangling and deleted the edge (7 of the 151 links
    the sweep stripped on one book, measured 2026-08-16). The fold is exact
    after dropping case, punctuation and accents — never a ratio."""

    def test_hyphen_variant_is_relinked_not_stripped(self, vault):
        _write(vault, "Ahura-Mazda.md", "---\ntitle: Ahura-Mazda\n---\n\nBody.\n")
        _write(vault, "A.md", "---\ntitle: A\n---\n\nThe law of [[Ahura Mazda]].\n")
        from silica.kernel.link.sweep import sweep_dangling_links

        summary = sweep_dangling_links(["A"])

        text = (vault / "A.md").read_text(encoding="utf-8")
        assert "[[Ahura-Mazda|Ahura Mazda]]" in text
        assert summary["links_stripped"] == 0
        assert summary["links_relinked"] == 1

    def test_an_alias_keeps_its_display_text(self, vault):
        _write(vault, "Amesha-Spentas.md", "---\ntitle: x\n---\n\nBody.\n")
        _write(vault, "A.md", "---\ntitle: A\n---\n\nThe [[Amesha Spentas|holy ones]].\n")
        from silica.kernel.link.sweep import sweep_dangling_links

        sweep_dangling_links(["A"])

        assert "[[Amesha-Spentas|holy ones]]" in (vault / "A.md").read_text()

    def test_related_entry_is_repointed_not_dropped(self, vault):
        _write(vault, "Ahura-Mazda.md", "---\ntitle: x\n---\n\nBody.\n")
        _write(
            vault, "A.md",
            '---\ntitle: A\nrelated:\n  - "[[Ahura Mazda]]"\n---\n\nBody.\n',
        )
        from silica.kernel.link.sweep import sweep_dangling_links

        sweep_dangling_links(["A"])

        text = (vault / "A.md").read_text(encoding="utf-8")
        assert '- "[[Ahura-Mazda]]"' in text

    def test_a_name_that_only_looks_similar_is_still_stripped(self, vault):
        """Folding is not fuzzy matching: a different name stays dangling."""
        _write(vault, "Zoroastrian Philosophy.md", "---\ntitle: x\n---\n\nBody.\n")
        _write(
            vault, "A.md",
            "---\ntitle: A\n---\n\nOn [[Zoroastrian philosophy and ethics]].\n",
        )
        from silica.kernel.link.sweep import sweep_dangling_links

        summary = sweep_dangling_links(["A"])

        assert summary["links_stripped"] == 1
        assert "[[" not in (vault / "A.md").read_text().split("---\n")[-1]


def test_the_next_run_sweeps_what_an_interrupted_run_landed(tmp_vault, monkeypatch, tmp_path):
    """2026-10-12: a 14-lecture run stopped by Ctrl+C after Lezione 5 left 34
    links to ideas that never landed; its sweep never ran and the resumed run
    swept only the notes it wrote itself."""
    import time

    import silica.kernel.progress as prog
    from silica.kernel.write.provenance import append_record
    from silica.router.coordinator import Coordinator

    monkeypatch.setattr(prog, "_RUNS_DIR", tmp_path / "runs")
    tmp_vault.note("T/A.md", "---\nAI: true\n---\n\nSee [[Never Written]] here.\n")
    append_record("Lezione 1.md", "sha-1", "stopped-run", ["T/A"])
    marker = prog.run_dir_for("stopped-run") / "interrupted"
    marker.parent.mkdir(parents=True)
    marker.touch()
    foreign = prog.run_dir_for("other-vault-run") / "interrupted"
    foreign.parent.mkdir(parents=True)
    foreign.touch()

    coord = Coordinator(inbox_files=["Inbox/x.md"], target_dir="T")
    coord._run_started = time.time()
    coord._sweep_dangling_links({})

    from pathlib import Path
    from silica.config import CONFIG
    text = (Path(CONFIG.vault_path) / "T/A.md").read_text(encoding="utf-8")
    assert "[[Never Written]]" not in text and "Never Written" in text
    assert not marker.exists()
    assert foreign.exists()   # no record of it in this vault: not this vault's to clear


def test_a_run_ends_by_reconciling_the_indexes_after_its_last_edit(tmp_vault, tmp_path, monkeypatch):
    """The WRITE refresh leaves patches to this sweep, and the passes after
    WRITE (sub-agent repairs, the dangling-link sweep) edit notes too: without
    it the next chat question paid their re-embedding on the read path."""
    import silica.kernel.progress as prog
    import silica.kernel.recall.sync as sync
    from silica.router.coordinator import Coordinator

    monkeypatch.setattr(prog, "_RUNS_DIR", tmp_path / "runs")
    order: list[str] = []
    coord = Coordinator(inbox_files=["Inbox/x.md"], target_dir="T")
    monkeypatch.setattr(coord.fsm, "run", lambda: order.append("fsm") or {"final_status": "success"})
    monkeypatch.setattr(coord, "_sweep_dangling_links", lambda result: order.append("links"))
    monkeypatch.setattr(sync, "sweep", lambda *, force=False: order.append(f"sweep force={force}"))

    coord.run()
    assert order == ["fsm", "links", "sweep force=True"]
