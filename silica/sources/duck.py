# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""DuckDB confined to one folder, and a data file bound in it as `t`.

Below both of its readers: `tools/tabular` runs the model's SELECT here, and
`convert` profiles a CSV here, so the converter need not reach up into a tool.
"""
from __future__ import annotations

from pathlib import Path


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
