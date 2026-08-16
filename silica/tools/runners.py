# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Runner tools — whole pipelines and leashed sub-agent batches.

Full FSM runs (injector, organizer), taxonomy generation, run-ledger
inspection, and the dedup/refine/enrich sub-agent passes.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from silica.driver import DRIVER
from silica.tools import tool
from silica.tools.graph import _in_folder


class RunInjectorArgs(BaseModel):
    inbox_file: str = Field(default="", description="Path to a single inbox file (legacy; use inbox_files for multiple files)")
    inbox_files: list[str] = Field(default_factory=list, description="Paths to one or more inbox files to nucleate in a single run")
    target_dir: str = Field(description="Destination directory for the extracted concepts")
    hub: str = Field(default="", description="Optional reference hub note")
    resume_run_id: str = Field(default="", description="Run ID to resume (re-processes only failed chunks, skips done ones)")

@tool(RunInjectorArgs, cls="composed", collapse="eager")
def silica_run_injector(
    inbox_file: str = "",
    inbox_files: list[str] | None = None,
    target_dir: str = "",
    hub: str = "",
    resume_run_id: str = "",
    cancel_token: Any = None,
) -> dict[str, Any]:
    """Nucleate one or more inbox files into the vault — the full pipeline with
    quality gates and rollback. This is THE tool for "nucleate/inject this file".

    Per-chunk failure containment: a failed chunk is rolled back and marked
    'failed' while the remaining chunks continue. Pass resume_run_id to re-run
    only the chunks that failed in a previous partial run. Near-duplicate
    merging runs concurrently in the background.
    For a single quick note, silica_write_note/silica_patch_note are cheaper.
    """
    from silica.router.coordinator import Coordinator

    files: list[str] = list(inbox_files or [])
    if inbox_file and inbox_file not in files:
        files.insert(0, inbox_file)
    if not files:
        return {"error": "No inbox file(s) specified"}

    # A target folder is not optional: the distiller prompt interpolates it into
    # {TARGET} and derives {HUB_NAME} from it, so an empty one renders the two
    # placeholders literally. The model then obeys "hub MUST be exactly
    # {HUB_NAME}" to the letter and every op comes back with hub="{HUB_NAME}"
    # and path="/<title>.md" — all rejected by lint, after a full LLM run, and
    # the hub auto-creator drops a junk `{HUB_NAME}.md` in the vault root. The
    # CLI always resolves a target before dispatching (cli.py:1226); this guard
    # covers the agent-tool path, whose contract already says to pass one.
    # Asked before conversion: a PDF is minutes of work to transcode and it
    # would all be thrown away here.
    if not target_dir.strip():
        return {"error": "No target_dir specified. Pick the vault folder for these notes and pass it as target_dir."}

    # The FSM re-reads inbox files as prose, so a file no adapter claims (PDF,
    # EPUB, other binaries) has to be transcoded first. `/nucleate` has always
    # done that inline; the agent tool used to answer "ask the user to run
    # /convert", which in a REPL means telling the user to type, themselves, the
    # thing they just asked for — and in a library of 74 scanned books that
    # instruction stands between the user and the entire corpus.
    from silica.kernel.vault_manifest import get_active_manifest
    from silica.sources.registry import adapter_for

    enabled = get_active_manifest().sources
    if any(adapter_for(f, enabled=enabled) is None for f in files):
        from silica.sources.convert import convert

        resolved: list[str] = []
        rejected: list[str] = []
        for f in files:
            if adapter_for(f, enabled=enabled) is not None:
                resolved.append(f)
                continue
            try:
                resolved.extend(convert(f, dest_dir=target_dir))
            except (ValueError, RuntimeError) as exc:
                rejected.append(f"{f} ({exc})")
        if rejected:
            return {"error": f"Not ingestible as-is: {'; '.join(rejected)}."}
        files = list(dict.fromkeys(resolved))
        if not files:
            return {"error": "Conversion produced no markdown to nucleate."}

    coordinator = Coordinator(
        inbox_files=files,
        target_dir=target_dir,
        hub=hub or None,
        resume_run_id=resume_run_id or None,
        cancel_token=cancel_token,
    )
    result = coordinator.run()

    # Agent-facing projection: outcomes only, never the raw FSM context. The
    # raw context once fed a confabulated success report — planned concepts in
    # payload.chunks read as "created notes", and a last-write-wins error field
    # hid 5 of 6 batch failures.
    failed = result.get("failed_chunks", [])
    projected: dict[str, Any] = {
        "final_status": result.get("final_status", "unknown"),
        "run_id": getattr(coordinator.fsm.progress, "run_id", None),
        "chunks_committed": result.get("committed_chunks", 0),
        "chunks_failed": len(failed),
        "failed_chunks": failed,
        "files_summary": result.get("files_summary", []),
        "subagents": result.get("subagents", {}),
        # What the run actually yielded. Both frontends build their completion
        # line from these; until they were projected here the keys existed only
        # inside fsm.context and every summary read "1 file · 3m12s", never the
        # note and link counts it was written to show.
        "yield_notes": result.get("yield_notes", 0),
        "yield_links": result.get("yield_links", 0),
        "files_total": len(files),
    }
    if result.get("error"):
        projected["error"] = result["error"]
    return projected


