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
