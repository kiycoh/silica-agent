# Silica Agent

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-22d3ee.svg)](https://www.python.org/)
[![Obsidian Compatible](https://img.shields.io/badge/Obsidian-Compatible-38afef.svg)](https://obsidian.md/)
[![License: AGPL 3.0](https://img.shields.io/badge/License-AGPL_v3-4d8af0.svg)](https://opensource.org/licenses/AGPL-3.0)
[![Powered by UV](https://img.shields.io/badge/package--manager-uv-6366f1.svg)](https://github.com/astral-sh/uv)

<p align="center">
  <img src="docs/assets/sili_no_bg.png" alt="Silica Mascot Sili" width="250" />
</p>

> **Silica** is a CLI-based agent designed for automated curation, organization and **safe** knowledge management. Local-first and open-source. Supports Obsidian.

---

## Table of Contents
- [System Overview](#system-overview)
- [Target Audience](#target-audience)
- [Architecture](#architecture)
- [Use Cases](#use-cases)
- [Configuration](#configuration)
- [Quick Start](#quick-start)
  - [Installation](#installation)
  - [Setup](#setup)
  - [Execution](#execution)
  - [REPL Commands](#repl-commands)
  - [System Tools](#system-tools)
- [Directory Structure](#directory-structure)
- [Engineering Decisions and Trade-offs](#engineering-decisions-and-trade-offs)
- [License](#license)

---

## System Overview

Silica is a CLI-based deterministic agent orchestrator that can manage Obsidian vaults, codebases (wip) and any other directory. 

- Silica is ***local-first*** (LM Studio, Ollama), OpenRouter is also supported.
- **Silica prevents the risk of vault corruption and structural cluttering** by using safety-hardened tools and rollbacks.
- Silica maintains and updates **a vault index separate from your files.**
- Silica is not a free-form agent orchestrator.

---

### Execution Models

The underlying toolset is accessed via two distinct execution flows depending on the level of autonomy required:

* **Conversational Agent (REPL):** A high-autonomy LLM loop designed for interactive note discovery and ad-hoc operations. Runs step-by-step reasoning but is bound by mechanical invariants embedded directly inside the Python tools.
* **Deterministic Pipelines (FSM):** Zero-autonomy state machines executing fixed recipes (e.g., importing/injecting new materials). Applies strict validation gates (orphan checks, unresolved link checks, backlink counts) and automatically executes rollbacks if gates fail.

---

## Use Cases

1. **Automated Inbox Ingestion**
   Reads raw clippings and drafts from an inbox directory, distills them into atomic markdown concepts, resolves duplicate matches against the existing vault, and writes them safely.
2. **Conversational Vault Querying**
   Allows users to query their notes, map paths across the graph, and generate outlines or synthesis documents using semantic search and graph-traversal tools in the REPL.
3. **Graph-Safe Note Refactoring**
   Handles complex merges and splits of concept notes. Redirects incoming links automatically to prevent broken references or orphaned files.

---

## Configuration

Configure the agent via environment variables (e.g., in a `.env` file):

| Variable | Default | Description |
| :--- | :--- | :--- |
| `SILICA_MODEL` | *(none — set via `silica init`)* | Chat LLM model identifier (e.g., loaded in LM Studio or from OpenRouter) |
| `SILICA_PROVIDER` | `derived` *(set via `silica init`)* | Chat provider preset: `lmstudio` or `openrouter` |
| `OPENROUTER_API_KEY` | *(none — set via `silica init`)* | Required when the provider is `openrouter` |
| `SILICA_VAULT` | *(unset — set via `silica init`)* | Vault path for the filesystem backend (or repo mode: `.silica/` in git root) |
| `SILICA_BACKEND` | `fs` *(set via `silica init`)* | `fs` (headless filesystem) or `cli` (live Obsidian desktop via CDP — adds rollback + live cache) |
| `SILICA_INBOX_DIR` | `Inbox` | Name of the inbox folder inside the vault for staging files |
| `SILICA_EMBEDDING_MODEL` | `qwen3-embedding-4b` *(set via `silica init`)* | Embedding model identifier used for semantic tasks |
| `SILICA_EMBEDDING_BASE_URL` | `http://localhost:1234/v1` *(set via `silica init`)* | Embedding API endpoint |
| `SILICA_EMBEDDING_API_KEY` | `lm-studio` *(set via `silica init`)* | Embedding API key |
| `SILICA_WORKER_MODEL` | *(none)* | Sub-agent worker model (e.g., small local model for dedup / refinement) |
| `SILICA_WORKER_PROVIDER` | `lmstudio` | Provider preset for the sub-agent worker model |
| `SILICA_WORKER_API_KEY` | *(none)* | API key for the worker model |
| `SILICA_SUBAGENT_MAX_CONCURRENT` | `3` | Maximum concurrent sub-agent execution threads |
| `SILICA_WORKER_MAX_CONCURRENT` | `4` | Global ceiling on concurrent worker-model LLM calls |
| `SILICA_TAVILY_API_KEY` | *(none)* | API key for Tavily search (enables `/web-search` command) |
| `SILICA_PDF_PROVIDER` | `pymupdf4llm` | PDF-to-Markdown converter: `pymupdf4llm` or `mineru` (OCR tool) |
| `SILICA_MAX_CONTEXT` | *(auto)* | REPL context meter budget — pin explicitly, or auto-detected from the provider (LM Studio loaded window / OpenRouter context_length; fallback `60000`) |
| `SILICA_SHOW_THINKING` | `True` | Toggle printing of LLM thinking/reasoning blocks |
| `SILICA_TOOL_PROGRESS` | `all` | CLI tool display level: `off`, `new`, `all`, or `verbose` |
| `SILICA_SHOW_BANNER` | `True` | Startup banner art (`True` → wordmark, `False` → plain one-liner) |
| `SILICA_SIM_THRESHOLD_HIGH` | `0.85` | Cosine similarity threshold for merging/patching notes |
| `SILICA_SIM_THRESHOLD_LOW` | `0.65` | Cosine similarity threshold for creating new notes |
| `SILICA_SIM_TITLE_THRESHOLD` | `0.80` | Title similarity threshold for dedup promotion |
| `SILICA_DEDUP_SCAN_K` | `5` | Candidate notes retrieved per note during dedup scans |
| `SILICA_COOCCURRENCE_LANG` | `auto` | Language for co-occurrence graph (`auto` detects language) |
| `SILICA_SIM_THRESHOLD_THEME` | `0.35` | Minimum cosine similarity to vault theme for salience |
| `SILICA_GIT_COMMIT` | `off` | Git commit safety net for vault writes (`off`, `auto`) |
| `SILICA_OBSIDIAN_CLI_TIMEOUT` | `8.0` | Timeout in seconds for Obsidian desktop app CDP calls |
| `SILICA_VERBOSE` | `False` | Enables verbose debug outputs to stderr |

---

## Quick Start

### Installation
Clone the repository and install it in editable mode:

```bash
git clone https://github.com/kiycoh/silica-agent.git
cd silica-agent
uv pip install -e .
```

### Setup

Run the interactive wizard — it writes your `.env` (vault, backend, chat provider, embeddings) and finishes with a diagnostic report:

```bash
uv run silica init
```

Re-check the environment at any time:

```bash
uv run silica doctor
```

### Execution

Start the interactive REPL:

```bash
uv run silica
```

Run the ingestion pipeline from inside the REPL:

```
/ingest Inbox/note.md --target=Concepts/AI
```

### REPL Commands

**Workflow** — agent-directed:

| Command | Usage | Description |
| :--- | :--- | :--- |
| `/report` | `[folder] [--top-k=N] [--embeddings]` | Structural audit of the vault (hubs, bridges, orphans). Pauses for confirmation. |
| `/ingest` | `<file...> [--target=DIR] [--hub=H]` | Bring files in: notes via Injector FSM, code as skeleton stubs |
| `/organize` | `"<intent>" [--scope=FOLDER] [--file=taxonomy.yaml] [--merge] [--move-uncategorized] [--apply]` | Classify and reorganize vault notes according to a taxonomy |

**Direct** — immediate, no LLM round-trip:

| Command | Usage | Description |
| :--- | :--- | :--- |
| `/status` | `[run_id]` | Progress digest of the last run |
| `/convert` | `<file...> [--target=DIR]` | Transcode a non-`.md` file (PDF) into a markdown note in the inbox |
| `/web-search` | `"<concept>" [--max-searches=N]` | Research a concept on the web → cited findings note in the Inbox (then `/ingest`) |
| `/embed` | `[folder] [--force]` | Build/update the embedding index |
| `/cooccur` | `[folder] [--force]` | Build/update the co-occurrence index (no embedder needed) |
| `/graph` | `[out.html] [folder]` | Export the knowledge graph |
| `/find` | `<query> [--k=N]` | Semantic search |
| `/undo` | `[note-path]` | Undo the last patch on a note |
| `/review` | `[--flush=HASH]` | Inspect the async review queue (deferred ops) |
| `/revert` | `[run-id]` | Revert a whole injection (per-run, LIFO) |
| `/dedup` | `[folder]` | Deduplicate notes (sub-agent) |
| `/curate` | `[folder] [--apply]` | Curate the vault: plan autolink/orphan/dedup/refine work (dry-run; `--apply` executes) |
| `/refine` | `[folder]` | Enrich and normalize notes (sub-agent) |
| `/enrich` | `[folder]` | Enrich note semantics (sub-agent) |
| `/stale` | | List notes whose `documents:` paths have new commits since `code_ref` |
| `/plans` | | List `plans/` notes grouped by `status:` |


**System:** `/help` · `/model` · `/tools` · `/clear` · `/verbose` · `/thinking` · `/vault` `[path]` (show or switch the active vault for this session) · `/exit`
---

## Performance, Quirks & Features

Here are some of the noteworthy performance traits and architectural behaviors of Silica:

* **Token-Efficient Vault Auditing (`/report`)**: Computes community detection clusters (using Louvain modularity), detects god-nodes (high-degree hubs), structural bridges (inter-community connectors), and orphans. It is capable of auditing and building a full structural remediation plan for a vault of **1,000+ markdown files in under 10 seconds**.
* **Parallel Worker Sub-Agents**: Cognitive-heavy, long-running batch operations like semantic deduplication (`/dedup`) and detail refinement (`/refine` or `/enrich`) are offloaded to leashed sub-agents. These run concurrently (up to `SILICA_SUBAGENT_MAX_CONCURRENT`) on a separate worker model (e.g., a small local model like `SILICA_WORKER_MODEL`), keeping the main model's context window clean and free.
* **Embedder-Free Concept Modeling**: If an embedding model is offline or unconfigured, Silica's concept matching degrades gracefully. It utilizes a deterministic, local co-occurrence concept graph (`/cooccur`) to query relatedness and label communities in `/graph` exports without making network calls or LLM API queries.
* **Strict Zero-Trust Staging**: Web search queries (`/web-search`) write findings exclusively into the inbox directory (`Inbox/`). External web-content is never injected directly into the active knowledge vault without explicit human staging and FSM ingestion review.
* **Git Safety Net**: If `SILICA_GIT_COMMIT=auto` is enabled, Silica automatically commits touched paths to Git after each successful write batch, creating a history checkpoint alongside the interactive `/undo` and `/revert` features.


## License

This project is licensed under the **GNU Affero General Public License v3.0** (AGPL-3.0).

See [LICENSE](LICENSE) for details.
