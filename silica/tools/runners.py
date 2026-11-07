# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Runner tools — whole pipelines and leashed sub-agent batches.

Full FSM runs (injector, organizer), taxonomy generation, run-ledger
inspection, and the dedup/enrich sub-agent passes.
"""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from silica.tools import tool


@tool(cls="composed", collapse="eager")
def silica_run_injector(
    inbox_file: Annotated[str, Field(description='Path to a single inbox file (legacy; use inbox_files for multiple files)')] = "",
    inbox_files: Annotated[list[str] | None, Field(description='Paths to one or more inbox files to nucleate in a single run')] = None,
    # Required, and keyword-only so the rest of the signature keeps its order:
    # the guard below rejects an empty target anyway, and a schema that let the
    # model omit it turned that rejection into a wasted turn instead of a
    # missing argument the caller can see before dispatch.
    *,
    target_dir: Annotated[str, Field(description='Destination directory for the extracted concepts')],
    hub: Annotated[str, Field(description='Optional reference hub note')] = "",
    resume_run_id: Annotated[str, Field(description='Run ID to resume (re-processes only failed chunks, skips done ones)')] = "",
    keep_sources: Annotated[bool, Field(description='Write the verbatim source leaf in sources/ beside the notes (default on; pass false to skip)')] = True,
    cancel_token: Any = None,
) -> dict[str, Any]:
    """Nucleate inbox files into the vault — THE tool for "nucleate/inject
    this file": full pipeline, quality gates, rollback. A failed chunk rolls
    back and is marked 'failed' while the rest continue; resume_run_id re-runs
    only the failed chunks. For a single quick note,
    silica_write_note/silica_patch_note are cheaper.
    """
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
    # CLI always resolves a target before dispatching (ingest.nucleate); this guard
    # covers the agent-tool path, whose contract already says to pass one.
    # Asked before conversion: a PDF is minutes of work to transcode and it
    # would all be thrown away here.
    if not target_dir.strip():
        return {"error": "No target_dir specified. Pick the vault folder for these notes and pass it as target_dir."}

    from silica.sources.ingest import run_injector

    return run_injector(files, target_dir, hub=hub, resume_run_id=resume_run_id,
                        keep_sources=keep_sources, cancel_token=cancel_token)


@tool(cls="composed")
def silica_ledger_digest(run_id: Annotated[str, Field(description='Run ID to inspect (latest saved run if empty)')] = "") -> dict[str, Any]:
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


@tool(cls="composed")
def silica_dedup_pairs(pairs: Annotated[list[dict], Field(description="List of duplicate pairs to merge. Each dict must have 'source' and 'target' keys.")]) -> dict[str, Any]:
    """Merge an ALREADY-KNOWN list of duplicate note pairs (e.g. from a ledger task).

    The smaller note's genuinely-new info is appended to the larger note as a
    single patch. To discover duplicate pairs by scanning, use silica_dedup.
    """
    from silica.agent.subagent import run_subagent_batch
    from silica.kernel.recall import duplicates

    if not pairs:
        return {"error": "No pairs provided."}

    items = duplicates.pairs_to_items(pairs)
    if not items:
        return {"success": False, "message": "No valid pairs to process"}

    res = run_subagent_batch(items)
    res["pairs_found"] = len(items)
    return res


@tool(cls="composed")
def silica_dedup(folder: Annotated[str, Field(description='Vault folder to scan for near-duplicate notes (empty = whole vault)')] = "", cancel_token: Any = None) -> dict[str, Any]:
    """SCAN a folder (or the vault) for near-duplicate pairs and merge each
    smaller note into its larger twin: only genuinely-new info is appended
    (one patch) — never rewrites, deletes, or creates. Requires the embedding
    index (silica_embed_refresh). Known pairs: silica_dedup_pairs. Admission
    is borderline body similarity OR strong title similarity ("ROS" /
    "JSON in ROS 2").
    """
    from silica.agent.subagent import run_subagent_batch
    from silica.kernel.recall import duplicates

    pairs, err = duplicates.scan_pairs(folder)
    if err:
        return {"error": err}

    items = duplicates.pairs_to_items(pairs)
    res = run_subagent_batch(items, cancel_token=cancel_token)
    res["pairs_found"] = len(items)
    res["folder"] = folder or "(vault)"
    return res


@tool(cls="composed")
def silica_enrich_batch(note_paths: Annotated[list[str], Field(description='List of vault-relative paths to semantically enrich.')], cancel_token: Any = None) -> dict[str, Any]:
    """Add what a batch of lean or empty notes is missing, each as an appended
    section: nothing the note already says is rewritten or removed."""
    if not note_paths:
        return {"error": "No note paths provided."}

    from silica.kernel.workqueue import WorkItem
    from silica.agent.subagent import run_subagent_batch

    items: list[WorkItem] = [WorkItem(kind="enrich", target_path=p, context={}) for p in note_paths]
    res = run_subagent_batch(items, cancel_token=cancel_token)
    res["notes"] = len(items)
    return res


@tool(cls="composed")
def silica_generate_taxonomy(
    user_intent: Annotated[str, Field(description='Natural-language description of how the user wants to organize their vault')], scope: Annotated[str, Field(description='Vault-relative subfolder to restrict taxonomy generation and scanning to')] = "", save_path: Annotated[str, Field(description="Vault-relative path where the taxonomy YAML should be written. Defaults to 'taxonomy.yaml' inside the configured vault.")] = "", merge: Annotated[bool, Field(description='If True, feed the existing taxonomy to the LLM as standing rules and update it incrementally instead of regenerating it from scratch.')] = False
) -> dict[str, Any]:
    """Generate a taxonomy YAML from a natural-language organization intent
    and write it to taxonomy.yaml (or save_path). merge=True treats the
    existing taxonomy as standing directives: preserves its rules, only adds
    what the new intent requires. The user should review the output before
    silica_run_organizer.
    """
    from pathlib import Path

    from silica.model.llm import call_llm
    from silica.config import CONFIG
    from silica.model.reply import parse_reply
    from silica.kernel.organize.taxonomy import (
        TAXONOMY_GENERATION_PROMPT,
        TAXONOMY_MERGE_BLOCK,
        Taxonomy,
        default_taxonomy_path,
    )
    from silica.driver import DRIVER

    # Resolve the destination first — merge mode reads the current file from there.
    # save_path is documented vault-relative and is model-supplied, so it goes
    # through the vault choke point: to_yaml creates parent dirs and writes, so
    # an absolute or `..` path would plant a file anywhere on the filesystem.
    if save_path:
        from silica.kernel.base.layout import contain_in_vault
        try:
            dest = Path(CONFIG.vault_path) / contain_in_vault(save_path, Path(CONFIG.vault_path))
        except ValueError as exc:
            return {"error": f"Refused save_path '{save_path}': {exc}"}
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
        taxonomy_dict = parse_reply(raw)
        if not isinstance(taxonomy_dict, dict):
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


@tool(cls="composed")
def silica_run_organizer(
    taxonomy_path: Annotated[str, Field(description="Path to the taxonomy YAML file. Defaults to 'taxonomy.yaml' in the vault.")] = "",
    scope: Annotated[str, Field(description='Vault-relative subfolder to restrict organization to (empty = vault-wide)')] = "",
    dry_run: Annotated[bool, Field(description='If True (default), compute and return the move plan without executing any moves. Set to False to actually move notes.')] = True,
    llm_arbiter: Annotated[bool, Field(description='If True, use the LLM to classify borderline notes (ambiguous band)')] = True,
    move_uncategorized: Annotated[bool, Field(description='If True, notes matching no taxonomy rule are moved to the uncategorized folder. Default False: unmatched notes stay where they are.')] = False,
) -> dict[str, Any]:
    """Classify notes against the taxonomy (silica_generate_taxonomy first)
    and move them into its folders. dry_run=True (default) returns the plan;
    dry_run=False moves graph-safely (wikilinks updated) with rollback on a
    failed lint gate. Single note: silica_move.
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
