# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Tabular query tool — read-only SQL over a data file, via DuckDB.

The BI probe lane. Rows cannot survive the SourceAdapter contract (ADR-0014
turns every source into markdown prose), and no amount of rerank tuning makes
"revenue by region" a top-k similarity problem — so tabular data gets its own
retrieval path instead of being forced through recall. This module is that
path's whole surface: one tool, one dependency, no ETL and no server.

Zero-trust (ADR-0009): the SQL is model-authored, so it is parsed and rejected
unless it is a single SELECT, and the connection is confined to the target
file's own directory before the query runs.
"""
from __future__ import annotations

from typing import Annotated

import io
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import Field

from silica.kernel.text import tabular as sandbox
from silica.tools import tool

logger = logging.getLogger(__name__)

# Rows are capped in bytes as well as in count: `limit` alone never sees that
# 200 rows of a wide table is a six-figure token payload.
_PAYLOAD_BYTE_CAP = 50_000


def _fit_rows(rows: list[list[Any]]) -> list[list[Any]]:
    """The longest prefix of `rows` that serializes under the byte cap."""
    size = 0
    for i, row in enumerate(rows):
        size += len(json.dumps(row, ensure_ascii=False, default=str)) + 2
        if size > _PAYLOAD_BYTE_CAP:
            return rows[:i]
    return rows


@tool(cls="atomic", ingress=False, note_args=("path",))
def silica_query_table(
    path: Annotated[str, Field(description='Path to the data file to query (.csv/.tsv/.parquet/.json, or .xlsx/.xls for one sheet at a time)')], sql: Annotated[str, Field(description="A single read-only SELECT. The file is bound to the table name `t` — e.g. SELECT region, avg(score) FROM t GROUP BY 1. When the columns are unknown, call with `SUMMARIZE t` first: one call returns every column's type, min/max, distinct count and null share, without spending rows.")], limit: Annotated[int, Field(description='Max rows returned; the reply flags whether it truncated')] = 200, sheet: Annotated[str, Field(description='Excel files only: which sheet to query. Omit for single-sheet workbooks; a multi-sheet workbook rejects the call and lists its sheet names.')] = ""
) -> dict[str, Any]:
    """Answers a question about a data file (.csv/.tsv/.parquet/.json, Excel
    via sheet=) by running SQL over it — the aggregation path semantic search
    cannot do: sums, group-by, ranking, ranges. Queried in place, bound to
    table `t`. Read-only: one SELECT per call. `SUMMARIZE t` is the cheap
    first call when columns are unknown. Replies carry the bound schema; a
    numeric-looking VARCHAR column holds non-numbers — count what try_cast
    loses before trusting an aggregate.
    """
    from silica.config import CONFIG

    src = Path(path).expanduser()
    vault = (CONFIG.vault_path or "").strip()
    # Vault first, cwd second — mirrors convert._resolve_input. The profile
    # note prints its query example repo-relative (machine-portable), so the
    # example must resolve from wherever the server happens to be running.
    if not src.is_absolute() and vault and (Path(vault) / src).exists():
        src = Path(vault) / src
    try:
        src = src.resolve(strict=True)
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e}") from e
    # The model names this path, so it is held to the vault the way a note
    # read is (audit 2026-10-09, S5): before, SQL ran over any data file the
    # process could open. With no vault configured the cwd is the vault.
    root = Path(vault or os.getcwd()).resolve()
    if root not in src.parents:
        raise ValueError(f"{path} is outside the vault {root.as_posix()}")
    from silica.config import in_config_dir

    if in_config_dir(src):
        raise ValueError(f"{path} is Silica's own configuration, not vault content")
    if not src.is_file():
        raise ValueError(f"{src} is not a file")
    suffix = src.suffix.lower()
    if suffix not in (*sandbox.READABLE_EXTS, *sandbox.SHEET_EXTS):
        raise ValueError(
            f"{suffix or 'no extension'} is not a tabular format — "
            f"expected one of {', '.join((*sandbox.READABLE_EXTS, *sandbox.SHEET_EXTS))}"
        )
    if sheet and suffix not in sandbox.SHEET_EXTS:
        raise ValueError(f"sheet= applies to Excel files only, not {suffix}")

    inner = sandbox.select_only(sql)
    sheet_name = ""
    # The temp dir is the confinement boundary for the sheet path: the workbook
    # is read in-process BEFORE any model SQL runs, so the SQL can only see the
    # one extracted sheet.
    with tempfile.TemporaryDirectory() as tmp:
        if suffix in sandbox.SHEET_EXTS:
            sheet_name, bound = sandbox.sheet_to_csv(src, sheet, Path(tmp))
        elif suffix in sandbox.SNIFFED_EXTS:
            bound = sandbox.utf8_source(src, Path(tmp))
        else:
            bound = src
        con = sandbox.connect(bound.parent)
        try:
            sandbox.bind_source(con, bound)
            schema = dict(
                (row[0], row[1]) for row in con.execute("DESCRIBE t").fetchall()
            )
            # limit+1 so truncation is observed rather than guessed at.
            rows = con.execute(f"SELECT * FROM ({inner}) LIMIT {int(limit) + 1}").fetchall()
            columns = [d[0] for d in con.description]
        except Exception as e:
            raise ValueError(f"query failed: {type(e).__name__}: {e}") from e
        finally:
            con.close()

    over_limit = len(rows) > limit
    within_limit = [list(r) for r in rows[:limit]]
    kept = _fit_rows(within_limit)
    truncated = over_limit or len(kept) < len(within_limit)
    if over_limit:
        note = f"truncated at limit={limit}; aggregate or raise limit"
    elif truncated:
        note = (
            f"truncated at ~{_PAYLOAD_BYTE_CAP // 1000} KB of rows; "
            "aggregate or select fewer columns instead of scanning"
        )
    else:
        note = ""
    return {
        "path": str(src),
        **({"sheet": sheet_name} if sheet_name else {}),
        # The bound table's schema on every reply: SQL written against guessed
        # column names or types is the lane's main failure mode.
        "schema": schema,
        "columns": columns,
        "rows": kept,
        "row_count": len(kept),
        # Never a silent cap: a truncated answer that reads as complete is how a
        # BI number comes out confidently wrong.
        "truncated": truncated,
        **({"note": note} if note else {}),
    }

# One file per call, so no joins across files. Binding a dict of path→name as
# t1..tn is the design for the day a real question needs two tables at once.


# The census deliberately drops .txt and .json from READABLE_EXTS: a vault's
# .txt is prose more often than a table and .json is config more often than
# records, so listing them would bury the real tables under false positives.
# Both stay queryable by explicit path through silica_query_table.
_CENSUS_EXTS = (".csv", ".tsv", ".parquet", ".ndjson", *sandbox.SHEET_EXTS)
_CATALOG_FILE_CAP = 500  # files walked before the census itself truncates
# Head sample, not query_table's whole-file sniff: a catalog is orientation,
# and the full sniff on a hundred-file vault costs seconds per call, while a
# head-typed column corrupts nothing here — query_table re-sniffs in full.
_CATALOG_SNIFF_ROWS = 2048


def _describe_table(src: Path) -> dict[str, Any]:
    """One census entry body: {"columns": …} | {"sheets": …} | {"error": …}.

    The census must finish: an unreadable file becomes a named error entry,
    never a dropped path or an aborted catalog.
    """
    import duckdb

    if src.suffix.lower() in sandbox.SHEET_EXTS:
        # Sheets only, no per-sheet schema: opening every sheet of every
        # workbook is a query_table call's worth of work per file.
        try:
            if src.suffix.lower() == ".xls":
                import xlrd

                book = xlrd.open_workbook(str(src), logfile=io.StringIO())
                return {"sheets": book.sheet_names()}
            from openpyxl import load_workbook

            return {"sheets": load_workbook(src, read_only=True).sheetnames}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def describe(path: Path) -> dict[str, str]:
        quoted = str(path).replace("'", "''")
        rel = (
            f"read_csv('{quoted}', sample_size={_CATALOG_SNIFF_ROWS})"
            if path.suffix.lower() in sandbox.SNIFFED_EXTS
            else f"'{quoted}'"
        )
        # A bare connection, not sandbox.connect: the SQL here is ours (a
        # DESCRIBE over a resolved vault path), never model-authored.
        con = duckdb.connect()
        try:
            rows = con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()
        finally:
            con.close()
        return {r[0]: r[1] for r in rows}

    try:
        return {"columns": describe(src)}
    except Exception as first:
        if src.suffix.lower() in sandbox.SNIFFED_EXTS:
            # Retry through the encoding ladder before giving up: public-data
            # exports are routinely cp1252 (4 of 26 CSVs on a real vault) and
            # DuckDB refuses them outright.
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    return {"columns": describe(sandbox.utf8_source(src, Path(tmp)))}
            except Exception:
                pass
        return {"error": f"{type(first).__name__}: {first}"}


@tool(cls="atomic", ingress=False)
def silica_tables(folder: Annotated[str, Field(description='Vault-relative folder to scope the census; empty = the whole vault')] = "", column: Annotated[str, Field(description='Case-insensitive substring filter: only tables with a matching column name return, with the matches named')] = "", limit: Annotated[int, Field(description='Max tables described in the reply')] = 50) -> dict[str, Any]:
    """Census of the vault's tabular files (.csv/.tsv/.parquet/.ndjson, Excel):
    path, size and column schema per file, filterable by column name — the
    orientation call before silica_query_table. column= answers "which table
    holds NEET?" in one call instead of head-reading every file. Excel entries
    list their sheets (a sheet's schema costs one query_table call). Schemas
    come from a head sample; the types query_table replies with (whole-file
    sniff) are the ones to trust."""

    from silica.config import CONFIG
    from silica.kernel.base.layout import walk_tree

    vault = (CONFIG.vault_path or "").strip()
    if not vault:
        raise ValueError("no vault configured")
    root = Path(vault).resolve()
    base = (root / folder).resolve() if folder.strip() else root
    try:
        base.relative_to(root)
    except ValueError:
        raise ValueError(f"{folder!r} escapes the vault") from None
    if not base.is_dir():
        raise ValueError(f"{folder or str(root)!r} is not a folder in the vault")

    found: list[Path] = []
    for dirpath, dirnames, filenames in walk_tree(root, base):
        # Images/ too: conversion output, not data.
        dirnames[:] = [d for d in dirnames if d != "Images"]
        for name in filenames:
            if not name.startswith(".") and Path(name).suffix.lower() in _CENSUS_EXTS:
                found.append(Path(dirpath) / name)
        if len(found) > _CATALOG_FILE_CAP:
            break
    walk_truncated = len(found) > _CATALOG_FILE_CAP
    found = found[:_CATALOG_FILE_CAP]

    needle = column.strip().casefold()
    tables: list[dict[str, Any]] = []
    unsearchable = 0
    scan_stopped = False
    for src in found:
        if len(tables) >= max(1, limit):
            # Files past this point were never described: truncated, not absent.
            scan_stopped = True
            break
        body = _describe_table(src)
        if needle:
            cols = body.get("columns")
            if cols is None:
                unsearchable += 1  # sheets/errors carry no columns to match
                continue
            matched = sorted(c for c in cols if needle in c.casefold())
            if not matched:
                continue
            body = {**body, "matched": matched}
        tables.append({
            "path": src.relative_to(root).as_posix(),
            "size_bytes": src.stat().st_size,
            **body,
        })

    # Same byte discipline as query_table: 50 wide schemas can outweigh 200 rows.
    kept, size = [], 0
    for entry in tables:
        size += len(json.dumps(entry, ensure_ascii=False, default=str)) + 2
        if size > _PAYLOAD_BYTE_CAP:
            break
        kept.append(entry)

    truncated = walk_truncated or scan_stopped or len(kept) < len(tables)
    notes = []
    if walk_truncated:
        notes.append(f"census stopped at {_CATALOG_FILE_CAP} files; narrow with folder=")
    if scan_stopped:
        notes.append(f"stopped at limit={limit}; raise limit or narrow with folder=")
    if len(kept) < len(tables):
        notes.append(f"~{_PAYLOAD_BYTE_CAP // 1000} KB payload cap hit; narrow the census")
    if needle and unsearchable:
        notes.append(
            f"{unsearchable} file(s) had no readable columns to match "
            "(Excel workbooks or unreadable files)"
        )
    return {
        # The census size, before filter and caps: "3 of 143" reads correctly.
        "total": len(found),
        "returned": len(kept),
        "tables": kept,
        "truncated": truncated,
        **({"note": "; ".join(notes)} if notes else {}),
    }
