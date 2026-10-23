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
