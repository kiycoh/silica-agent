from __future__ import annotations

import pytest

from silica.tools.tabular import silica_query_table

duckdb = pytest.importorskip("duckdb")  # the [bi] extra is opt-in


@pytest.fixture
def sales(tmp_path):
    p = tmp_path / "sales.csv"
    p.write_text(
        "region,amount,year\n"
        "north,100,2024\nnorth,50,2025\nsouth,30,2024\nsouth,20,2024\n"
    )
    return p


def test_aggregates(sales):
    out = silica_query_table(
        str(sales), "SELECT region, sum(amount) AS tot FROM t GROUP BY 1 ORDER BY 1"
    )
    assert out["columns"] == ["region", "tot"]
    assert out["rows"] == [["north", 150], ["south", 50]]
    assert out["truncated"] is False


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE t",
        "COPY (SELECT 1) TO '/tmp/pwn.csv'",
        "ATTACH '/tmp/x.db'",
        "SELECT 1; DROP TABLE t",  # the second statement is the payload
    ],
)
def test_rejects_non_select(sales, sql):
    with pytest.raises(ValueError):
        silica_query_table(str(sales), sql)


def test_confined_to_the_targets_directory(sales, tmp_path):
    """A SELECT is not enough: read_csv() inside one can still name any path."""
    outside = tmp_path.parent / "secret.csv"
    outside.write_text("s\n42\n")
    with pytest.raises(ValueError, match="query failed"):
        silica_query_table(str(sales), f"SELECT * FROM read_csv('{outside}')")


def test_truncation_is_flagged_not_silent(sales):
    out = silica_query_table(str(sales), "SELECT * FROM t", limit=2)
    assert out["row_count"] == 2 and out["truncated"] is True
    assert "limit=2" in out["note"]


def test_rejects_non_tabular_extension(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# hi")
    with pytest.raises(ValueError, match="not a tabular format"):
        silica_query_table(str(note), "SELECT * FROM t")


# --- schema and type honesty -------------------------------------------------

def test_schema_rides_on_every_reply(sales):
    """SQL written against guessed column types is the lane's failure mode."""
    out = silica_query_table(str(sales), "SELECT 1 AS one FROM t LIMIT 1")
    assert out["schema"] == {"region": "VARCHAR", "amount": "BIGINT", "year": "BIGINT"}


def test_full_file_sniff_survives_a_late_string(tmp_path):
    """DuckDB's default sample types on the head; row 21001 must not crash."""
    p = tmp_path / "long.csv"
    p.write_text("v\n" + "1\n" * 21000 + "x\n")
    out = silica_query_table(str(p), "SELECT count(*) AS n FROM t")
    assert out["rows"] == [[21001]]
    assert out["schema"]["v"] == "VARCHAR"  # typed on the whole file, not the head


def test_varchar_aggregate_fails_loud_not_wrong(tmp_path):
    """European numerics + n/a: sum() must error, never a partial number."""
    p = tmp_path / "eu.csv"
    p.write_text('city,revenue\nRoma,"1.234,50"\nMilano,987\nRoma,n/a\n')
    out = silica_query_table(str(p), "SUMMARIZE t")  # the taught first call
    assert out["schema"]["revenue"] == "VARCHAR"
    with pytest.raises(ValueError, match="VARCHAR"):
        silica_query_table(str(p), "SELECT sum(revenue) FROM t")


def test_byte_cap_truncates_wide_payloads(tmp_path):
    """`limit` counts rows; the cap counts bytes. Both must announce themselves."""
    p = tmp_path / "wide.csv"
    p.write_text("c\n" + ("x" * 1000 + "\n") * 200)
    out = silica_query_table(str(p), "SELECT * FROM t")
    assert out["truncated"] is True
    assert 0 < out["row_count"] < 200
    assert "KB" in out["note"]


# --- Excel: one sheet per call, via the temp-CSV detour ----------------------

def _xlsx(tmp_path, sheets: dict) -> str:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    p = tmp_path / "book.xlsx"
    wb.save(p)
    return str(p)


def test_xlsx_single_sheet_needs_no_sheet_arg(tmp_path):
    import datetime

    p = _xlsx(
        tmp_path,
        {"Data": [
            ["day", "amount"],
            [datetime.date(2025, 1, 1), 100],
            [datetime.date(2025, 2, 1), 50],
        ]},
    )
    out = silica_query_table(p, "SELECT sum(amount) AS tot FROM t")
    assert out["rows"] == [[150]]
    assert out["sheet"] == "Data"
    # a date cell survives the CSV round-trip as a date, not as text
    assert out["schema"]["day"] in ("DATE", "TIMESTAMP")


def test_xlsx_multi_sheet_rejects_and_lists(tmp_path):
    p = _xlsx(tmp_path, {"A": [["x"], [1]], "B": [["y"], [2]]})
    with pytest.raises(ValueError, match="A, B"):
        silica_query_table(p, "SELECT * FROM t")
    out = silica_query_table(p, "SELECT * FROM t", sheet="B")
    assert out["columns"] == ["y"]


def test_sheet_arg_on_a_csv_is_rejected(sales):
    with pytest.raises(ValueError, match="Excel"):
        silica_query_table(str(sales), "SELECT * FROM t", sheet="Data")
