"""vault.yaml manifest (ADR-0014): declared capabilities, not vault types."""
import subprocess

import pytest

from silica.config import CONFIG
from silica.kernel.vault_manifest import (
    apply_manifest_to_config,
    get_active_manifest,
    load_manifest,
    reset_manifest_cache,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    reset_manifest_cache()
    yield
    reset_manifest_cache()


def test_defaults_prose_and_mail_outside_git(tmp_path):
    m = load_manifest(tmp_path)
    assert m.sources == ("prose", "mail")
    assert m.cooccurrence_lang is None


def test_defaults_include_code_inside_git(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    vault = tmp_path / ".silica"
    vault.mkdir()
    assert load_manifest(vault).sources == ("prose", "mail", "code", "notebook")


def test_manifest_file_overrides_defaults(tmp_path):
    (tmp_path / "vault.yaml").write_text(
        "sources: [prose]\ncooccurrence_lang: italian\n",
        encoding="utf-8",
    )
    m = load_manifest(tmp_path)
    assert m.sources == ("prose",)
    assert m.cooccurrence_lang == "italian"


def test_malformed_manifest_degrades_to_defaults(tmp_path):
    (tmp_path / "vault.yaml").write_text("sources: 42\n", encoding="utf-8")
    assert load_manifest(tmp_path).sources == ("prose", "mail")


def test_episodic_keys_absent_means_no_schema(tmp_path):
    (tmp_path / "vault.yaml").write_text("conventions:\n  max_tags: 5\n",
                                         encoding="utf-8")
    assert load_manifest(tmp_path).conventions.episodic_keys is None


def test_episodic_keys_empty_block_gets_defaults(tmp_path):
    (tmp_path / "vault.yaml").write_text("conventions:\n  episodic_keys: {}\n",
                                         encoding="utf-8")
    ks = load_manifest(tmp_path).conventions.episodic_keys
    assert ks is not None
    assert ks.prefixes == ("user", "assistant")
    assert ks.default_prefix == "user"
    assert ks.max_depth == 3


def test_episodic_keys_custom_values_parsed(tmp_path):
    (tmp_path / "vault.yaml").write_text(
        "conventions:\n"
        "  episodic_keys:\n"
        "    prefixes: [user, assistant, team]\n"
        "    default_prefix: team\n"
        "    max_depth: 2\n",
        encoding="utf-8",
    )
    ks = load_manifest(tmp_path).conventions.episodic_keys
    assert ks.prefixes == ("user", "assistant", "team")
    assert ks.default_prefix == "team"
    assert ks.max_depth == 2


def test_episodic_keys_malformed_block_means_no_schema(tmp_path):
    (tmp_path / "vault.yaml").write_text(
        "conventions:\n  episodic_keys: [user]\n", encoding="utf-8")
    assert load_manifest(tmp_path).conventions.episodic_keys is None


def test_episodic_keys_bad_fields_fall_back_to_defaults(tmp_path):
    (tmp_path / "vault.yaml").write_text(
        "conventions:\n"
        "  episodic_keys:\n"
        "    prefixes: 42\n"
        "    default_prefix: [x]\n"
        "    max_depth: -1\n",
        encoding="utf-8",
    )
    ks = load_manifest(tmp_path).conventions.episodic_keys
    assert ks.prefixes == ("user", "assistant")
    assert ks.default_prefix == "user"
    assert ks.max_depth == 3
    (tmp_path / "vault.yaml").write_text(":\n  - not yaml mapping [", encoding="utf-8")
    assert load_manifest(tmp_path).sources == ("prose", "mail")


def test_get_active_manifest_caches_until_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    assert get_active_manifest().sources == ("prose", "mail")
    (tmp_path / "vault.yaml").write_text("sources: [prose, code]\n", encoding="utf-8")
    assert get_active_manifest().sources == ("prose", "mail")  # cached
    reset_manifest_cache()
    assert get_active_manifest().sources == ("prose", "code")


def test_apply_manifest_defaults_to_auto_without_manifest(tmp_path, monkeypatch):
    """Regression: vault with no vault.yaml at all must land on the config-level
    "auto" default (per-store detection), not the dead "english" fallback."""
    monkeypatch.delenv("SILICA_COOCCURRENCE_LANG", raising=False)
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    monkeypatch.setattr(CONFIG, "cooccurrence_lang", "english")
    apply_manifest_to_config()
    assert CONFIG.cooccurrence_lang == "auto"


def test_apply_manifest_defaults_to_auto_when_manifest_omits_field(tmp_path, monkeypatch):
    """Regression: vault.yaml present but without `cooccurrence_lang:` must also
    land on "auto", not "english"."""
    (tmp_path / "vault.yaml").write_text("sources: [prose]\n", encoding="utf-8")
    monkeypatch.delenv("SILICA_COOCCURRENCE_LANG", raising=False)
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    monkeypatch.setattr(CONFIG, "cooccurrence_lang", "english")
    apply_manifest_to_config()
    assert CONFIG.cooccurrence_lang == "auto"


def test_apply_manifest_env_var_wins_over_auto_default(tmp_path, monkeypatch):
    """Env var precedence must survive the bug fix: it still wins even when the
    manifest declares nothing."""
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    monkeypatch.setattr(CONFIG, "cooccurrence_lang", "french")
    monkeypatch.setenv("SILICA_COOCCURRENCE_LANG", "french")
    apply_manifest_to_config()
    assert CONFIG.cooccurrence_lang == "french"


def test_apply_manifest_env_wins(tmp_path, monkeypatch):
    (tmp_path / "vault.yaml").write_text(
        "cooccurrence_lang: italian\n", encoding="utf-8"
    )
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    monkeypatch.setattr(CONFIG, "cooccurrence_lang", "english")

    monkeypatch.delenv("SILICA_COOCCURRENCE_LANG", raising=False)
    apply_manifest_to_config()
    assert CONFIG.cooccurrence_lang == "italian"

    reset_manifest_cache()
    monkeypatch.setattr(CONFIG, "cooccurrence_lang", "french")
    monkeypatch.setenv("SILICA_COOCCURRENCE_LANG", "french")
    apply_manifest_to_config()
    assert CONFIG.cooccurrence_lang == "french"  # env precedence


def test_apply_manifest_clears_lang_on_switch_to_plain_vault(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "vault.yaml").write_text(
        "cooccurrence_lang: italian\n", encoding="utf-8"
    )
    monkeypatch.delenv("SILICA_COOCCURRENCE_LANG", raising=False)
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path / "a"))
    monkeypatch.setattr(CONFIG, "cooccurrence_lang", "english")
    apply_manifest_to_config()
    assert CONFIG.cooccurrence_lang == "italian"

    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path / "b"))
    reset_manifest_cache()
    apply_manifest_to_config()
    assert CONFIG.cooccurrence_lang == "auto"  # not leaked from vault a; "auto" is the real default


