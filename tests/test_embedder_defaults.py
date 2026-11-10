# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The cosine thresholds and role prompts follow the configured embedder
(ADR-0078): EmbeddingGemma 2's cosines sit far above Qwen3-Embedding's, so a
bar set on one would merge half the vault under the other."""
from silica.config import DEFAULT_EMBEDDING_MODEL, SilicaConfig


def _clear(monkeypatch):
    for key in ("SILICA_EMBEDDING_MODEL", "SILICA_EMBEDDING_QUERY_PREFIX", "SILICA_EMBEDDING_DOCUMENT_PREFIX",
                "SILICA_SIM_THRESHOLD_HIGH", "SILICA_SIM_THRESHOLD_LOW", "SILICA_SIM_TITLE_THRESHOLD",
                "SILICA_SIM_THRESHOLD_THEME", "SILICA_AUTOLINK_MIN_SIM", "SILICA_EPISODIC_RECALL_FLOOR"):
        monkeypatch.delenv(key, raising=False)


def test_default_embedder_brings_its_own_prompts_and_thresholds(monkeypatch):
    _clear(monkeypatch)
    c = SilicaConfig()
    assert c.embedding_model == DEFAULT_EMBEDDING_MODEL == "embeddinggemma-2"
    assert c.embedding_query_prefix == "task: search result | query: "
    assert c.embedding_document_prefix == "title: none | text: "
    assert (c.sim_threshold_high, c.sim_threshold_low) == (0.911, 0.844)
    assert c.sim_title_threshold == 0.876 and c.sim_threshold_theme == 0.708
    assert c.autolink_min_sim == 0.654 and c.episodic_recall_floor == 0.724


def test_qwen_keeps_the_thresholds_it_was_measured_with(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SILICA_EMBEDDING_MODEL", "text-embedding-qwen3-embedding-0.6b")
    c = SilicaConfig()
    assert (c.embedding_query_prefix, c.embedding_document_prefix) == ("", "")
    assert (c.sim_threshold_high, c.sim_threshold_low, c.sim_title_threshold) == (0.85, 0.70, 0.80)
    assert (c.sim_threshold_theme, c.autolink_min_sim, c.episodic_recall_floor) == (0.35, 0.30, 0.5)


def test_jina_gets_its_own_bars(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SILICA_EMBEDDING_MODEL", "jina-embeddings-v5-text-small-retrieval")
    c = SilicaConfig()
    assert (c.embedding_query_prefix, c.embedding_document_prefix) == ("Query: ", "Document: ")
    assert (c.sim_threshold_high, c.sim_threshold_low, c.sim_title_threshold) == (0.814, 0.71, 0.709)
    assert (c.sim_threshold_theme, c.autolink_min_sim, c.episodic_recall_floor) == (0.208, 0.180, 0.5)


def test_pplx_4b_gets_its_own_bars_and_the_0_6b_does_not(monkeypatch):
    """The hosted index (2026-10-23): no role prompts, cosines below Qwen's.
    The 0.6b shares the id's prefix but was never calibrated, so it must not
    inherit the 4b's bars."""
    from silica.config import embedder_defaults

    _clear(monkeypatch)
    monkeypatch.setenv("SILICA_EMBEDDING_MODEL", "perplexity/pplx-embed-v1-4b")
    c = SilicaConfig()
    assert (c.embedding_query_prefix, c.embedding_document_prefix) == ("", "")
    assert (c.sim_threshold_high, c.sim_threshold_low, c.sim_title_threshold) == (0.785, 0.640, 0.508)
    assert (c.sim_threshold_theme, c.autolink_min_sim, c.episodic_recall_floor) == (0.189, 0.204, 0.52)
    assert embedder_defaults("perplexity/pplx-embed-v1-4b")["dim"] == "2560"
    assert embedder_defaults("perplexity/pplx-embed-v1-0.6b") == {}


