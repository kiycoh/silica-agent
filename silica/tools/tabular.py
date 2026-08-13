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

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from silica.tools import tool

logger = logging.getLogger(__name__)

# Everything DuckDB binds from a bare path with no extension loaded. xlsx needs
# the `excel` extension, and INSTALL is blocked by the sandbox below, so it is
# deliberately absent rather than half-working.
READABLE_EXTS = (".csv", ".tsv", ".txt", ".parquet", ".json", ".ndjson")


def _connect(data_dir: Path):
    """A DuckDB connection that can read `data_dir` and nothing else.

    The order is load-bearing and was verified, not assumed: `allowed_directories`
    on its own confines nothing (it read /etc/passwd and wrote via COPY TO). It
    is an allowlist carved out of `enable_external_access=false`, so the access
    flag must be dropped *after* the directory is named, and the configuration
    locked *after* both — otherwise the model's SQL can simply widen it back.
    """
    import duckdb

    con = duckdb.connect()
    con.execute("SET allowed_directories=[?]", [str(data_dir)])
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")
    return con


def _select_only(sql: str) -> str:
    """The one statement in `sql` if it is a SELECT, else ValueError.

    Parsed by DuckDB, not pattern-matched: a keyword blocklist misses `ATTACH`,
    misreads a column literally named "drop", and is exactly the flimsy half of
    a choice where the correct half costs the same one call.
    """
    import duckdb

    try:
        statements = duckdb.extract_statements(sql)
    except Exception as e:
        raise ValueError(f"unparsable SQL: {e}") from e
    if len(statements) != 1:
        raise ValueError(
            f"expected exactly 1 statement, got {len(statements)} — "
            "this tool reads, so it runs one SELECT per call"
        )
    stmt = statements[0]
    if stmt.type != duckdb.StatementType.SELECT:
        raise ValueError(
            f"{stmt.type.name} rejected: silica_query_table is read-only, "
            "the query must be a SELECT (WITH … SELECT is fine)"
        )
    return stmt.query.strip().rstrip(";")


class QueryTableArgs(BaseModel):
    path: str = Field(
        description="Path to the data file to query (.csv/.tsv/.parquet/.json)",
    )
    sql: str = Field(
        description=(
            "A single read-only SELECT. The file is bound to the table name `t` "
            "— e.g. SELECT cucina, avg(valutazione) FROM t GROUP BY 1. "
            "Call with `SELECT * FROM t LIMIT 5` first if the columns are unknown."
        ),
    )
    limit: int = Field(
        default=200,
        description="Max rows returned; the reply flags whether it truncated",
    )


@tool(QueryTableArgs, cls="atomic")
def silica_query_table(path: str, sql: str, limit: int = 200) -> dict[str, Any]:
    """Answers a question about a data file by running SQL over it.

    For .csv/.tsv/.parquet/.json — the aggregation path, for questions semantic
    search structurally cannot answer: sums, averages, group-by, ranking,
    counting, filtering on numeric or date ranges. The file is queried in place
    (nothing is imported, nothing is written) and is bound to the table name `t`.

    Read-only: a single SELECT per call, anything else is rejected. When the
    columns aren't known yet, `SELECT * FROM t LIMIT 5` is the cheap first call.
    """
    try:
        import duckdb  # noqa: F401
    except ImportError as e:
        raise ValueError(
            "the tabular lane needs DuckDB: pip install 'silica-agent[bi]'"
        ) from e

    src = Path(path).expanduser()
    try:
        src = src.resolve(strict=True)
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e}") from e
    if not src.is_file():
        raise ValueError(f"{src} is not a file")
    if src.suffix.lower() not in READABLE_EXTS:
        raise ValueError(
            f"{src.suffix or 'no extension'} is not a tabular format — "
            f"expected one of {', '.join(READABLE_EXTS)}"
        )

    inner = _select_only(sql)
    con = _connect(src.parent)
    try:
        # Bound as a view, so the file is read once per query and never copied.
        # Interpolated because a path cannot be a prepared-statement parameter in
        # FROM; `src` is a resolved real path and the quote-doubling closes the
        # only injection route left.
        con.execute(f"CREATE VIEW t AS SELECT * FROM '{str(src).replace(chr(39), chr(39) * 2)}'")
        # limit+1 so truncation is observed rather than guessed at.
        rows = con.execute(f"SELECT * FROM ({inner}) LIMIT {int(limit) + 1}").fetchall()
        columns = [d[0] for d in con.description]
    except Exception as e:
        raise ValueError(f"query failed: {type(e).__name__}: {e}") from e
    finally:
        con.close()

    truncated = len(rows) > limit
    return {
        "path": str(src),
        "columns": columns,
        "rows": [list(r) for r in rows[:limit]],
        "row_count": len(rows[:limit]),
        # Never a silent cap: a truncated answer that reads as complete is how a
        # BI number comes out confidently wrong.
        "truncated": truncated,
        **({"note": f"truncated at limit={limit}; aggregate or raise limit"} if truncated else {}),
    }

# ponytail: one file per call, so no joins across files. Bind a dict of
# path→name as t1..tn when a real question needs two tables at once.
