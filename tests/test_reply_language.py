"""`conventions.reply_language`: chat language, distinct from note-content
`language`. Declared, it is the chat's default; undeclared, the chat follows the
person, and a button or slash-command turn takes their earlier messages' language
or the notes'."""
from __future__ import annotations

from silica.kernel.vault_manifest import load_manifest
from silica.prompts import SYSTEM_PROMPT, system_prompt


def test_reply_language_parsed_and_stripped(tmp_path):
    (tmp_path / "vault.yaml").write_text(
        "conventions:\n  reply_language: ' Italian '\n", encoding="utf-8"
    )
    assert load_manifest(tmp_path).conventions.reply_language == "Italian"


def test_blank_reply_language_folds_to_none(tmp_path):
    (tmp_path / "vault.yaml").write_text(
        "conventions:\n  reply_language: '   '\n", encoding="utf-8"
    )
    assert load_manifest(tmp_path).conventions.reply_language is None


def test_absent_reply_language_is_none(tmp_path):
    (tmp_path / "vault.yaml").write_text(
        "conventions:\n  language: italian\n", encoding="utf-8"
    )
    assert load_manifest(tmp_path).conventions.reply_language is None


def test_reply_language_falls_back_to_content_language(tmp_path):
    """No reply_language ⇒ chat follows content language (the common case:
    set `language` only, chat matches it for free)."""
    (tmp_path / "vault.yaml").write_text(
        "conventions:\n  language: italian\n", encoding="utf-8"
    )
    conv = load_manifest(tmp_path).conventions
    assert (conv.reply_language or conv.language) == "italian"


def test_reply_language_overrides_content_language(tmp_path):
    """The divergence case: English papers, Italian chat."""
    (tmp_path / "vault.yaml").write_text(
        "conventions:\n  language: english\n  reply_language: italian\n",
        encoding="utf-8",
    )
    conv = load_manifest(tmp_path).conventions
    assert (conv.reply_language or conv.language) == "italian"


def test_prompt_with_preferred_language_names_it_and_covers_buttons():
    p = system_prompt("Italian")
    assert "Italian" in p
    assert "button" in p and "slash-command" in p  # the turn that had no signal


def test_prompt_without_preference_follows_the_person():
    """No "by default" for an undeclared vault: "Reply in italian by default"
    answered 2 of 12 Chinese questions in Chinese, this rule 9 (2026-10-16). The
    turn with no language of its own is the follow rule's to place."""
    assert system_prompt(None) == SYSTEM_PROMPT
    assert "ALWAYS reply in the language of the person's latest message" in SYSTEM_PROMPT
    assert "by default" not in SYSTEM_PROMPT
    assert "slash command" in SYSTEM_PROMPT and "else its notes'" in SYSTEM_PROMPT


def test_the_seed_of_an_undeclared_vault_follows_the_person(tmp_vault):
    """Italian notes, nothing declared: the language they are detected in no
    longer sets the chat's, at the head of the seed or in its last line."""
    from silica.cli import seed_messages
    from silica.prompts import _LANG_FOLLOW

    it = "Il gatto è sul tavolo e la casa è bella, ma non è facile trovare la strada giusta."
    for i in range(3):
        tmp_vault.note(f"nota{i}.md", it)
    seed = seed_messages()
    assert seed[-1]["content"] == _LANG_FOLLOW
    assert seed[0]["content"].endswith(_LANG_FOLLOW)
    tmp_vault.note("vault.yaml", "conventions:\n  reply_language: italian\n")
    assert "Reply in italian by default" in seed_messages()[-1]["content"]


def test_math_instruction_is_gui_only():
    """GUI renders dollar-math as MathML; TUI would show it raw, so only the
    GUI branch gets the instruction."""
    assert "$$" not in system_prompt("Italian")  # default (TUI): no math
    gui = system_prompt("Italian", math=True)
    assert "$$" in gui and "Italian" in gui  # math + language both present
