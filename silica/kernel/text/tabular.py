# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The DuckDB sandbox: a read-only connection to one folder, and the data files
it can read. silica_query_table and the conversion's table profile share it.

Zero-trust (ADR-0009): the SQL is model-authored, so it is parsed and rejected
unless it is a single SELECT, and the connection is confined to the target
file's own directory before the query runs.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

# Everything DuckDB binds from a bare path with no extension loaded.
READABLE_EXTS = (".csv", ".tsv", ".txt", ".parquet", ".json", ".ndjson")
# Spreadsheets: one sheet per call, extracted in-process to a temp CSV that is
# then queried like any other. The direct route (DuckDB's `excel` extension)
# needs INSTALL, which the sandbox below blocks by design — the detour keeps
# the whole downstream path (sniffing, confinement, caps) shared instead of
# duplicated per format.
SHEET_EXTS = (".xlsx", ".xlsm", ".xls")
# Text formats are sniffed on the WHOLE file (sample_size=-1), not DuckDB's
# default 20480-row head: a column typed on its head either errors on the first
# odd row past the sample or — worse — bakes a type the data does not keep.
# Parquet/JSON carry their own types, so the knob does not apply to them.
SNIFFED_EXTS = (".csv", ".tsv", ".txt")


def connect(data_dir: Path):
    """A DuckDB connection that can read `data_dir` and nothing else.

    The order is load-bearing and was verified, not assumed: `allowed_directories`
    on its own confines nothing (it read /etc/passwd and wrote via COPY TO). It
    is an allowlist carved out of `enable_external_access=false`, so the access
    flag must be dropped *after* the directory is named, and the configuration
    locked *after* both — otherwise the model's SQL can simply widen it back.
    """
    import duckdb

    from silica.config import hosted

    con = duckdb.connect()
    if hosted():
        # The tenant's cgroup is 1 GB (silica@.service MemoryMax=1G) and
        # DuckDB's default is 80% of the machine's memory. A 100 MB CSV's
        # profile peaked at 283 MB for the whole process (2026-10-17), so
        # 256 MB for DuckDB's own buffers is past it, and a spill or an
        # out-of-memory error is the query's, not the tenant's (ADR-0120).
        con.execute("SET memory_limit='256MB'")
        con.execute("SET threads=2")
    con.execute("SET allowed_directories=[?]", [str(data_dir)])
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")
    return con


def select_only(sql: str) -> str:
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


def bind_source(con, path: Path) -> None:
    """CREATE VIEW t over `path` — read per query, never copied.

    Interpolated because a path cannot be a prepared-statement parameter in
    FROM; the path is resolved and real, and the quote-doubling closes the
    only injection route left.
    """
    quoted = str(path).replace("'", "''")
    if path.suffix.lower() in SNIFFED_EXTS:
        con.execute(
            f"CREATE VIEW t AS SELECT * FROM read_csv('{quoted}', sample_size=-1)"
        )
    else:
        con.execute(f"CREATE VIEW t AS SELECT * FROM '{quoted}'")


# Western-European ladder, tried in order. utf-8 first so a correct file is
# never re-encoded; cp1252 before latin-1 because Windows exports put curly
# quotes and the ellipsis in the C1 range (ISTAT's "…" is 0x85, which latin-1
# reads as the NEL control); latin-1 last because it decodes every byte and
# so can never itself raise.
_TEXT_ENCODINGS = ("utf-8", "cp1252", "latin-1")


def utf8_source(src: Path, tmpdir: Path) -> Path:
    """`src` when it is already utf-8, else a re-encoded copy under `tmpdir`.

    DuckDB's reader validates encoding and refuses anything else outright —
    including bytes its own `encoding='latin-1'` rejects (0x85 again) — so the
    decode happens here, in Python, where the ladder is ours to pick. Measured
    on a real public-data vault: 4 of 26 CSVs were not utf-8.
    """
    raw = src.read_bytes()
    for enc in _TEXT_ENCODINGS:
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if enc == "utf-8":
            return src            # the common case copies nothing
        out = tmpdir / src.name
        out.write_text(text, encoding="utf-8")
        return out
    return src  # undecodable by latin-1 is impossible; bind and let DuckDB speak


def pick_sheet(names: list[str], sheet: str) -> str:
    """The requested sheet name, or the only one; ambiguity is a teaching error."""
    if sheet:
        if sheet in names:
            return sheet
        raise ValueError(f"sheet {sheet!r} not found — this workbook has: {', '.join(names)}")
    if len(names) == 1:
        return names[0]
    # Guessing (say, the first sheet) would answer from the wrong table without
    # any signal that it did; the retry costs one call and names the choices.
    raise ValueError(
        "this workbook has several sheets — pass sheet= to pick one of: " + ", ".join(names)
    )


def sheet_rows(src: Path, sheet: str):
    """(sheet name, iterator of value rows) for a workbook, values not formulas."""
    if src.suffix.lower() == ".xls":
        import xlrd  # base dependency; convert.py reads .xls with it too

        # logfile: xlrd narrates parse trivia straight to stdout otherwise.
        book = xlrd.open_workbook(str(src), logfile=io.StringIO())
        name = pick_sheet(book.sheet_names(), sheet)
        sh = book.sheet_by_name(name)

        def cell(c):
            # Dates are bare floats in BIFF; only the cell type says so.
            if c.ctype == xlrd.XL_CELL_DATE:
                return xlrd.xldate_as_datetime(c.value, book.datemode)
            return c.value

        return name, ([cell(c) for c in sh.row(r)] for r in range(sh.nrows))
    from openpyxl import load_workbook

    wb = load_workbook(src, read_only=True, data_only=True)
    name = pick_sheet(wb.sheetnames, sheet)
    return name, (list(r) for r in wb[name].iter_rows(values_only=True))


def sheet_to_csv(src: Path, sheet: str, workdir: Path) -> tuple[str, Path]:
    """Extract one sheet to a CSV under `workdir`; (sheet name, csv path).

    A CSV round-trip on purpose: datetimes go out as ISO strings and numbers as
    their repr, both of which the full-file sniff recovers unambiguously — and
    typed values that openpyxl/xlrd already resolved (a formula's cached value,
    a BIFF date float) arrive as data, not artifacts.
    """

    name, rows = sheet_rows(src, sheet)
    out = workdir / "sheet.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        for row in rows:
            writer.writerow(
                "" if v is None else v.isoformat() if hasattr(v, "isoformat") else v
                for v in row
            )
    return name, out