class LedgerDigestArgs(BaseModel):
    run_id: str = Field(default="", description="Run ID to inspect (latest saved run if empty)")

@tool(LedgerDigestArgs, cls="composed")
def silica_ledger_digest(run_id: str = "") -> dict[str, Any]:
    """Compact summary of a run's plan and progress (< 500 tokens).

    Use to inspect an nucleate/audit run before advancing it with
    silica_ledger_next. Pass run_id="" for the most recently saved run.
    """
    from silica.kernel.progress import ProgressLedger, latest_run_id

    resolved_id = run_id.strip() or (latest_run_id() or "")
    if not resolved_id:
        return {"error": "No runs found in ~/.silica/runs/"}

    try:
        ledger = ProgressLedger.load(resolved_id)
    except FileNotFoundError:
        return {"error": f"Run '{resolved_id}' not found"}
    except Exception as e:
        return {"error": f"Failed to load ledger: {e}"}

    return {"run_id": resolved_id, "digest": ledger.digest()}


def _scan_dedup_pairs(folder: str = "") -> tuple[list[dict], str | None]:
    """Cosine + title scan for near-duplicate note pairs — vectors only, no body
    reads. Returns (pairs, error); each pair is {source, target, score, full_score,
    title_score}. Shared by silica_dedup (sync) and the /dedup ledger seed.

    A pair is admitted when body similarity is borderline (tau_low < score <
    tau_high) OR titles are strongly similar (title_score >= tau_title) — the
    latter catches "ROS" / "JSON in ROS 2" where bodies diverge but titles are
    clearly related.
    """
    import numpy as np

    from silica.kernel.recall.embed import get_store, _cosine
    from silica.config import CONFIG as _C

    store = get_store()
    if len(store) == 0:
        return [], "Embedding index empty — run /embed first."

    τ_high = getattr(_C, "sim_threshold_high", 0.85)
    τ_low = getattr(_C, "sim_threshold_low", 0.75)
    τ_title = getattr(_C, "sim_title_threshold", 0.80)

    scope = [p for p in store.paths() if _in_folder(p, folder)]
    # One blocked matmul for the whole scope, not one matvec per note: the scan is
    # N searches over an N-row index, so the per-note shape was quadratic in vault
    # size. Same top-k (see cosine_top_k_batch); notes without a usable vector are
    # simply absent from the result, as the `if not vec: continue` did.
    neighbours = store.cosine_top_k_batch(scope, k=_C.dedup_scan_k)
    seen_pairs: set[tuple[str, str]] = set()
    pairs: list[dict] = []

    _title_cache: dict[str, Any] = {}

    def _title_vec(path: str):
        """Title vector as a float64 array, or None when the note has none.

        ponytail: memo, because the title gate below re-converted the same stored
        list on every candidate pair — 10 conversions per distinct vector, measured
        — and the conversion, not the dot product, was the cost (158 us -> 6.6 us
        per pair). float64 specifically: `_cosine` asks for float64, so asarray on
        an already-float64 array is a no-op and the score stays bit-identical; a
        float32 cache would be re-copied on every call and is slower than no cache
        at all. Drop this if the title vectors ever arrive as arrays.
        """
        if path not in _title_cache:
            raw = store.get_title_vec(path)
            _title_cache[path] = np.asarray(raw, dtype=np.float64) if raw else None
        return _title_cache[path]

    for p in scope:
        for match in neighbours.get(p, ()):
            score = match.get("score", 0.0)
            other = match.get("path", "")
            if not other or not _in_folder(other, folder):
                continue

            # Title-level similarity gate: catches pairs whose bodies diverge
            # but whose titles share a strong semantic relationship.
            title_vec_p = _title_vec(p)
            title_vec_o = _title_vec(other)
            title_score = (
                _cosine(title_vec_p, title_vec_o)
                # `is not None`, not truthiness: a numpy array raises on bool().
                if title_vec_p is not None and title_vec_o is not None
                else 0.0
            )

            in_full_window = τ_low < score < τ_high
            in_title_gate = title_score >= τ_title
            # continue (not break): candidates are score-descending; a match above
            # τ_high arrives before borderline ones — break would kill the loop early.
            if not in_full_window and not in_title_gate:
                continue

            key = tuple(sorted((p, other)))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)

            pairs.append({
                "source": p,
                "target": other,
                "score": max(score, title_score),
                "full_score": score,
                "title_score": title_score,
            })

    return pairs, None


