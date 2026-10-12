# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The retrieval engine comes from the silica-core package (ADR-0043), and
the seam in tools/atomic.py is what keeps it a library here.

The engine's five functions carry `@tool` decorators, and they register into
`silica_core.tools.TOOLS`, a different registry from this tree's, so the four
names it shares with tools that do a different job here (silica_search
matches titles, silica_write_note runs the quality gate) stay owned by this
tree. Under ADR-0042 that was a vendoring delta to maintain by hand; it is a
structural fact now, and the test stays because the registry is
last-writer-wins and silent.
"""
import pytest

from silica.tools import TOOLS


@pytest.fixture
def vault(tmp_path, monkeypatch):
    from silica.config import CONFIG
    from silica.kernel.recall import lexical, paths
    from silica_core.kernel.recall import lexical as core_lexical
    from silica_core.kernel.recall import paths as core_paths

    (tmp_path / "lsm.md").write_text(
        "# LSM trees\n\n## Write path\n\nWrites land in a memtable.\n\n"
        "## Compaction\n\nLeveled compaction merges sorted runs.\n", encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    for mod in (paths, core_paths):
        monkeypatch.setattr(mod, "index_dir_for", lambda vault, _d=tmp_path / ".idx": _d)
    lexical.clear()
    core_lexical.clear()
    return tmp_path


def test_the_engine_registers_nothing_here(vault):
    import silica_core.core  # noqa: F401 — the import under test
    import silica.tools.atomic  # noqa: F401
    import silica.tools.codedocs_tool  # noqa: F401
    import silica.tools.notes  # noqa: F401

    owner = {"silica_search": "silica.tools.atomic",
             "silica_files": "silica.tools.atomic",
             "silica_write_note": "silica.tools.notes",
             "silica_code_pack": "silica.tools.codedocs_tool"}
    assert {n: TOOLS[n].fn.__module__ for n in owner} == owner


def test_read_serves_a_section_and_refuses_a_stale_citation(vault):
    from silica.tools.atomic import silica_read, silica_search_context

    hit = silica_search_context("leveled compaction")["hits"][0]
    r = silica_read(hit["path"], section=hit["section"], expect_version=hit["version"])
    assert r["text"].startswith("## Compaction") and r["start"] == 7
    assert [h["title"] for h in r["outline"]] == ["LSM trees", "Write path", "Compaction"]

    (vault / "lsm.md").write_text("# LSM trees\n\nRewritten.\n", encoding="utf-8")
    stale = silica_read(hit["path"], expect_version=hit["version"])
    assert stale["error"]["code"] == "changed" and stale["version"] != hit["version"]


def test_a_vault_opened_at_home_does_not_serve_silicas_own_keys(vault, monkeypatch):
    """With the vault at $HOME, `.silica/.env` is a vault-relative path; the
    engine's containment allows it, and the file holds every API key."""
    from silica.tools.atomic import silica_read

    env = vault / ".silica" / ".env"
    env.parent.mkdir()
    env.write_text("SILICA_X=secret123\n", encoding="utf-8")
    monkeypatch.setattr("silica.config.USER_ENV", env)

    for path in (".silica/.env", "./.silica/../.silica/.env"):
        r = silica_read(path)
        assert "secret123" not in str(r)
        assert r["error"]["code"] == "out_of_root"


def test_the_engine_indexes_apart_from_the_fusion_leg(vault):
    """Its build drops every path its own walk did not produce: pointed at the
    fusion store it would prune the notes the sync lane maintains. The seam
    is what points it at its own file, so the build goes through the seam."""
    from silica.kernel.recall import paths
    from silica.tools.atomic import _engine
    from silica_core.kernel.recall import paths as core_paths

    _engine().build_index()
    assert core_paths.index_file("retrieval").is_file()
    assert not paths.index_file("lexical").is_file()
    assert not core_paths.index_file("lexical").is_file()


def test_the_seam_mirrors_the_vault_into_the_engine(vault, monkeypatch):
    """The engine's CONFIG is its own singleton, read from the same env: what
    this tree serves after a /vault switch reaches it only through the seam."""
    from silica.config import CONFIG
    from silica.tools.atomic import _engine
    from silica_core.config import CONFIG as CORE_CONFIG

    monkeypatch.setattr(CORE_CONFIG, "vault_path", "")
    monkeypatch.setattr(CONFIG, "vault_path", str(vault / "elsewhere"))
    assert _engine().INDEX == "retrieval"
    assert CORE_CONFIG.vault_path == str(vault / "elsewhere")
