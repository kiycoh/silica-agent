"""silica_files caps its listing: `total` is always the real count, the
`files` array never exceeds the cap, and truncation is flagged with a hint —
so a bare "how many notes?" costs a dict, not 1000 rows of context.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from silica.tools.atomic import _FILES_CAP, silica_files


def _refs(n: int):
    return [SimpleNamespace(name=f"n{i}", path=f"F/n{i}.md") for i in range(n)]


def test_small_vault_not_truncated():
    with patch("silica.tools.atomic.DRIVER") as drv:
        drv.list_files.return_value = _refs(3)
        res = silica_files()
    assert res["total"] == 3
    assert len(res["files"]) == 3
    assert "truncated" not in res


def test_large_vault_capped_with_hint():
    with patch("silica.tools.atomic.DRIVER") as drv:
        drv.list_files.return_value = _refs(_FILES_CAP + 35)
        res = silica_files()
    assert res["total"] == _FILES_CAP + 35
    assert len(res["files"]) == _FILES_CAP
    assert res["truncated"] is True
    assert "folder=" in res["hint"]
