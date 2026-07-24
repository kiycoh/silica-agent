"""/nucleate — one verb, extension dispatch (spec D2).

md/.txt → Coordinator FSM dispatched inline ("" sentinel); target folder from
--target= or a single folder-pick LLM call (agent-message fallback if the pick
fails); code → skeleton stub staged inline, "" sentinel."""
import json
import subprocess
from pathlib import Path

import pytest

from silica.cli import _expand_workflow_shortcut
from silica.config import CONFIG


@pytest.fixture(autouse=True)
def _reset_manifest_cache():
    from silica.kernel.vault_manifest import reset_manifest_cache
    reset_manifest_cache()
    yield
    reset_manifest_cache()


@pytest.fixture
def stub_coordinator(monkeypatch):
    """Record Coordinator ctor kwargs; skip the real FSM."""
    calls: list[dict] = []

    class _FakeCoordinator:
        def __init__(self, **kw):
            calls.append(kw)

        def run(self):
            return {"final_status": "Success"}

    import silica.router.coordinator as coord_mod
    monkeypatch.setattr(coord_mod, "Coordinator", _FakeCoordinator)
    return calls


def test_supported_nucleate_extensions_covers_every_lane():
    # The GUI "+" picker derives its accept= list from this; every nucleate lane
    # (prose, code, notebook, pdf) must be represented or the picker hides files
    # the server would actually accept.
    from silica.kernel.codeast import BARE_LANGUAGES, EXTENSION_MAP
    from silica.sources.registry import supported_nucleate_extensions

    exts = set(supported_nucleate_extensions())
    assert {".md", ".txt", ".ipynb", ".pdf"} <= exts  # prose / notebook / pdf lanes
    symbol_bearing = {e for e, lang in EXTENSION_MAP.items() if lang not in BARE_LANGUAGES}
    assert symbol_bearing <= exts                      # every symbol-bearing code language
    # bare languages are graph-only (presence, co-change): not a nucleate lane,
    # so the picker must not advertise them
    assert not {".toml", ".html", ".css"} & exts
    assert all(e.startswith(".") for e in exts)        # accept= wants dotted extensions


def test_code_adapter_matches_new_languages_not_bare():
    from silica.sources.code import CodeAdapter

    adapter = CodeAdapter()
    assert adapter.matches("src/App.java")
    assert adapter.matches("src/main.c")
    assert adapter.matches("include/x.hpp")
    for bare in ("pyproject.toml", "site/index.html", "site/style.css"):
        assert not adapter.matches(bare)


def test_nucleate_md_with_target_dispatches_fsm_directly(stub_coordinator):
    msg = _expand_workflow_shortcut("/nucleate Inbox/a.md --target=Concepts/AI")
    assert msg == ""  # handled inline — no agent turn
    assert stub_coordinator == [
        {"inbox_files": ["Inbox/a.md"], "target_dir": "Concepts/AI", "hub": None}
    ]


def test_nucleate_md_missing_target_uses_folder_pick(stub_coordinator, monkeypatch):
    import silica.cli as cli_mod
    monkeypatch.setattr(cli_mod, "_pick_target_folder", lambda files: "Concepts/AI")
    msg = _expand_workflow_shortcut("/nucleate Inbox/a.md")
    assert msg == ""
    assert stub_coordinator[0]["target_dir"] == "Concepts/AI"
    assert stub_coordinator[0]["inbox_files"] == ["Inbox/a.md"]


def test_nucleate_folder_pick_failure_falls_back_to_agent(monkeypatch):
    import silica.cli as cli_mod

    def boom(files):
        raise ValueError("no llm")

    monkeypatch.setattr(cli_mod, "_pick_target_folder", boom)
    msg = _expand_workflow_shortcut("/nucleate Inbox/a.md")
    assert msg is not None and "silica_run_injector" in msg
    assert "Inbox/a.md" in msg
    # the agent must pick the folder, not receive a preset one
    assert "target_dir=<chosen folder>" in msg
    assert "most relevant existing vault folder" in msg


