# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The REPL prompt and toolbar are HTML() markup, so any text interpolated into
them has to survive being parsed as XML — a vault folder named "R&D" used to
raise ExpatError on every iteration of the loop."""
from __future__ import annotations


def test_prompt_survives_a_vault_name_that_is_not_valid_xml(tmp_path, monkeypatch):
    from silica.config import CONFIG
    from silica.ui import prompt

    for name in ("R&D", "Notes & Ideas", "<brackets>", 'quote"d'):
        monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path / name))
        rendered = prompt.prompt_text()  # HTML() parses eagerly: this is the assert
        assert name in "".join(text for _, text in rendered.__pt_formatted_text__())


def test_toolbar_survives_a_model_id_that_is_not_valid_xml(monkeypatch):
    from silica.config import CONFIG
    from silica.ui import prompt

    monkeypatch.setattr(CONFIG, "model", "openrouter/vendor/model<a&b>")
    rendered = prompt.bottom_toolbar()
    assert "model<a&b>" in "".join(
        text for _, text in rendered.__pt_formatted_text__()
    )
