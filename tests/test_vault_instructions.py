# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0127 point 4: a vault's author tells every chat over it what to do, in
`.silica/instructions.md`, and Settings › Skills writes it."""
from __future__ import annotations

import io

import pytest

from silica.kernel import vault_instructions as vi


def test_the_seed_carries_them_after_memory_and_before_the_language_line():
    from silica.cli import seed_messages
    from silica.config import CONFIG

    assert not any(m.get("origin") == vi.ORIGIN for m in seed_messages())
    assert vi.write(CONFIG.vault_path, "Cite articles as art. 1321 c.c.")["ok"]
    seed = seed_messages()
    at = next(i for i, m in enumerate(seed) if m.get("origin") == vi.ORIGIN)
    assert at == len(seed) - 2 and "art. 1321 c.c." in seed[at]["content"]
    assert "author's words" in seed[at]["content"]


def test_write_refuses_over_the_cap_and_an_empty_text_removes_the_file():
    from silica.config import CONFIG

    out = vi.write(CONFIG.vault_path, "x" * (vi.CAP + 1))
    assert not out["ok"] and str(vi.CAP) in out["error"]
    assert vi.write(CONFIG.vault_path, "Rispondi in italiano.")["ok"]
    assert vi.write(CONFIG.vault_path, "Rispondi in italiano.") == {"ok": True, "unchanged": True}
    assert vi.write(CONFIG.vault_path, "  ")["ok"] and not vi.path(CONFIG.vault_path).exists()


def test_a_file_edited_by_hand_over_the_cap_is_cut_at_a_line():
    from silica.config import CONFIG

    p = vi.path(CONFIG.vault_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(["una riga di istruzioni"] * 200), encoding="utf-8")
    body = vi.message(CONFIG.vault_path)["content"].split("\n\n", 1)[1]
    assert len(body) <= vi.CAP and body.endswith("istruzioni")


@pytest.fixture
def client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from silica.ui.web import server

    return TestClient(server.app), server


def test_settings_reads_and_writes_them_and_the_next_chat_is_seeded_with_them(client):
    tc, server = client
    assert tc.get("/instructions").json()["text"] == ""
    assert tc.put("/instructions", json={"text": "Cita gli articoli."}).status_code == 200
    assert tc.get("/instructions").json()["text"] == "Cita gli articoli."
    assert any("Cita gli articoli." in m["content"] for m in server._seeded()[0]
               if m.get("origin") == vi.ORIGIN)
    assert tc.put("/instructions", json={"text": "x" * (vi.CAP + 5)}).status_code == 400


def test_skills_lists_and_previews_a_persona_as_one(client):
    tc, _server = client
    text = ("---\nname: parafrasi\ndescription: clear legal paraphrase\nmetadata:\n  persona: true\n"
            "---\nBold the key terms.\n")
    pv = tc.post("/skills", files={"file": ("SKILL.md", io.BytesIO(text.encode()), "text/markdown")},
                 data={"confirm": "false"}).json()["skill"]
    assert pv["persona"] is True
    tc.post("/skills", files={"file": ("SKILL.md", io.BytesIO(text.encode()), "text/markdown")},
            data={"confirm": "true"})
    rows = [r for g in tc.get("/skills").json()["groups"] for r in g["skills"]]
    assert next(r for r in rows if r["name"] == "parafrasi")["persona"] is True
    assert not any(r["persona"] for r in rows if r["name"] != "parafrasi")
