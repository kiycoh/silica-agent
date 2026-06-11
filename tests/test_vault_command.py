# tests/test_vault_command.py
import silica.driver as driver_pkg
from silica.cli import _handle_direct_shortcut
from silica.config import CONFIG
from silica.ui.commands import command_names


def test_vault_is_registered():
    assert "/vault" in command_names()


def test_vault_no_args_shows_status_without_mutating(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    monkeypatch.setattr(CONFIG, "backend", "fs")
    (tmp_path / "a.md").write_text("# a")
    (tmp_path / "b.md").write_text("# b")

    handled = _handle_direct_shortcut("/vault", [])

    assert handled is True
    assert CONFIG.vault_path == str(tmp_path)  # unchanged
    out = capsys.readouterr().out
    assert str(tmp_path) in out
    assert "fs" in out
    assert "2" in out  # note count


def test_vault_switch_updates_config_and_resets_driver(tmp_path, monkeypatch):
    target = tmp_path / "other_vault"
    target.mkdir()
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    # Prime the driver singleton with a sentinel so we can observe the reset.
    monkeypatch.setattr(driver_pkg, "_driver", object())

    handled = _handle_direct_shortcut(f"/vault {target}", [])

    assert handled is True
    assert CONFIG.vault_path == str(target.resolve())
    assert driver_pkg._driver is None  # reset so next get_driver() rebuilds


def test_vault_switch_rejects_nonexistent_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(CONFIG, "vault_path", str(tmp_path))
    sentinel = object()
    monkeypatch.setattr(driver_pkg, "_driver", sentinel)

    handled = _handle_direct_shortcut(f"/vault {tmp_path / 'does_not_exist'}", [])

    assert handled is True
    assert CONFIG.vault_path == str(tmp_path)  # unchanged
    assert driver_pkg._driver is sentinel  # driver NOT reset
    assert "directory" in capsys.readouterr().out.lower()


def test_reset_driver_forces_rebuild(monkeypatch):
    fresh = object()
    monkeypatch.setattr(driver_pkg, "_create_driver", lambda: fresh)
    monkeypatch.setattr(driver_pkg, "_driver", object())

    driver_pkg.reset_driver()
    assert driver_pkg._driver is None

    assert driver_pkg.get_driver() is fresh
