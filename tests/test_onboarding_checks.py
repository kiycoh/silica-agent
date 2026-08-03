"""Tests for silica.onboarding.checks — pure doctor diagnostics."""
from __future__ import annotations


import httpx
import pytest

from silica.config import SilicaConfig


def _cfg(**overrides) -> SilicaConfig:
    """Fresh config with explicit fields — never depends on the dev's .env."""
    cfg = SilicaConfig()
    cfg.model = ""
    cfg._provider = None
    cfg.vault_path = ""
    cfg.backend = "fs"
    cfg.embedding_model = "test-embed"
    cfg.embedding_base_url = "http://localhost:9999/v1"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class TestCheckChatModel:
    def test_empty_model_fails_with_init_hint(self):
        from silica.onboarding.checks import check_chat_model
        r = check_chat_model(_cfg(model=""))
        assert r.status == "fail"
        assert "silica init" in r.hint

    def test_openrouter_without_key_fails(self, monkeypatch):
        from silica.onboarding.checks import check_chat_model
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        r = check_chat_model(_cfg(model="openrouter/openai/gpt-4o-mini"))
        assert r.status == "fail"
        assert "OPENROUTER_API_KEY" in r.detail

    def test_openrouter_with_key_ok(self, monkeypatch):
        from silica.onboarding.checks import check_chat_model
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        r = check_chat_model(_cfg(model="openrouter/openai/gpt-4o-mini"))
        assert r.status == "ok"

    def test_lmstudio_model_ok_without_key(self, monkeypatch):
        from silica.onboarding.checks import check_chat_model
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        r = check_chat_model(_cfg(model="qwen3-30b"))
        assert r.status == "ok"


class TestCheckChatEndpoint:
    def test_no_model_skips_as_unknown(self):
        """Nothing was probed, so nothing is known — a skip is not a warning."""
        from silica.onboarding.checks import check_chat_endpoint
        r = check_chat_endpoint(_cfg(model=""))
        assert r.status == "unknown"
        assert "skipped" in r.detail

    def test_openrouter_not_probed(self):
        """"ok" would claim the endpoint is live; the check never asked it."""
        from silica.onboarding.checks import check_chat_endpoint
        r = check_chat_endpoint(_cfg(model="openrouter/openai/gpt-4o-mini"))
        assert r.status == "unknown"
        assert "not probed" in r.detail

    def test_lmstudio_unreachable_fails(self, monkeypatch):
        import silica.onboarding.checks as checks

        def boom(url, timeout):
            raise checks.httpx.ConnectError("refused")

        monkeypatch.setattr(checks.httpx, "get", boom)
        r = checks.check_chat_endpoint(_cfg(model="qwen3-30b"))
        assert r.status == "fail"
        assert "LM Studio" in r.hint

    def test_lmstudio_reachable_ok(self, monkeypatch):
        import silica.onboarding.checks as checks

        class FakeResp:
            status_code = 200

        monkeypatch.setattr(checks.httpx, "get", lambda url, timeout: FakeResp())
        r = checks.check_chat_endpoint(_cfg(model="qwen3-30b"))
        assert r.status == "ok"

    def test_ollama_is_probed_not_hosted(self, monkeypatch):
        import silica.onboarding.checks as checks

        captured: dict = {}

        class FakeResp:
            status_code = 200

        def fake_get(url, timeout):
            captured["url"] = url
            return FakeResp()

        monkeypatch.setattr(checks.httpx, "get", fake_get)
        r = checks.check_chat_endpoint(_cfg(model="ollama/llama3.2:3b"))
        assert r.status == "ok"
        assert captured["url"] == "http://localhost:11434/v1/models"

    def test_ollama_unreachable_fails_with_ollama_hint(self, monkeypatch):
        import silica.onboarding.checks as checks

        def boom(url, timeout):
            raise checks.httpx.ConnectError("refused")

        monkeypatch.setattr(checks.httpx, "get", boom)
        r = checks.check_chat_endpoint(_cfg(model="ollama/llama3.2:3b"))
        assert r.status == "fail"
        assert "Ollama" in r.hint


