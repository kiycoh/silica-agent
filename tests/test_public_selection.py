"""The Page's choice (ADR-0143, ADR-0151): vaults, notes, and what never goes out."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def vault(tmp_vault, tmp_path, monkeypatch):
    from silica.kernel.vault_manifest import reset_manifest_cache

    monkeypatch.delenv("SILICA_PUBLIC_HANDLE", raising=False)
    monkeypatch.setenv("SILICA_PUBLIC_DIR", str(tmp_path / "public"))
    reset_manifest_cache()
    tmp_vault.note("Memory/Spacing effect.md", "# Spacing effect\n\nSee [[Forgetting curve]].\n")
    tmp_vault.note("Memory/Forgetting curve.md", "Ebbinghaus.\n")
    tmp_vault.note("Memory/Working memory.md", "---\ntags: [memory]\n---\nDraft.\n")
    tmp_vault.note("Memory/Clipped.md", "---\nform: clip\nsource: web-fetch\n---\nSomeone else's.\n")
    tmp_vault.note("Courses/Exam plan June.md", "Private plan.\n")
    tmp_vault.note("sources/Spacing effect.md", "the verbatim book\n")
    tmp_vault.note("Inbox/lecture.md", "converted\n")
    tmp_vault.note(".silica-chat/s1/a.md", "attachment\n")
    yield tmp_vault
    reset_manifest_cache()


def _states():
    from silica.kernel.publish import selection

    return selection()["notes"]


def test_nothing_is_public_until_the_person_says_so(vault):
    from silica.kernel.publish import vaults

    assert vaults() == []
    assert {s for s in _states().values()} <= {"off", "never"}


def test_a_vault_takes_its_folders_notes_but_not_a_clip(vault):
    from silica.kernel.publish import add_vault

    v = add_vault("Memory")
    assert (v["slug"], v["title"], v["visibility"], v["license"]) == ("memory", "Memory", "off", "not stated")
    st = _states()
    assert st["Memory/Spacing effect.md"] == "on"
    assert st["Memory/Working memory.md"] == "on"
    assert st["Memory/Clipped.md"] == "off"
    assert st["Courses/Exam plan June.md"] == "off"


def test_a_note_outside_every_vault_never_goes_out_even_with_its_own_true(vault, tmp_path):
    from silica.kernel.publish import add_vault, set_note

    (Path(tmp_path) / "vault" / "Courses/Exam plan June.md").write_text(
        "---\npublish: true\n---\nPrivate plan.\n", encoding="utf-8")
    add_vault("Memory")
    assert _states()["Courses/Exam plan June.md"] == "off"
    with pytest.raises(ValueError, match="no wiki"):
        set_note("Courses/Exam plan June.md", True)


def test_silica_folders_are_never_published_and_cannot_be_vaults(vault):
    from silica.kernel.publish import add_vault, set_note

    st = _states()
    # sources/ is outside the notes walk already; whatever reaches it is never
    assert st.get("sources/Spacing effect.md", "never") == "never"
    assert st.get(".silica-chat/s1/a.md", "never") == "never"
    assert st["Inbox/lecture.md"] == "never"
    for folder in ("sources", "Inbox", ".silica-chat"):
        with pytest.raises(ValueError):
            add_vault(folder)
    with pytest.raises(ValueError):
        set_note("sources/Spacing effect.md", True)


def test_a_laptop_holds_twenty_vaults_and_none_inside_another(vault, tmp_path):
    from silica.kernel.publish import MAX_VAULTS, add_vault

    add_vault("Memory")
    (Path(tmp_path) / "vault" / "Memory" / "Sub").mkdir()
    with pytest.raises(ValueError, match="overlaps"):
        add_vault("Memory/Sub")
    with pytest.raises(ValueError, match="not a folder"):
        add_vault("Nope")
    for i in range(MAX_VAULTS - 1):
        (Path(tmp_path) / "vault" / f"F{i}").mkdir()
        add_vault(f"F{i}")
    (Path(tmp_path) / "vault" / "OneTooMany").mkdir()
    with pytest.raises(ValueError, match="20 wikis"):
        add_vault("OneTooMany")


def test_free_makes_three_and_a_downgrade_loses_none_it_had(vault, tmp_path, monkeypatch):
    from silica.kernel.publish import add_vault, update_vault, vaults

    for i in range(5):
        (Path(tmp_path) / "vault" / f"F{i}").mkdir()
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_VIEWS", "graph,public")
    for i in range(3):
        add_vault(f"F{i}")
    with pytest.raises(ValueError, match="3 wikis"):
        add_vault("F3")

    monkeypatch.setenv("SILICA_VIEWS", "all")
    add_vault("F3")
    add_vault("F4")
    monkeypatch.setenv("SILICA_VIEWS", "graph,public")
    with pytest.raises(ValueError, match="3 wikis"):
        add_vault("Memory")
    # a write rewrites every row it read: the fifth must come through it
    update_vault(vaults()[0]["slug"], {"title": "Renamed"})
    assert [v["folder"] for v in vaults()] == ["F0", "F1", "F2", "F3", "F4"]


def test_a_note_globe_strikes_it_from_its_vault_and_writes_the_least(vault, tmp_path):
    from silica.kernel.publish import add_vault, set_note

    add_vault("Memory")
    assert set_note("Memory/Working memory.md", False) == "struck"
    text = (Path(tmp_path) / "vault" / "Memory/Working memory.md").read_text(encoding="utf-8")
    assert "publish: false" in text and "True" not in text and "tags:" in text
    assert _states()["Memory/Working memory.md"] == "struck"

    # back on: the vault decides again, no key left
    assert set_note("Memory/Working memory.md", True) == "on"
    text = (Path(tmp_path) / "vault" / "Memory/Working memory.md").read_text(encoding="utf-8")
    assert "publish" not in text and "tags:" in text

    # a clip is published only by its own globe, and loses the key when off
    assert set_note("Memory/Clipped.md", True) == "on"
    assert "publish: true" in (Path(tmp_path) / "vault" / "Memory/Clipped.md").read_text(encoding="utf-8")
    assert set_note("Memory/Clipped.md", False) == "off"
    assert "publish" not in (Path(tmp_path) / "vault" / "Memory/Clipped.md").read_text(encoding="utf-8")


def test_a_copy_of_a_private_vault_has_no_globe(vault, tmp_path):
    from silica.kernel.publish import add_vault, set_note

    vault.note("Memory/From Sara.md", "---\nform: clip\nshared: private\nauthor: '@sara'\n---\nHers.\n")
    add_vault("Memory")
    assert _states()["Memory/From Sara.md"] == "never"
    with pytest.raises(ValueError, match="Private"):
        set_note("Memory/From Sara.md", True)


def test_the_public_block_keeps_the_rest_of_vault_yaml_and_replaces_adr_0143s_keys(vault, tmp_path):
    from silica.kernel.publish import update_vault, vaults

    manifest = Path(tmp_path) / "vault" / "vault.yaml"
    manifest.write_text("# my vault\nlanguage: Italian\npublic:\n  page: true\n  folders:\n"
                        "    - Courses\n    - Memory\nzotero:\n  library: x\n", encoding="utf-8")
    # read before any write: each old folder is a vault, public as the page was
    assert [(v["folder"], v["visibility"]) for v in vaults()] == [("Courses", "public"), ("Memory", "public")]
    update_vault("courses", {"title": "  Corsi ", "line": "Two  exams\n in June", "license": "CC BY 4.0"})
    text = manifest.read_text(encoding="utf-8")
    assert text.startswith("# my vault\nlanguage: Italian\npublic:\n")
    assert "folders" not in text and "page:" not in text and "    - Memory" not in text
    assert text.rstrip().endswith("zotero:\n  library: x")
    c = vaults()[0]
    assert (c["title"], c["line"], c["license"], c["slug"]) == ("Corsi", "Two exams in June", "CC BY 4.0", "courses")
    with pytest.raises(ValueError):
        update_vault("courses", {"license": "WTFPL"})
    # the slug was fixed when the vault was made: a new title keeps the address
    update_vault("courses", {"title": "Esami"})
    assert vaults()[0]["slug"] == "courses"


def test_readers_are_handles_and_never_the_author(vault, monkeypatch):
    from silica.kernel.publish import add_vault, update_vault

    monkeypatch.setenv("SILICA_PUBLIC_HANDLE", "marta")
    add_vault("Memory")
    v = update_vault("memory", {"readers": ["@Sara", "luca", "marta", "luca"]})
    assert v["readers"] == ["luca", "sara"]
    with pytest.raises(ValueError):
        update_vault("memory", {"readers": ["not a handle"]})


def test_off_moves_a_vault_aside_and_public_brings_it_back(vault, tmp_path):
    from silica.kernel.publish import add_vault, handle, set_visibility, site_root

    add_vault("Memory")
    root = site_root(handle())
    (root / "live" / "memory").mkdir(parents=True)
    (root / "live" / "memory" / "index.html").write_text("page", encoding="utf-8")
    set_visibility("memory", "off")
    assert not (root / "live" / "memory").exists() and (root / "paused" / "memory" / "index.html").is_file()
    set_visibility("memory", "public")
    assert (root / "live" / "memory" / "index.html").read_text(encoding="utf-8") == "page"


def test_private_is_a_paid_hosted_accounts_and_a_downgrade_turns_it_off(vault, monkeypatch, tmp_path):
    from silica.kernel.publish import (
        add_vault, enforce_tier, handle, private_allowed, set_visibility, site_root, vaults)

    add_vault("Memory")
    assert private_allowed() == "hosted"
    with pytest.raises(ValueError, match="hosted"):
        set_visibility("memory", "private")
    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_PUBLIC_HANDLE", "marta")
    monkeypatch.setenv("SILICA_VIEWS", "graph,public")
    assert private_allowed() == "paid"
    with pytest.raises(ValueError, match="paid"):
        set_visibility("memory", "private")
    monkeypatch.setenv("SILICA_VIEWS", "all")
    assert set_visibility("memory", "private")["visibility"] == "private"
    (site_root(handle()) / "private" / "memory").mkdir(parents=True)
    # the seat ends: the tier's line loses `all`
    monkeypatch.setenv("SILICA_VIEWS", "graph,public")
    assert enforce_tier() == ["memory"]
    v = vaults()[0]
    assert v["visibility"] == "off" and v["downgraded"]
    assert not (site_root(handle()) / "private" / "memory").exists()
    assert enforce_tier() == []


def test_the_server_gives_no_page_without_a_handle_from_the_host(vault, monkeypatch):
    from silica.kernel.publish import handle

    monkeypatch.setenv("SILICA_HOSTED", "1")
    assert handle() == ""
    monkeypatch.setenv("SILICA_PUBLIC_HANDLE", "Marta")
    assert handle() == "marta"


def test_a_slug_folds_accents(vault):
    from silica.kernel.publish import slugify

    assert slugify("Perché la memoria è fragile?") == "perche-la-memoria-e-fragile"


def test_the_plan_is_the_hosts_words_and_only_an_account_that_can_still_buy_it_gets_them(monkeypatch):
    """silica-agent names no plan (ADR-0140 point 3): the list is host.env's, and a
    paying account, or one on a host that sets none, has no window to open."""
    from silica.kernel import publish

    monkeypatch.setenv("SILICA_HOSTED", "1")
    monkeypatch.setenv("SILICA_PAID_PLAN", "Private wikis | 20 wikis instead of 3 |")
    monkeypatch.setenv("SILICA_VIEWS", "graph,public")
    assert publish.paid_plan() == ["Private wikis", "20 wikis instead of 3"]
    monkeypatch.setenv("SILICA_VIEWS", "all")
    assert publish.paid_plan() == []
    monkeypatch.setenv("SILICA_VIEWS", "graph,public")
    monkeypatch.delenv("SILICA_PAID_PLAN")
    assert publish.paid_plan() == []