def test_nucleate_no_resolvable_files_falls_back_to_agent():
    # A dropped --folder= (starts with '-', so the flag parser skips it) used to
    # hard-error "requires at least one file". Now the raw line goes to the agent
    # to infer intent instead of rejecting it.
    msg = _expand_workflow_shortcut("/nucleate --folder=Inbox/x --target=Concepts")
    assert msg is not None
    assert not msg.startswith("Error:")
    assert "silica_run_injector" in msg
    assert "--folder=Inbox/x" in msg  # the raw input is echoed for the agent


def test_unknown_slash_command_falls_through_to_agent():
    from silica.cli import _handle_slash_command
    # Known meta command → handled deterministically (True).
    assert _handle_slash_command("/model", []) is True
    # Unknown command → None so the REPL hands the raw line to the agent
    # instead of printing "Unknown command".
    assert _handle_slash_command("/ingest --folder=x --target=y", []) is None


def test_inject_shortcut_is_retired():
    assert _expand_workflow_shortcut("/inject Inbox/a.md --target=C") is None


def test_plain_prose_with_apostrophe_is_not_hijacked():
    # An Italian contraction ("L'hub") is a single unmatched shlex quote char.
    # Non-slash input must skip shlex entirely, not get replaced by the
    # "unbalanced quotes" error message.
    msg = _expand_workflow_shortcut("L'hub machine learning quali 5 argomenti fondamentali riporta?")
    assert msg is None


def test_slash_command_unbalanced_quotes_still_errors():
    msg = _expand_workflow_shortcut('/nucleate "Inbox/no closing quote.pdf')
    assert msg == 'Error: unbalanced quotes in command. Wrap paths with spaces in "...".'


def test_run_injector_rejects_pdf_with_convert_hint(repo_vault):
    # The agent tool has no converter; a .pdf reaching the FSM would be read as
    # binary garbage. Guard rejects it and points the agent at /convert.
    from silica.tools import TOOLS

    out = TOOLS["silica_run_injector"].fn(inbox_files=["Inbox/paper.pdf"], target_dir="Concepts")
    assert "error" in out
    assert "paper.pdf" in out["error"] and "/convert" in out["error"]


def test_run_injector_rejects_unknown_type_without_convert_hint(repo_vault):
    from silica.tools import TOOLS

    out = TOOLS["silica_run_injector"].fn(inbox_files=["Inbox/data.csv"], target_dir="Concepts")
    assert "error" in out
    assert "data.csv" in out["error"] and "/convert" not in out["error"]