def _pairs_to_items(pairs: list[dict]) -> list[WorkItem]:
    """Build dedup WorkItems from {source, target, score} dicts — the single
    place bodies are read and the larger/smaller split is decided. Optional
    full_score/title_score telemetry is propagated into context when present.
    """
    from silica.kernel.workqueue import WorkItem

    items: list[WorkItem] = []
    for pair in pairs:
        source = pair.get("source")
        target = pair.get("target")
        score = pair.get("score", 0.0)
        if not source or not target:
            continue
        try:
            body_src = DRIVER.read_note(source).content or ""
            body_tgt = DRIVER.read_note(target).content or ""
        except Exception:
            continue

        # The more reliable note is the merge target; the other is the source of
        # new info. Reliability first, length only to break a tie within a tier.
        from silica.kernel.write.contested import merge_rank
        if merge_rank(body_tgt) >= merge_rank(body_src):
            larger, smaller, smaller_body = target, source, body_src
        else:
            larger, smaller, smaller_body = source, target, body_tgt

        context = {
            "concept": smaller.removesuffix(".md").rsplit("/", 1)[-1],
            "excerpt": smaller_body[:4000],
            "candidate": larger.removesuffix(".md").rsplit("/", 1)[-1],
            "score": score,
            "inbox_file": smaller,
            # The loser is a real vault note here (unlike the FSM path, where
            # inbox_file is a source document), so it can be marked on merge.
            "loser_path": smaller,
        }
        reason = f"dedup score={score:.3f}"
        if "full_score" in pair and "title_score" in pair:
            context["full_score"] = pair["full_score"]
            context["title_score"] = pair["title_score"]
            reason += f" (full={pair['full_score']:.3f} title={pair['title_score']:.3f})"

        items.append(WorkItem(kind="dedup", target_path=larger, context=context, reason=reason))
    return items


class DedupPairsArgs(BaseModel):
    pairs: list[dict] = Field(description="List of duplicate pairs to merge. Each dict must have 'source' and 'target' keys.")

@tool(DedupPairsArgs, cls="composed")
def silica_dedup_pairs(pairs: list[dict]) -> dict[str, Any]:
    """Merge an ALREADY-KNOWN list of duplicate note pairs (e.g. from a ledger task).

    The smaller note's genuinely-new info is appended to the larger note as a
    single patch. To discover duplicate pairs by scanning, use silica_dedup.
    """
    from silica.agent.subagent import run_subagent_batch

    if not pairs:
        return {"error": "No pairs provided."}

    items = _pairs_to_items(pairs)
    if not items:
        return {"success": False, "message": "No valid pairs to process"}

    res = run_subagent_batch(items)
    res["pairs_found"] = len(items)
    return res


class DedupFolderArgs(BaseModel):
    folder: str = Field(default="", description="Vault folder to scan for near-duplicate notes (empty = whole vault)")


@tool(DedupFolderArgs, cls="composed")
def silica_dedup(folder: str = "", cancel_token: Any = None) -> dict[str, Any]:
    """SCAN a folder (or the vault) for near-duplicate note pairs and merge each
    smaller note into its larger twin.

    Only the smaller note's genuinely-new info is appended to the larger note
    (a single append-only patch) — never rewrites, deletes, or creates notes.
    Requires the embedding index (silica_embed_refresh). If you already know
    the pairs, use silica_dedup_pairs instead.

    A pair is admitted when its body similarity is borderline OR its titles are
    strongly similar — the latter catches cases like "ROS" / "JSON in ROS 2"
    where bodies diverge but titles are clearly related.
    """
    from silica.agent.subagent import run_subagent_batch

    pairs, err = _scan_dedup_pairs(folder)
    if err:
        return {"error": err}

    items = _pairs_to_items(pairs)
    res = run_subagent_batch(items, cancel_token=cancel_token)
    res["pairs_found"] = len(items)
    res["folder"] = folder or "(vault)"
    return res