def test_nucleate_gating_sources_prose_only(tmp_path, monkeypatch, capsys):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    vault = tmp_path / ".silica"
    vault.mkdir()
    (vault / "vault.yaml").write_text("sources: [prose]\n", encoding="utf-8")
    monkeypatch.setattr(CONFIG, "vault_path", str(vault))
    from silica.driver import fs_backend
    import silica.driver as driver_mod
    monkeypatch.setattr(driver_mod, "DRIVER", fs_backend.ObsidianFSBackend(str(vault)))
    reset_manifest_cache()

    from silica.cli import _expand_workflow_shortcut

    msg = _expand_workflow_shortcut("/nucleate m.py")
    assert msg == ""  # handled: skipped, nothing for the agent
    assert not (vault / "Inbox" / "m.md").exists()  # code source disabled
    assert "Skipped" in capsys.readouterr().out


# --- the line-level writer of a block's key (set_block_key / unset_block_key) -----------------
# vault.yaml is hand-written and PyYAML writes lists both ways, so a key's value can run over many
# lines in either shape: what is left of an old value makes the file unreadable, and the next
# write of `vaults:` writes back the empty read (final review of ADR-0157, C1).

def _manifest(vault, text: str):
    from silica.kernel.vault_manifest import MANIFEST_REL

    p = vault / MANIFEST_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _read(vault) -> dict:
    import yaml

    from silica.kernel.vault_manifest import MANIFEST_REL

    return yaml.safe_load((vault / MANIFEST_REL).read_text(encoding="utf-8"))


_INDENTED = ("public:\n  handle: marta\n  vaults:\n    - folder: Memory\n      title: Old\n"
             "    - folder: Courses\n  sources: {}\n  folders:\n    - Memory\n    - Courses\n"
             "  page: true\nzotero:\n  library: dc8XI1gaCC7j\n")
# PyYAML's own default_flow_style=False shape: a list's dashes at its key's column
_INDENTLESS = ("public:\n  handle: marta\n  vaults:\n  - folder: Memory\n    title: Old\n"
               "  - folder: Courses\n  sources: {}\n  folders:\n  - Memory\n  - Courses\n"
               "  page: true\nzotero:\n  library: dc8XI1gaCC7j\n")