def test_an_exported_value_wins_even_empty(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SILICA_SIM_THRESHOLD_HIGH", "0.95")
    monkeypatch.setenv("SILICA_EMBEDDING_QUERY_PREFIX", "")
    c = SilicaConfig()
    assert c.sim_threshold_high == 0.95
    assert c.embedding_query_prefix == ""
    assert c.sim_threshold_low == 0.844  # the rest still follow the embedder


def test_a_swap_brings_the_new_embedders_prefixes_and_bars(monkeypatch):
    _clear(monkeypatch)
    c = SilicaConfig()
    c.embedding_model = "perplexity/pplx-embed-v1-4b"
    assert (c.embedding_query_prefix, c.embedding_document_prefix) == ("", "")
    assert (c.sim_threshold_high, c.sim_threshold_low, c.sim_title_threshold) == (0.785, 0.640, 0.508)
    assert (c.sim_threshold_theme, c.autolink_min_sim, c.episodic_recall_floor) == (0.189, 0.204, 0.52)
    c.embedding_model = "text-embedding-qwen3-embedding-0.6b"  # no row: the Qwen-era defaults
    assert (c.sim_threshold_high, c.sim_threshold_low, c.sim_title_threshold) == (0.85, 0.70, 0.80)
    assert (c.sim_threshold_theme, c.autolink_min_sim, c.episodic_recall_floor) == (0.35, 0.30, 0.5)


def test_a_pin_outranks_the_row_across_a_swap(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("SILICA_SIM_THRESHOLD_HIGH", "0.95")
    monkeypatch.setenv("SILICA_EMBEDDING_QUERY_PREFIX", "")
    c = SilicaConfig()
    c.autolink_min_sim = 0.1
    c.embedding_model = "jina-embeddings-v5-text-small"
    assert (c.sim_threshold_high, c.embedding_query_prefix, c.autolink_min_sim) == (0.95, "", 0.1)
    assert c.sim_threshold_low == 0.71 and c.embedding_document_prefix == "Document: "


def test_a_setenv_after_the_config_is_built_does_not_reach_a_threshold(monkeypatch):
    _clear(monkeypatch)
    c = SilicaConfig()
    monkeypatch.setenv("SILICA_SIM_THRESHOLD_HIGH", "0.95")
    assert c.sim_threshold_high == 0.911


def test_mock_patch_leaves_the_derived_value_in_place(monkeypatch):
    from unittest.mock import patch

    _clear(monkeypatch)
    c = SilicaConfig()
    with patch.object(c, "sim_threshold_low", 0.1):
        assert c.sim_threshold_low == 0.1
    assert c.sim_threshold_low == 0.844


def test_the_panels_swap_reindexes_under_the_new_document_prefix(monkeypatch, tmp_path):
    """B5: the swap assigned the id alone, so the reindex embedded under the old
    embedder's prefix and the next boot, reading the id's own, re-embedded it all."""
    import pytest

    pytest.importorskip("fastapi")
    from silica.config import CONFIG
    from silica.kernel.recall import refresh
    from silica.model.providers import get_embedder
    from silica.ui.web import settings_routes

    monkeypatch.setattr("silica.kernel.base.user_env.resolve_env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(CONFIG, "embedding_model", "embeddinggemma-2")
    assert CONFIG.embedding_document_prefix == "title: none | text: "
    seen: dict = {}

    def reindex(*_a, **_kw):
        embedder = get_embedder(CONFIG)
        seen.update(model=embedder.model, document=embedder.document_prefix, query=embedder.query_prefix)
        return {}

    monkeypatch.setattr(refresh, "embed", reindex)
    result = settings_routes._apply_embedding_swap("SILICA_EMBEDDING_MODEL", "jina-embeddings-v5-text-small")
    assert result["ok"]
    assert seen == {"model": "jina-embeddings-v5-text-small", "document": "Document: ", "query": "Query: "}


def test_a_reindex_that_raises_is_said_in_reindex_and_the_swap_stands(monkeypatch, tmp_path):
    """W7: the swap called the tool, whose run() turned any exception into its error
    shape; it calls refresh.embed now, and a raise must not read as a failed swap."""
    import pytest

    pytest.importorskip("fastapi")
    from silica.config import CONFIG
    from silica.kernel.recall import refresh
    from silica.ui.web import settings_routes

    monkeypatch.setattr("silica.kernel.base.user_env.resolve_env_path", lambda: tmp_path / ".env")
    monkeypatch.setattr(CONFIG, "embedding_model", "embeddinggemma-2")

    def broken(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(refresh, "embed", broken)
    result = settings_routes._apply_embedding_swap("SILICA_EMBEDDING_MODEL", "jina-embeddings-v5-text-small")
    assert result["ok"] and result["reindex"] == {"error": "OSError: disk full"}