class RefineBatchArgs(BaseModel):
    note_paths: list[str] = Field(description="List of vault-relative paths to stylistically refine.")

@tool(RefineBatchArgs, cls="composed")
def silica_refine_batch(note_paths: list[str], cancel_token: Any = None) -> dict[str, Any]:
    """Stylistically refine a batch of notes: reformat for clarity and Obsidian
    style WITHOUT adding or losing information.

    To add missing content to thin notes, use silica_enrich_batch instead.
    """
    if not note_paths:
        return {"error": "No note paths provided."}

    from silica.kernel.workqueue import WorkItem
    from silica.agent.subagent import run_subagent_batch

    items = [WorkItem(kind="refine", target_path=p, context={}) for p in note_paths]
    res = run_subagent_batch(items, cancel_token=cancel_token)
    res["notes"] = len(items)
    return res


class EnrichBatchArgs(BaseModel):
    note_paths: list[str] = Field(description="List of vault-relative paths to semantically enrich.")

@tool(EnrichBatchArgs, cls="composed")
def silica_enrich_batch(note_paths: list[str], cancel_token: Any = None) -> dict[str, Any]:
    """Semantically enrich a batch of lean or empty notes: adds substantive
    content. To only fix style/formatting without changing content, use
    silica_refine_batch instead."""
    if not note_paths:
        return {"error": "No note paths provided."}

    from silica.kernel.workqueue import WorkItem
    from silica.agent.subagent import run_subagent_batch

    items = [WorkItem(kind="enrich", target_path=p, context={}) for p in note_paths]
    res = run_subagent_batch(items, cancel_token=cancel_token)
    res["notes"] = len(items)
    return res


class GenerateTaxonomyArgs(BaseModel):
    user_intent: str = Field(description="Natural-language description of how the user wants to organize their vault")
    scope: str = Field(
        default="",
        description="Vault-relative subfolder to restrict taxonomy generation and scanning to",
    )
    save_path: str = Field(
        default="",
        description=(
            "Vault-relative path where the taxonomy YAML should be written. "
            "Defaults to 'taxonomy.yaml' inside the configured vault."
        ),
    )
    merge: bool = Field(
        default=False,
        description=(
            "If True, feed the existing taxonomy to the LLM as standing rules and "
            "update it incrementally instead of regenerating it from scratch."
        ),
    )