class TestCheckVault:
    def test_explicit_path_missing_fails(self):
        from silica.onboarding.checks import check_vault
        r = check_vault(_cfg(vault_path="/nonexistent/vault"))
        assert r.status == "fail"

    def test_explicit_path_ok_with_inbox(self, tmp_path):
        from silica.onboarding.checks import check_vault
        (tmp_path / "Inbox").mkdir()
        r = check_vault(_cfg(vault_path=str(tmp_path), inbox_dir="Inbox"))
        assert r.status == "ok"

    def test_missing_inbox_warns(self, tmp_path):
        from silica.onboarding.checks import check_vault
        r = check_vault(_cfg(vault_path=str(tmp_path), inbox_dir="Inbox"))
        assert r.status == "warn"
        assert "Inbox" in r.detail

    def test_unset_no_repo_fs_backend_fails(self, monkeypatch):
        """fs + no vault_path and no repo → fail with actionable hint."""
        import silica.onboarding.checks as checks
        monkeypatch.setattr(checks.gitstate, "find_repo_root", lambda p: None)
        r = checks.check_vault(_cfg(vault_path="", backend="fs"))
        assert r.status == "fail"
        assert "SILICA_VAULT" in r.hint
        assert "silica init" in r.hint

    def test_unset_with_repo_reports_the_root_as_the_vault(self, monkeypatch, tmp_path):
        # Startup adopts the repo root without asking, so doctor must agree.
        import silica.onboarding.checks as checks
        monkeypatch.setattr(checks.gitstate, "find_repo_root", lambda p: tmp_path)
        r = checks.check_vault(_cfg(vault_path=""))
        assert r.status == "ok"
        assert r.detail == f"repo mode → {tmp_path}"

    def test_unset_with_repo_reports_the_root_even_with_a_legacy_layout(
        self, monkeypatch, tmp_path
    ):
        import silica.onboarding.checks as checks
        (tmp_path / "docs" / "silica").mkdir(parents=True)
        (tmp_path / "docs" / "silica" / "nota.md").write_text("# nota", encoding="utf-8")
        monkeypatch.setattr(checks.gitstate, "find_repo_root", lambda p: tmp_path)
        r = checks.check_vault(_cfg(vault_path=""))
        assert r.detail == f"repo mode → {tmp_path}"

    def test_explicit_path_not_writable_fails(self, tmp_path):
        import os as os_mod

        from silica.onboarding.checks import check_vault

        vault = tmp_path / "ro_vault"
        vault.mkdir()
        vault.chmod(0o500)
        try:
            if os_mod.access(vault, os_mod.W_OK):
                pytest.skip("running with permissions that ignore chmod (e.g. root)")
            r = check_vault(_cfg(vault_path=str(vault)))
            assert r.status == "fail"
            assert "writable" in r.detail
        finally:
            vault.chmod(0o700)


