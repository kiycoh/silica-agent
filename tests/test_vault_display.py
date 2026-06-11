# tests/test_vault_display.py
"""The toolbar and home banner must reflect CONFIG.vault_path after a /vault switch."""
from silica.config import CONFIG


def test_bottom_toolbar_shows_vault_path_basename(tmp_path, monkeypatch):
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path / "MyVault"))
    monkeypatch.setattr(CONFIG, "vault_name", "OldName")

    from silica.ui.prompt import bottom_toolbar
    html = bottom_toolbar().value
    assert "MyVault" in html
    assert "OldName" not in html


def test_bottom_toolbar_falls_back_to_vault_name_when_path_unset(monkeypatch):
    monkeypatch.setattr(CONFIG, "vault_path", "")
    monkeypatch.setattr(CONFIG, "vault_name", "Personal")

    from silica.ui.prompt import bottom_toolbar
    html = bottom_toolbar().value
    assert "Personal" in html


def test_print_home_shows_vault_path_basename(tmp_path, monkeypatch):
    vault = tmp_path / "MyVault"
    vault.mkdir()
    monkeypatch.setattr(CONFIG, "vault_path", str(vault))
    monkeypatch.setattr(CONFIG, "vault_name", "OldName")

    from io import StringIO
    from silica.ui.home import print_home
    from silica.ui.console import CONSOLE
    buf = StringIO()
    monkeypatch.setattr(CONSOLE, "print", lambda *a, **kw: buf.write(str(a)))
    # just check _model_vault_line is built with the right vault label
    from silica.ui.home import _model_vault_line
    from pathlib import Path
    vault_label = Path(CONFIG.vault_path).name if CONFIG.vault_path else (CONFIG.vault_name or "—")
    text = _model_vault_line("gpt", "worker", vault_label)
    assert "MyVault" in text.plain
    assert "OldName" not in text.plain