@tool(GenerateTaxonomyArgs, cls="composed")
def silica_generate_taxonomy(
    user_intent: str, scope: str = "", save_path: str = "", merge: bool = False
) -> dict[str, Any]:
    """Generate a taxonomy YAML from a natural-language organization intent.

    Uses the LLM to translate the user's description into a structured
    FolderRule list, validates it with Pydantic, and writes it to disk
    at taxonomy.yaml (or the specified path).

    With merge=True the existing taxonomy (if any) is treated as standing
    directives: the LLM preserves its rules and only adds/updates what the
    new intent requires.

    Returns the validated taxonomy dict and the path it was written to.
    The user should review the output before running silica_run_organizer.
    """
    from pathlib import Path

    from silica.agent.llm import call_llm
    from silica.config import CONFIG
    from silica.kernel.text.sanitize import parse_json
    from silica.kernel.organize.taxonomy import (
        TAXONOMY_GENERATION_PROMPT,
        TAXONOMY_MERGE_BLOCK,
        Taxonomy,
        default_taxonomy_path,
    )
    from silica.driver import DRIVER

    # Resolve the destination first — merge mode reads the current file from there.
    if save_path:
        dest = Path(save_path)
        if not dest.is_absolute() and CONFIG.vault_path:
            dest = Path(CONFIG.vault_path) / dest
    else:
        dest = default_taxonomy_path()

    note_titles: list[str] = []
    try:
        refs = DRIVER.list_files(scope or "")
        note_titles = [
            Path(ref.path or ref.name).stem
            for ref in refs
            if (ref.path or ref.name).endswith(".md")
        ]
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("silica_generate_taxonomy: failed to list files for scope %s: %s", scope, exc)

    # Format the titles clearly (e.g., max 400 titles to prevent prompt bloat)
    titles_summary = "\n".join(f"- {t}" for t in note_titles[:400])
    if len(note_titles) > 400:
        titles_summary += f"\n- ... and {len(note_titles) - 400} more notes."

    system_prompt = "You are an expert knowledge manager. Follow the user instructions exactly."
    user_msg = TAXONOMY_GENERATION_PROMPT.format(
        user_intent=user_intent,
        scope=scope or "Entire Vault",
        note_titles=titles_summary or "(No notes found in scope)",
    )

    if merge and dest.exists():
        try:
            existing = Taxonomy.from_yaml(dest)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "silica_generate_taxonomy: cannot parse existing taxonomy at %s (%s) — generating from scratch",
                dest, exc,
            )
            existing = None
        if existing is not None and existing.rules:
            import yaml as _y
            existing_yaml = _y.dump(
                existing.model_dump(), allow_unicode=True, sort_keys=False, default_flow_style=False
            )
            user_msg += TAXONOMY_MERGE_BLOCK.format(existing_yaml=existing_yaml)

    try:
        response = call_llm(
            model=CONFIG.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            tools=None,
        )
        raw = (response.text or "").strip()
    except Exception as exc:
        return {"error": f"LLM call failed: {exc}"}

    # The LLM is instructed to return raw YAML; try yaml.safe_load first,
    # then fall back to parse_json for robustness.
    import yaml as _yaml

    taxonomy_dict: dict | None = None
    try:
        taxonomy_dict = _yaml.safe_load(raw)
    except Exception:
        pass

    if not isinstance(taxonomy_dict, dict):
        parsed, _ = parse_json(raw, strict=False)
        if isinstance(parsed, dict):
            taxonomy_dict = parsed
        else:
            return {"error": f"LLM returned unparseable output: {raw[:300]}"}

    try:
        taxonomy = Taxonomy.from_dict(taxonomy_dict)
    except Exception as exc:
        return {"error": f"Taxonomy validation failed: {exc}", "raw": taxonomy_dict}

    try:
        taxonomy.to_yaml(dest)
    except Exception as exc:
        return {"error": f"Failed to write taxonomy to {dest}: {exc}"}

    return {
        "success": True,
        "taxonomy_path": str(dest),
        "taxonomy": taxonomy.model_dump(),
        "rules_count": len(taxonomy.rules),
    }


class RunOrganizerArgs(BaseModel):
    taxonomy_path: str = Field(
        default="",
        description="Path to the taxonomy YAML file. Defaults to 'taxonomy.yaml' in the vault.",
    )
    scope: str = Field(
        default="",
        description="Vault-relative subfolder to restrict organization to (empty = vault-wide)",
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "If True (default), compute and return the move plan without executing any moves. "
            "Set to False to actually move notes."
        ),
    )
    llm_arbiter: bool = Field(
        default=True,
        description="If True, use the LLM to classify borderline notes (ambiguous band)",
    )
    move_uncategorized: bool = Field(
        default=False,
        description=(
            "If True, notes matching no taxonomy rule are moved to the uncategorized "
            "folder. Default False: unmatched notes stay where they are."
        ),
    )

@tool(RunOrganizerArgs, cls="composed")
def silica_run_organizer(
    taxonomy_path: str = "",
    scope: str = "",
    dry_run: bool = True,
    llm_arbiter: bool = True,
    move_uncategorized: bool = False,
) -> dict[str, Any]:
    """Classify vault notes against the taxonomy and move them into its folders.

    Requires a taxonomy (silica_generate_taxonomy first). Two-phase:
    dry_run=True (default) returns the move plan without touching anything;
    dry_run=False executes the moves graph-safely (wikilinks updated), with
    automatic rollback if the post-move lint gate fails. To move a single note
    directly, use silica_move instead.
    """
    from silica.kernel.organize.taxonomy import load_taxonomy
    from silica.router.organize_fsm import OrganizerFSM

    taxonomy = load_taxonomy(taxonomy_path or None)
    if not taxonomy.rules:
        return {
            "error": (
                "Taxonomy has no rules. Run silica_generate_taxonomy first or "
                "create taxonomy.yaml manually."
            )
        }

    fsm = OrganizerFSM(
        taxonomy=taxonomy,
        scope=scope,
        dry_run=dry_run,
        llm_arbiter=llm_arbiter,
        move_uncategorized=move_uncategorized,
    )
    return fsm.run()