class TestCheckEmbeddings:
    def test_unreachable_warns_never_fails(self, monkeypatch):
        import silica.onboarding.checks as checks

        def boom(url, json, timeout):
            raise checks.httpx.ConnectError("refused")

        monkeypatch.setattr(checks.httpx, "post", boom)
        r = checks.check_embeddings(_cfg())
        assert r.status == "warn"
        assert "co-occurrence" in r.hint

    def test_model_rejected_warns(self, monkeypatch):
        import silica.onboarding.checks as checks

        class FakeResp:
            status_code = 400

            def raise_for_status(self):
                raise httpx.HTTPStatusError(
                    "400", request=httpx.Request("POST", "http://x"),
                    response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
                )

        monkeypatch.setattr(checks.httpx, "post", lambda url, json, timeout: FakeResp())
        r = checks.check_embeddings(_cfg(embedding_model="test-embed"))
        assert r.status == "warn"
        assert "test-embed" in r.detail

    def test_successful_probe_is_ok(self, monkeypatch):
        import silica.onboarding.checks as checks

        class FakeResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"embedding": [0.1, 0.2]}]}

        monkeypatch.setattr(checks.httpx, "post", lambda url, json, timeout: FakeResp())
        r = checks.check_embeddings(_cfg(embedding_model="test-embed"))
        assert r.status == "ok"

    def test_probe_ignores_a_file_path_model_id(self, monkeypatch):
        """llama-server reports the loaded gguf's path as its /models id and
        ignores the requested `model` field on a single-model server — the
        probe must not depend on the two agreeing."""
        import silica.onboarding.checks as checks

        class FakeResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"embedding": [0.1, 0.2]}]}

        monkeypatch.setattr(checks.httpx, "post", lambda url, json, timeout: FakeResp())
        r = checks.check_embeddings(_cfg(embedding_model="text-embedding-qwen3-embedding-4b"))
        assert r.status == "ok"


def test_check_manifest_absent_is_ok(tmp_path):
    from silica.config import SilicaConfig
    from silica.onboarding.checks import check_manifest

    cfg = SilicaConfig()
    cfg.vault_path = str(tmp_path)
    assert check_manifest(cfg).status == "ok"


def test_check_manifest_unknown_source_warns(tmp_path):
    from silica.config import SilicaConfig
    from silica.onboarding.checks import check_manifest

    (tmp_path / "vault.yaml").write_text("sources: [prose, zotero]\n", encoding="utf-8")
    cfg = SilicaConfig()
    cfg.vault_path = str(tmp_path)
    res = check_manifest(cfg)
    assert res.status == "warn" and "zotero" in res.detail


class TestCheckRerank:
    """The rerank pass was invisible: off by default, undocumented, unchecked.
    Doctor must make its state a fact the user can see, never a silent no-op."""

    def _rr_cfg(self, **kw):
        return _cfg(rerank_base_url="", rerank_model="", **kw)

    def test_extra_installed_reports_ok(self, monkeypatch):
        import silica.onboarding.checks as checks

        monkeypatch.setattr(checks, "has_local_rerank", lambda: True)
        r = checks.check_rerank(self._rr_cfg())
        assert r.status == "ok" and "in-process" in r.detail

    def test_nothing_installed_warns_with_install_hint(self, monkeypatch):
        import silica.onboarding.checks as checks

        monkeypatch.setattr(checks, "has_local_rerank", lambda: False)
        r = checks.check_rerank(self._rr_cfg())
        assert r.status == "warn"
        assert "silica-agent[rerank]" in r.hint

    def test_configured_endpoint_reachable_is_ok(self, monkeypatch):
        import silica.onboarding.checks as checks

        class FakeResp:
            status_code = 200

            def raise_for_status(self):
                pass

        monkeypatch.setattr(checks.httpx, "post", lambda *a, **k: FakeResp())
        r = checks.check_rerank(_cfg(rerank_base_url="http://x/v1", rerank_model="m"))
        assert r.status == "ok" and "http://x/v1" in r.detail

    def test_configured_endpoint_down_warns(self, monkeypatch):
        """Never 'fail': a down reranker degrades to the fused pool's order."""
        import silica.onboarding.checks as checks

        def boom(*a, **k):
            raise checks.httpx.ConnectError("refused")

        monkeypatch.setattr(checks.httpx, "post", boom)
        r = checks.check_rerank(_cfg(rerank_base_url="http://x/v1", rerank_model="m"))
        assert r.status == "warn" and "unreachable" in r.detail