@pytest.fixture
def repo_vault(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "m.py").write_text("def hi():\n    return 1\n", encoding="utf-8")
    (tmp_path / "data.csv").write_text("a,b\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    vault = tmp_path / ".silica"
    vault.mkdir()
    monkeypatch.setattr(CONFIG, "vault_path", str(vault))
    monkeypatch.setattr(CONFIG, "inbox_dir", "Inbox")
    from silica.driver import fs_backend
    import silica.driver as driver_mod
    monkeypatch.setattr(driver_mod, "DRIVER", fs_backend.ObsidianFSBackend(str(vault)))
    return tmp_path, vault


def test_nucleate_code_stages_stub_and_returns_sentinel(repo_vault):
    root, vault = repo_vault
    msg = _expand_workflow_shortcut("/nucleate m.py")
    assert msg == ""  # fully handled inline, nothing for the agent
    stub = vault / "Inbox" / "m.md"
    assert stub.is_file()
    text = stub.read_text(encoding="utf-8")
    assert "def hi()" in text and "return 1" not in text


def test_nucleate_mixed_batch_stages_code_and_dispatches_md(repo_vault, stub_coordinator):
    root, vault = repo_vault
    msg = _expand_workflow_shortcut("/nucleate m.py Inbox/note.md --target=Concepts")
    assert msg == ""
    assert stub_coordinator[0]["inbox_files"] == ["Inbox/note.md"]  # md → FSM
    # code file NOT forwarded (staged inline)
    assert (vault / "Inbox" / "m.md").is_file()


def test_nucleate_unsupported_extension_is_skipped(repo_vault, capsys):
    root, vault = repo_vault
    msg = _expand_workflow_shortcut("/nucleate data.csv")
    assert msg == ""  # handled, nothing for the agent
    assert not (vault / "Inbox" / "data.md").exists()
    out = capsys.readouterr().out
    assert "data.csv" in out and "Skipped" in out  # warning is part of the contract


def test_nucleate_folder_and_connective_words_falls_back_to_agent(repo_vault):
    # "Inbox/lacascia in lacascia/" — a folder plus the connective word "in".
    # None of the three tokens resolves to an ingestible file (no extension), but
    # the intent is clear, so the raw line goes to the agent instead of silently
    # doing nothing.
    root, vault = repo_vault
    msg = _expand_workflow_shortcut("/nucleate Inbox/lacascia in lacascia/")
    assert msg is not None and msg != ""   # not the silent "handled" sentinel
    assert not msg.startswith("Error:")
    assert "silica_run_injector" in msg


def test_nucleate_pdf_converts_and_forwards_converted_md(repo_vault, monkeypatch, stub_coordinator):
    """No adapter claims .pdf → convert() runs and the CONVERTED .md is what
    the FSM is told to re-read (not the .pdf)."""
    import silica.sources.convert as conv_mod

    monkeypatch.setattr(conv_mod, "convert", lambda f, dest_dir="": ["Inbox/paper.md"])
    msg = _expand_workflow_shortcut("/nucleate paper.pdf --target=Concepts/AI")
    assert msg == ""
    assert stub_coordinator[0]["inbox_files"] == ["Inbox/paper.md"]  # converted .md, not the .pdf


def test_nucleate_pdf_converter_error_is_caught(repo_vault, monkeypatch, capsys):
    import silica.sources.convert as conv_mod

    def boom(f, dest_dir=""):
        raise ValueError("mineru not installed")

    monkeypatch.setattr(conv_mod, "convert", boom)
    msg = _expand_workflow_shortcut("/nucleate paper.pdf --target=Concepts/AI")
    assert msg == ""  # nothing to run; batch did not crash
    assert "mineru not installed" in capsys.readouterr().out


def test_convert_command_returns_sentinel_and_reports(repo_vault, monkeypatch, capsys):
    import silica.sources.convert as conv_mod

    monkeypatch.setattr(conv_mod, "convert", lambda f, dest_dir="": ["Inbox/paper.md"])
    msg = _expand_workflow_shortcut("/convert paper.pdf")
    assert msg == ""  # fully handled inline
    assert "Converted" in capsys.readouterr().out


def test_convert_command_no_files_errors():
    msg = _expand_workflow_shortcut("/convert --target=X")
    assert msg is not None and msg.startswith("Error:")


# ---------------------------------------------------------------------------
# Re-nucleate-of-modified-source warning (spec-hermes-coherence §3): a file
# about to be staged whose basename is already registered in
# .silica/provenance.json under a DIFFERENT sha256 means notes derived from
# it may now be stale.
# ---------------------------------------------------------------------------

def test_nucleate_renucleate_of_modified_source_warns(repo_vault, capsys, stub_coordinator):
    root, vault = repo_vault
    inbox = vault / "Inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "lezione.md").write_text("v2 content", encoding="utf-8")

    from silica.kernel.provenance import append_record
    append_record("lezione.md", "old-sha-not-matching", "run1", ["Concepts/A", "Concepts/B"])

    msg = _expand_workflow_shortcut("/nucleate Inbox/lezione.md --target=Concepts/AI")
    assert msg == ""

    out = capsys.readouterr().out
    assert "re-nucleate of a modified source" in out
    assert "2 note" in out


def test_nucleate_same_sha_no_warning(repo_vault, capsys, stub_coordinator):
    root, vault = repo_vault
    inbox = vault / "Inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "lezione.md").write_text("same content", encoding="utf-8")

    from silica.kernel.provenance import append_record, content_sha256
    sha = content_sha256("Inbox/lezione.md")
    append_record("lezione.md", sha, "run1", ["Concepts/A"])

    msg = _expand_workflow_shortcut("/nucleate Inbox/lezione.md --target=Concepts/AI")
    assert msg is not None

    out = capsys.readouterr().out
    assert "re-nucleate of a modified source" not in out


def test_nucleate_no_prior_provenance_no_warning(repo_vault, capsys, stub_coordinator):
    root, vault = repo_vault
    inbox = vault / "Inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "fresh.md").write_text("brand new", encoding="utf-8")

    msg = _expand_workflow_shortcut("/nucleate Inbox/fresh.md --target=Concepts/AI")
    assert msg is not None

    out = capsys.readouterr().out
    assert "re-nucleate of a modified source" not in out


def test_nucleate_missing_target_still_warns_on_renucleate(repo_vault, capsys, monkeypatch):
    """Auto-target (no --target) is a valid invocation — the provenance
    drift warning must still print on the way to the agent fallback."""
    import silica.cli as cli_mod
    root, vault = repo_vault
    inbox = vault / "Inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "lezione.md").write_text("v2 content", encoding="utf-8")

    from silica.kernel.provenance import append_record
    append_record("lezione.md", "old-sha-not-matching", "run1", ["Concepts/A", "Concepts/B"])

    monkeypatch.setattr(cli_mod, "_pick_target_folder",
                        lambda files: (_ for _ in ()).throw(ValueError("no llm")))
    msg = _expand_workflow_shortcut("/nucleate Inbox/lezione.md")

    assert msg is not None and "silica_run_injector" in msg
    out = capsys.readouterr().out
    assert "re-nucleate of a modified source" in out


def test_settings_sets_and_shows_vault_yaml(repo_vault, capsys):
    from pathlib import Path
    from silica.config import CONFIG
    from silica.kernel.vault_manifest import get_active_manifest, reset_manifest_cache

    msg = _expand_workflow_shortcut("/settings conventions.language italian")
    assert msg == ""
    assert "language" in (Path(CONFIG.vault_path) / "vault.yaml").read_text()
    assert get_active_manifest().conventions.language == "italian"  # cache reset

    msg = _expand_workflow_shortcut("/settings")
    assert msg == ""
    assert "italian" in capsys.readouterr().out

    assert _expand_workflow_shortcut("/settings bogus.key x").startswith("Error:")
    reset_manifest_cache()


def test_run_injector_projects_outcomes_not_raw_context(repo_vault, monkeypatch):
    """Agent boundary gets outcomes only: no payload/recon (planned concepts once
    read as 'created notes'), per-chunk failures not last-error-wins."""
    import silica.router.coordinator as coord_mod

    raw = {
        "final_status": "failed",
        "committed_chunks": 0,
        "failed_chunks": [{"chunk": f"f0_c{i}", "error": "boom"} for i in range(6)],
        "error": "Critical failure delegating batch 5: boom",
        "payload": {"chunks": ["planned concepts must not leak"]},
        "recon": {"concepts": ["Stimatore media campionaria"]},
        "subagents": {},
    }

    class _Fake:
        def __init__(self, **kw):
            self.fsm = type("F", (), {"progress": type("P", (), {"run_id": "r1"})()})()

        def run(self):
            return raw

    monkeypatch.setattr(coord_mod, "Coordinator", _Fake)
    from silica.tools import TOOLS

    out = TOOLS["silica_run_injector"].fn(inbox_files=["Inbox/a.md"], target_dir="C")
    assert out["final_status"] == "failed"
    assert out["chunks_failed"] == 6 and out["chunks_committed"] == 0
    assert len(out["failed_chunks"]) == 6
    assert "payload" not in out and "recon" not in out
    assert out["run_id"] == "r1"
