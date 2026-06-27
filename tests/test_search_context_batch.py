"""search_context_batch parity + single-eval regression (driver primitive)."""
from unittest.mock import patch

from silica.driver.fs_backend import ObsidianFSBackend
from silica.driver.cli_backend import ObsidianCLIBackend


def test_fs_batch_equals_per_query(tmp_path):
    """FS batch is exactly {q: search_context(q)} — verifies the loop impl."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "A.md").write_text("alpha line\nbeta here", encoding="utf-8")
    (vault / "B.md").write_text("alpha again\ngamma", encoding="utf-8")
    backend = ObsidianFSBackend(str(vault))

    batch = backend.search_context_batch(["alpha", "beta"])

    assert batch == {
        "alpha": backend.search_context("alpha"),
        "beta": backend.search_context("beta"),
    }


def test_cli_batch_single_eval_and_parse(monkeypatch):
    """CLI batch issues ONE eval for all queries (N->1) and parses per-query."""
    from silica.config import CONFIG
    monkeypatch.setattr(CONFIG, "inbox_dir", "", raising=False)  # no inbox filtering
    backend = ObsidianCLIBackend(vault_name="t")

    fake = {
        "alpha": [{"path": "Notes/A.md", "name": "A", "line": 1, "content": "alpha line"}],
        "beta": [],
    }
    with patch.object(backend, "_eval", return_value=fake) as mock_eval:
        out = backend.search_context_batch(["alpha", "beta"])

    assert mock_eval.call_count == 1          # the N->1 regression we care about
    assert out["beta"] == []
    assert len(out["alpha"]) == 1
    hit = out["alpha"][0]
    assert hit.ref.name == "A"
    assert hit.ref.path == "Notes/A.md"
    assert hit.line == 1
    assert hit.snippet == "alpha line"


def test_cli_batch_empty_queries_short_circuits_no_eval():
    """[] -> {} with no eval issued (CLI short-circuit guard)."""
    backend = ObsidianCLIBackend(vault_name="t")
    with patch.object(backend, "_eval") as mock_eval:
        assert backend.search_context_batch([]) == {}
    mock_eval.assert_not_called()


def test_cli_batch_backend_down_maps_every_query_to_empty(monkeypatch):
    """Obsidian down: _eval returns its default ({}), so every query maps to []
    — same graceful degradation recon gets from search_context returning []."""
    from silica.config import CONFIG
    monkeypatch.setattr(CONFIG, "inbox_dir", "", raising=False)
    backend = ObsidianCLIBackend(vault_name="t")

    with patch.object(backend, "_eval", return_value={}):
        out = backend.search_context_batch(["alpha", "beta"])

    assert out == {"alpha": [], "beta": []}