class TestCheckLanguage:
    def _seed_italian_notes(self, tmp_path):
        (tmp_path / "n1.md").write_text(
            "Questo è un appunto scritto in italiano con molte parole comuni "
            "come il, la, di, che, per, con, sono, questo, quella.",
            encoding="utf-8",
        )
        (tmp_path / "n2.md").write_text(
            "Un altro appunto: la nota descrive come e perché il sistema funziona, "
            "con esempi e osservazioni sulla struttura.",
            encoding="utf-8",
        )

    def _store_with_lang(self, index_path, lang):
        from silica.kernel.recall.cooccurrence import CooccurStore

        store = CooccurStore(path=index_path, lang=lang)
        store.upsert_note("n1", {"nodes": {}, "edges": []})
        store.save()

    def test_no_vault_is_ok(self):
        from silica.onboarding.checks import check_language
        r = check_language(_cfg(vault_path=""))
        assert r.status == "ok"
        assert "no vault" in r.detail

    def test_no_notes_is_ok(self, tmp_path):
        from silica.onboarding.checks import check_language
        r = check_language(_cfg(vault_path=str(tmp_path)))
        assert r.status == "ok"
        assert "no notes" in r.detail

    def test_no_store_is_ok_and_names_detected_language(self, tmp_path, monkeypatch):
        import silica.kernel.recall.cooccurrence as cooc_mod
        from silica.onboarding.checks import check_language

        self._seed_italian_notes(tmp_path)
        monkeypatch.setattr(cooc_mod, "_index_path_for", lambda vault: tmp_path / "no_such_store.json")

        r = check_language(_cfg(vault_path=str(tmp_path)))
        assert r.status == "ok"
        assert "italian" in r.detail
        assert "no store" in r.detail

    def test_matching_store_is_ok(self, tmp_path, monkeypatch):
        import silica.kernel.recall.cooccurrence as cooc_mod
        from silica.onboarding.checks import check_language

        self._seed_italian_notes(tmp_path)
        index_path = tmp_path / "cooc.json"
        monkeypatch.setattr(cooc_mod, "_index_path_for", lambda vault: index_path)
        self._store_with_lang(index_path, "italian")

        r = check_language(_cfg(vault_path=str(tmp_path)))
        assert r.status == "ok"
        assert "language=italian" in r.detail
        assert "store=italian" in r.detail

    def test_declared_language_supersedes_misfiring_detection(self, tmp_path, monkeypatch):
        """User's bug: a vault DECLARES italian in vault.yaml and its store is
        frozen italian, but a frontmatter-heavy sample makes `detect` say
        english. The declaration is authority — no false 'mismatch' warning."""
        import silica.kernel.recall.cooccurrence as cooc_mod
        from silica.onboarding.checks import check_language, detect_vault_language

        (tmp_path / "n.md").write_text(
            "---\nlast: 2026-07-09\nrelated:\n  - null\n---\nappunto italiano",
            encoding="utf-8",
        )
        # Sanity: without the declaration, this sample is a real detection trap.
        assert detect_vault_language(str(tmp_path)) == "english"
        (tmp_path / "vault.yaml").write_text("cooccurrence_lang: italian\n", encoding="utf-8")
        index_path = tmp_path / "cooc.json"
        monkeypatch.setattr(cooc_mod, "_index_path_for", lambda vault: index_path)
        self._store_with_lang(index_path, "italian")

        r = check_language(_cfg(vault_path=str(tmp_path)))
        assert r.status == "ok"
        assert "language=italian" in r.detail

    def test_mismatched_store_warns_and_suggests_cooccur(self, tmp_path, monkeypatch):
        import silica.kernel.recall.cooccurrence as cooc_mod
        from silica.onboarding.checks import check_language

        self._seed_italian_notes(tmp_path)
        index_path = tmp_path / "cooc.json"
        monkeypatch.setattr(cooc_mod, "_index_path_for", lambda vault: index_path)
        self._store_with_lang(index_path, "english")

        r = check_language(_cfg(vault_path=str(tmp_path)))
        assert r.status == "warn"
        assert "italian" in r.detail and "english" in r.detail
        assert "/cooccur" in r.hint

    def test_corrupt_store_degrades_to_ok_no_traceback(self, tmp_path, monkeypatch):
        import silica.kernel.recall.cooccurrence as cooc_mod
        from silica.onboarding.checks import check_language

        self._seed_italian_notes(tmp_path)
        index_path = tmp_path / "cooc.json"
        index_path.write_text("not json", encoding="utf-8")
        monkeypatch.setattr(cooc_mod, "_index_path_for", lambda vault: index_path)

        r = check_language(_cfg(vault_path=str(tmp_path)))
        assert r.status == "ok"
        assert "italian" in r.detail

    def test_does_not_cross_check_a_different_vaults_frozen_store(self, tmp_path, monkeypatch):
        """Regression for the split-source-of-truth bug: check_language(config) must
        resolve BOTH halves from config.vault_path, never from the global CONFIG
        singleton. Simulates the wizard's step 6 (`run_checks(SilicaConfig())` right
        after a vault switch): global CONFIG still points at an OLD vault with a
        store frozen "english"; the freshly-built `config` passed in points at a
        DIFFERENT, brand-new Italian vault with no store of its own yet. The old
        vault's frozen store must never leak into this vault's verdict.
        """
        import silica.kernel.recall.cooccurrence as cooc_mod
        from silica.config import CONFIG
        from silica.onboarding.checks import check_language

        old_vault = tmp_path / "old_vault"
        old_vault.mkdir()
        old_index_path = tmp_path / "old_cooc.json"
        no_store_path = tmp_path / "no_such_store_for_new_vault.json"
        monkeypatch.setattr(
            cooc_mod, "_index_path_for",
            lambda vault: old_index_path if vault == str(old_vault) else no_store_path,
        )
        self._store_with_lang(old_index_path, "english")
        monkeypatch.setattr(CONFIG, "vault_path", str(old_vault))

        new_vault = tmp_path / "new_vault"
        new_vault.mkdir()
        self._seed_italian_notes(new_vault)

        r = check_language(_cfg(vault_path=str(new_vault)))
        # Must NOT report a mismatch by comparing against old_vault's "english"
        # store — new_vault has no store of its own, so this is the "no store
        # frozen yet" ok state, not a false warn.
        assert r.status == "ok"
        assert "italian" in r.detail
        assert "english" not in r.detail