@pytest.mark.parametrize("text", [_INDENTED, _INDENTLESS], ids=["indented", "indentless"])
def test_a_list_value_is_replaced_whole_and_its_neighbours_stay(tmp_path, text):
    from silica.kernel.vault_manifest import set_block_key, unset_block_key

    _manifest(tmp_path, text)

    set_block_key(tmp_path, "public", "vaults", [{"folder": "Notes", "title": "New"}])
    unset_block_key(tmp_path, "public", "folders")
    unset_block_key(tmp_path, "public", "page")

    got = _read(tmp_path)
    assert got["public"] == {"handle": "marta", "vaults": [{"folder": "Notes", "title": "New"}], "sources": {}}
    assert got["zotero"] == {"library": "dc8XI1gaCC7j"}


def test_only_a_key_at_the_blocks_own_depth_is_the_key(tmp_path):
    """A file named `handle` under `sources:` is not the Page's handle."""
    from silica.kernel.vault_manifest import set_block_key, unset_block_key

    _manifest(tmp_path, "public:\n  sources:\n    handle:\n      answer: mine\n  handle: marta\n")

    set_block_key(tmp_path, "public", "handle", "luca")
    assert _read(tmp_path)["public"] == {"sources": {"handle": {"answer": "mine"}}, "handle": "luca"}
    unset_block_key(tmp_path, "public", "handle")
    assert _read(tmp_path)["public"] == {"sources": {"handle": {"answer": "mine"}}}


def test_a_block_indented_by_four_gets_its_new_key_at_four(tmp_path):
    from silica.kernel.vault_manifest import set_block_key

    _manifest(tmp_path, "public:\n    handle: marta\n")

    set_block_key(tmp_path, "public", "vaults", [{"folder": "Memory"}])

    assert _read(tmp_path)["public"] == {"handle": "marta", "vaults": [{"folder": "Memory"}]}


@pytest.mark.parametrize("title", ["Culture \U0001f3a8", "Citt\u00e0 \u4e2d\u6587", "a\x85b\u2028c\x9fd\ufffee"])
def test_a_title_past_ascii_comes_back_as_it_was_written(tmp_path, monkeypatch, title):
    """An emoji went out as a surrogate pair, which PyYAML reads as two lone halves: the view
    could not be read and every build failed on it."""
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    (tmp_path / "Culture").mkdir()
    from silica.kernel import publish

    publish.add_vault("Culture")
    publish.update_vault("culture", {"title": title})

    assert publish.vaults(tmp_path)[0]["title"] == title.strip()[:80]
    publish.vaults(tmp_path)[0]["title"].encode("utf-8")  # no lone surrogate left to refuse


def test_an_emoji_credit_comes_back_as_it_was_written(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    from silica.kernel import rights

    rights.save_answer("Book.pdf", "open", license="CC BY", credit="Ada \U0001f9ea Lab", vault=tmp_path)

    assert rights.answers(tmp_path)["Book.pdf"]["credit"] == "Ada \U0001f9ea Lab"


def test_a_pair_written_before_the_fix_is_read_as_its_character(tmp_path):
    """Files written with the old escape exist: they read as the character, not two halves."""
    from silica.kernel import publish, rights

    _manifest(tmp_path, 'public:\n  vaults: [{"folder": "Culture", "title": "Culture \\ud83c\\udfa8"}]\n'
                        '  sources: {"Book.pdf": {"answer": "open", "credit": "\\ud83e\\uddea", "license": "CC BY"}}\n'
                        '  handle: "\\udfa8"\n')

    assert publish.vaults(tmp_path)[0]["title"] == "Culture \U0001f3a8"
    assert rights.answers(tmp_path)["Book.pdf"]["credit"] == "\U0001f9ea"
    assert publish._block(tmp_path)["handle"] == "\ufffd"  # a lone half has no character: replaced


@pytest.mark.parametrize("text", [
    "public:\n  vaults:\n    - folder: [Memory\n",  # does not parse
    "public: {}\n",  # parses, but a key under it would not
], ids=["unreadable", "inline-block"])
def test_a_write_that_cannot_keep_the_file_readable_is_refused_and_writes_nothing(tmp_path, text):
    from silica.kernel.vault_manifest import MANIFEST_REL, set_block_key, unset_block_key

    p = _manifest(tmp_path, text)

    with pytest.raises(ValueError):
        set_block_key(tmp_path, "public", "handle", "marta")
    assert p.read_bytes() == text.encode("utf-8")
    if text.startswith("public:\n"):
        with pytest.raises(ValueError):
            unset_block_key(tmp_path, "public", "vaults")
        assert (tmp_path / MANIFEST_REL).read_bytes() == text.encode("utf-8")
