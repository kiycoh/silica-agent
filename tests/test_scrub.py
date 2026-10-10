# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""scrub_credentials — the one redaction every output boundary relies on."""
from __future__ import annotations

from silica.kernel.scrub import scrub_credentials


def test_userinfo_and_query_secrets_are_redacted():
    out = scrub_credentials("http://user:sk-real@host:1234/v1?api_key=sk-real&x=1")
    assert "sk-real" not in out
    assert out == "http://***@host:1234/v1?api_key=***&x=1"


def test_cli_flag_secrets_are_redacted():
    out = scrub_credentials("llama-server -m model.gguf --api-key sk-real --port 8080")
    assert "sk-real" not in out
    assert "--port 8080" in out  # only the secret flag's value goes


def test_prose_survives():
    text = "the key = value pair in vault.yaml, and http://localhost:1234/v1"
    assert scrub_credentials(text) == text