class TestSampleVaultTextSpread:
    """Finding 2 (final multilingua review): the char budget must be spread
    across up to _LANG_SAMPLE_MAX_FILES files, not exhausted by the first
    handful of alphabetically-sorted ones — otherwise an alphabetical head of
    minority-language files (e.g. "AAA api notes.md") mis-reports the vault's
    dominant language.
    """

    @staticmethod
    def _gen(words: list[str], n: int, seed: int) -> str:
        import random
        rng = random.Random(seed)
        return " ".join(rng.choice(words) for _ in range(n))

    def test_alphabetical_head_minority_does_not_dominate_detection(self, tmp_path):
        from silica.onboarding.checks import detect_vault_language

        en_words = [
            "the", "company", "report", "market", "update", "system", "project",
            "team", "review", "plan", "with", "for", "and", "that", "this",
            "from", "have", "will", "not", "are",
        ]
        it_words = [
            "della", "azienda", "progetto", "sistema", "squadra", "relazione",
            "mercato", "aggiornamento", "con", "per", "che", "questo", "dal",
            "hanno", "sono", "del", "alla", "nella", "sulla", "non",
        ]
        # 4 English files sort first alphabetically, each long enough to fully
        # consume the OLD per-file cap (1000 chars) on their own.
        for i in range(4):
            (tmp_path / f"a{i}_notes.md").write_text(
                self._gen(en_words, 300, seed=i), encoding="utf-8",
            )
        # A larger population of Italian notes sorting after them — the
        # actual majority of the vault.
        for i in range(10):
            (tmp_path / f"z_nota_{i}.md").write_text(
                self._gen(it_words, 300, seed=100 + i), encoding="utf-8",
            )

        assert detect_vault_language(str(tmp_path)) == "italian"


