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
