"""Class B secrets (ADR-0089 point 7, bound by ADR-0091 point 3): a credential
that opens an account of the owner beyond Silica lives in the OS keyring, or
in a 0600 file under a 0700 folder the owner opted into, and never in
os.environ."""
from __future__ import annotations

import os
import stat

import pytest


@pytest.fixture
def no_keyring(monkeypatch):
    import keyring
    import keyring.backends.fail

    previous = keyring.get_keyring()
    keyring.set_keyring(keyring.backends.fail.Keyring())
    yield
    keyring.set_keyring(previous)


def test_a_secret_goes_to_the_keyring_and_never_to_the_environment(_no_real_keyring):
    from silica.connectors import credentials

    credentials.put("mcp/github", "GITHUB_TOKEN", "ghp_secret")
    assert credentials.get("mcp/github", "GITHUB_TOKEN") == "ghp_secret"
    assert ("silica", "mcp/github/GITHUB_TOKEN") in _no_real_keyring.data
    assert "ghp_secret" not in os.environ.values()
    assert credentials.where("mcp/github") == "keyring"


def test_without_a_keyring_saving_refuses_until_the_owner_opts_in(no_keyring):
    from silica.connectors import credentials

    assert credentials.where("mcp/github") == ""
    with pytest.raises(credentials.NoStore):
        credentials.put("mcp/github", "GITHUB_TOKEN", "ghp_secret")
    assert credentials.get("mcp/github", "GITHUB_TOKEN") is None


def test_the_opted_in_file_is_0600_inside_a_0700_folder(no_keyring):
    from silica.connectors import credentials

    path = credentials.opt_in_file("mcp/github")
    credentials.put("mcp/github", "GITHUB_TOKEN", "ghp_secret")
    assert credentials.where("mcp/github") == "file"
    assert path.name == "mcp-github.json" and path.parent.name == "credentials"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert credentials.get("mcp/github", "GITHUB_TOKEN") == "ghp_secret"


def test_the_file_is_never_world_readable_even_for_an_instant(no_keyring, monkeypatch):
    """Created 0600 from the first byte, as the bridge file is: a umask of 0
    must not leave a window where the token is readable."""
    from silica.connectors import credentials

    old = os.umask(0)
    try:
        path = credentials.opt_in_file("mcp/gmail")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        credentials.put("mcp/gmail", "API_KEY", "k")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        os.umask(old)


def test_delete_drops_one_field_and_forget_drops_the_scope(_no_real_keyring):
    from silica.connectors import credentials

    credentials.put("mcp/github", "A", "1")
    credentials.put("mcp/github", "B", "2")
    credentials.delete("mcp/github", "A")
    assert credentials.get("mcp/github", "A") is None
    assert credentials.get("mcp/github", "B") == "2"
    credentials.forget("mcp/github", ["B", "never-set"])
    assert credentials.get("mcp/github", "B") is None


def test_a_keyring_that_fails_at_runtime_reads_as_absent(monkeypatch, _no_real_keyring):
    """A Secret Service with no D-Bus session is viable on paper and raises on
    use; a read then finds nothing instead of killing the turn."""
    import keyring.errors

    from silica.connectors import credentials

    def boom(*a, **k):
        raise keyring.errors.KeyringError("no session bus")
    monkeypatch.setattr(_no_real_keyring, "get_password", boom)
    assert credentials.get("mcp/github", "X") is None