def test_render_report_does_not_eat_bracketed_text(capsys):
    """rich reads a bare [word] as a style tag: unescaped, `silica[rerank]` renders
    as `silica` and the hint tells the user to run the wrong command."""
    from silica.onboarding.checks import CheckResult, render_report

    render_report([CheckResult("rerank", "warn", "disabled", "pip install silica[rerank]")])
    out = capsys.readouterr().out
    assert "silica[rerank]" in out.replace("\n", "")


class TestAggregation:
    def test_run_checks_returns_every_check(self, monkeypatch, tmp_path):
        import silica.onboarding.checks as checks

        def boom(url, timeout):
            raise checks.httpx.ConnectError("refused")

        monkeypatch.setattr(checks.httpx, "get", boom)
        monkeypatch.setattr(checks.gitstate, "find_repo_root", lambda p: None)
        results = checks.run_checks(_cfg(vault_path=str(tmp_path)))
        assert [r.name for r in results] == [
            "chat model", "chat endpoint", "vault", "vault manifest",
            "language", "embeddings", "rerank", "quarantine", "OKF §11",
            "session capture", "own sessions",
        ]

    def test_check_quarantine_surfaces_corrupt_files(self, tmp_path):
        from silica.onboarding.checks import check_quarantine

        assert check_quarantine(_cfg(vault_path=str(tmp_path))).status == "ok"
        (tmp_path / "provenance.json.corrupt.20260710T120000").write_text("junk")
        result = check_quarantine(_cfg(vault_path=str(tmp_path)))
        assert result.status == "warn"
        assert "provenance.json.corrupt.20260710T120000" in result.detail

    def test_check_okf_reports_the_census(self, tmp_path):
        """Doctor renders the §11 walker: silent when the vault is a bundle."""
        from silica.onboarding.checks import check_okf

        (tmp_path / "clean.md").write_text("---\ntype: Note\n---\n\nB\n", encoding="utf-8")
        assert check_okf(_cfg(vault_path=str(tmp_path))).status == "ok"

        # Plain markdown (a repo-mode README) is counted, never warned about.
        (tmp_path / "README.md").write_text("# Just prose\n", encoding="utf-8")
        assert check_okf(_cfg(vault_path=str(tmp_path))).status == "ok"

        (tmp_path / "legacy.md").write_text("---\ntitle: X\n---\n\nB\n", encoding="utf-8")
        result = check_okf(_cfg(vault_path=str(tmp_path)))
        assert result.status == "warn"
        assert "legacy.md" in result.detail and "§11.2" in result.detail
        assert "backfill_notetype" in result.hint

    def test_has_failures(self):
        from silica.onboarding.checks import CheckResult, has_failures
        ok = CheckResult("a", "ok", "")
        warn = CheckResult("b", "warn", "")
        fail = CheckResult("c", "fail", "")
        assert not has_failures([ok, warn])
        assert has_failures([ok, fail])

    def test_render_report_smoke(self, monkeypatch):
        import io

        from rich.console import Console

        import silica.onboarding.checks as checks
        from silica.ui import console as console_mod

        buf = io.StringIO()
        monkeypatch.setattr(console_mod, "CONSOLE", Console(file=buf, highlight=False, width=120))
        checks.render_report([
            checks.CheckResult("chat model", "ok", "qwen3-30b via lmstudio"),
            checks.CheckResult("vault", "fail", "missing", "run `silica init`"),
        ])
        out = buf.getvalue()
        assert "chat model" in out
        assert "silica init" in out


