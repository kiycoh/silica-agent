# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Survey card D3, draft B: Settings > Memory shows each entry's date in the
field's right margin. /memory hands each dated entry over with its text, and
the editor splits and matches entries in app-settings.js the way
user_memory._spans and entry_id do, run here under node. Where the dates land
in the field is verified by opening the app."""
from __future__ import annotations

import datetime
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from silica.config import CONFIG  # noqa: E402
from silica.kernel import user_memory as um  # noqa: E402

JS = Path(__file__).resolve().parents[1] / "silica" / "ui" / "web" / "static" / "app-settings.js"


def _eval(tmp_path, expr: str):
    src = JS.read_text(encoding="utf-8")
    pure = src[src.index("// mem-pure:start"):src.index("// mem-pure:end")]
    script = tmp_path / "m.js"
    script.write_text(pure + f"\nconsole.log(JSON.stringify({expr}));\n", encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture
def client(tmp_vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "user_memory", True)
    monkeypatch.setattr(CONFIG, "capture_sessions", False)
    from silica.ui.web import server

    server._reset_session()
    return TestClient(server.app)


def test_memory_hands_each_dated_entry_over_with_its_text(client):
    um.write_file("vault", "- Thesis due in March\n")  # before stamps existed: undated
    um._stamps_path(um.path_for("vault")).unlink()
    um.save("vault", "Exam on Friday", by="chat", session="s1")
    um.write_file("vault", um.read("vault") + "- Fee is 41,250 NOK\n  invoiced in two parts\n")
    today = datetime.date.today().isoformat()
    dates = client.get("/memory").json()["scopes"]["vault"]["dates"]
    assert dates == [
        {"text": "- Exam on Friday", "at": today, "by": "chat"},
        {"text": "- Fee is 41,250 NOK\n  invoiced in two parts", "at": today, "by": "you"},
    ]  # the session is not shown, so not sent; the undated entry is left out
    assert client.get("/memory").json()["scopes"]["user"]["dates"] == []


TEXT = ("- Exam on Friday\n\n- Fee is  41,250 NOK\n  invoiced in two parts\n"
        "typed without a bullet\n\t- tab continues it\n- Città di Bergen\n")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_editor_splits_and_matches_entries_as_the_server_does(tmp_path):
    keys = _eval(tmp_path, f"(t => stMemSpans(t).map(([s, e]) => stMemKey(t.slice(s, e))))({json.dumps(TEXT)})")
    assert keys == [um._norm(e.removeprefix("- ")) for e in um.entries(TEXT)]
    assert len(keys) == 4


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_a_date_names_who_wrote_the_entry_and_the_year_only_when_it_is_not_this_one(tmp_path):
    year = datetime.date.today().year
    out = _eval(tmp_path, f'[stMemWhen({{at: "{year}-10-07", by: "chat"}}), '
                          f'stMemWhen({{at: "{year}-09-14", by: "chat"}}), '
                          f'stMemWhen({{at: "2025-12-01", by: "you"}}), '
                          f'stMemWhen({{at: "{year}-10-07", by: "chat"}}, true), '
                          f'stMemWhen({{at: "2025-12-01", by: "you"}}, true)]')
    assert out == ["saved in a chat · 7 Oct", "saved in a chat · 14 Sep", "your words · 1 Dec 2025",
                   "chat · 7 Oct", "you · 1 Dec 2025"]  # the short words, for a narrow field