class TestOllamaContextCheck:
    """Ollama silently truncates past its window; the doctor is where the user
    learns the pinned window cannot hold a turn before answers go quietly wrong."""

    def _check(self, window, monkeypatch):
        import silica.onboarding.checks as checks
        monkeypatch.setattr(checks, "model_limits", lambda p, m: (window, 0))
        return checks.check_ollama_context(_cfg(model="ollama/llama3.2:3b"))

    def test_window_below_one_turn_warns(self, monkeypatch):
        r = self._check(4096, monkeypatch)
        assert r.status == "warn"
        assert "4096" in r.detail
        assert "OLLAMA_NUM_CTX" in r.hint

    def test_roomy_window_is_ok(self, monkeypatch):
        assert self._check(32768, monkeypatch).status == "ok"

    def test_unreadable_window_is_unknown_with_a_pull_hint(self, monkeypatch):
        """The check could not read the window; it is not reporting a problem
        with the window, so it must not spend a warning line on it."""
        r = self._check(0, monkeypatch)
        assert r.status == "unknown"
        assert "llama3.2:3b" in r.hint  # the ollama/ prefix is stripped for the pull command

    def test_registered_only_for_ollama(self, monkeypatch):
        import silica.onboarding.checks as checks

        monkeypatch.setattr(checks, "check_chat_endpoint",
                            lambda c: checks.CheckResult("chat endpoint", "ok", ""))
        monkeypatch.setattr(checks, "model_limits", lambda p, m: (4096, 0))
        assert "ollama context" not in [
            r.name for r in checks.run_checks(_cfg(model="lmstudio/qwen3-8b"))
        ]
        assert "ollama context" in [
            r.name for r in checks.run_checks(_cfg(model="ollama/llama3.2:3b"))
        ]


class TestCaptureHook:
    """`silica doctor` tells you when session capture is not wired up."""

    def _config(self, vault):
        from silica.config import SilicaConfig
        return SilicaConfig(vault_path=str(vault))

    def test_missing_hook_is_a_warning_carrying_the_snippet(self, tmp_path, monkeypatch):
        from silica.onboarding.checks import check_capture_hook

        monkeypatch.setenv("HOME", str(tmp_path))
        vault = tmp_path / "vault"
        vault.mkdir()
        result = check_capture_hook(self._config(vault))

        assert result.status == "warn"
        assert "silica capture" in result.hint
        assert "SessionEnd" in result.hint

    def test_registered_hook_passes(self, tmp_path, monkeypatch):
        import json

        from silica.onboarding.checks import check_capture_hook

        monkeypatch.setenv("HOME", str(tmp_path))
        vault = tmp_path / "vault"
        (vault / ".claude").mkdir(parents=True)
        (vault / ".claude" / "settings.json").write_text(json.dumps({
            "hooks": {"SessionEnd": [
                {"hooks": [{"type": "command", "command": "silica capture"}]}]}
        }), encoding="utf-8")

        assert check_capture_hook(self._config(vault)).status == "ok"


class TestOwnSessionCapture:
    """The knob for Silica's own sessions is off by default, and says so."""

    def _config(self, **kw):
        from silica.config import SilicaConfig
        return SilicaConfig(**kw)

    def test_the_knob_being_off_is_a_hint_not_a_failure(self):
        from silica.onboarding.checks import check_session_capture

        result = check_session_capture(self._config(capture_sessions=False))

        assert result.status == "ok"  # off is a valid choice, not a defect
        assert "SILICA_CAPTURE_SESSIONS" in result.hint

    def test_opted_in_says_where_the_facts_go(self):
        from silica.onboarding.checks import check_session_capture

        result = check_session_capture(self._config(capture_sessions=True))

        assert result.status == "ok"
        assert "episodic" in result.detail.lower()


def test_check_quarantine_sees_cross_vault_state_in_home(tmp_path, monkeypatch):
    """undo_journal.db and checkpoints live at ~/.silica, not under the vault:
    their quarantined copies were invisible to the only surface that reports
    quarantines."""
    import silica.onboarding.checks as checks

    monkeypatch.setenv("HOME", str(tmp_path))
    silica_home = tmp_path / ".silica"
    silica_home.mkdir()
    (silica_home / "undo_journal.db.corrupt.20260802T000000").write_bytes(b"x")

    r = checks.check_quarantine(_cfg())
    assert r.status == "warn"
    assert "undo_journal" in r.detail
