# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""ADR-0055 "Turning it on" and "The Library surface": the web routes behind
Settings › Zotero and Explore › Library."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from silica.config import CONFIG
from silica.kernel.vault_manifest import load_manifest, set_zotero
from silica.sources import zotero as z
from silica.sources import zotero_write as zw
from tests.zotero_fake import SERVER_ID, FakeZotero

LEAF = "sources/@packerMemGPTLLMsOperating2024.md"


@pytest.fixture
def fake(tmp_vault, monkeypatch):
    f = FakeZotero()
    f.add_paper()

    def _client(*_a, **_k):
        return z.Zotero(server_id=(load_manifest(CONFIG.vault_path).zotero or
                                   type("L", (), {"library": ""})).library,
                        api_key=os.getenv("SILICA_ZOTERO_API_KEY", ""), transport=f.transport())

    monkeypatch.setattr(z, "client", _client)
    monkeypatch.setattr(z, "_client_for", lambda vault, link: _client())
    monkeypatch.setattr(z, "Zotero", _bind(f, z.Zotero))
    return f


def _bind(f, real):
    def make(*a, **kw):
        kw.setdefault("transport", f.transport())
        return real(*a, **kw)
    return make


@pytest.fixture
def web(tmp_vault):
    from silica.ui.web import server
    server._reset_session()
    return TestClient(server.app)


def _link():
    set_zotero(CONFIG.vault_path, "library", SERVER_ID)


# --- Settings › Zotero ------------------------------------------------------------

def test_status_of_a_running_zotero_on_an_unlinked_vault(fake, web):
    s = web.get("/zotero/status").json()

    assert (s["state"], s["version"], s["items"], s["library"]) == ("running", "10.0.5", 1, SERVER_ID)
    assert s["linked"] is False and s["write"] is False


@pytest.mark.parametrize("setup, state", [
    (lambda f: setattr(f, "refuse", True), "closed"),
    (lambda f: setattr(f, "api_off", True), "api_off"),
    (lambda f: setattr(f, "zotero_version", "8.0.1"), "read_only"),
    (lambda f: setattr(f, "server_id", "Qm41xTr8pZ0a"), "other"),
])
def test_status_names_each_state_of_zotero(fake, web, setup, state):
    _link()
    setup(fake)

    assert web.get("/zotero/status").json()["state"] == state


def test_use_in_this_vault_writes_and_removes_the_block(fake, web):
    assert web.post("/zotero/link", json={"on": True}).json()["ok"] is True
    assert load_manifest(CONFIG.vault_path).zotero.library == SERVER_ID

    web.post("/zotero/link", json={"on": False})
    assert load_manifest(CONFIG.vault_path).zotero is None


def test_a_library_linked_elsewhere_is_refused(fake, web, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    set_zotero(other, "library", SERVER_ID)
    monkeypatch.setattr("silica.kernel.recall.vault_registry.known_vaults", lambda: [other])

    r = web.post("/zotero/link", json={"on": True})

    assert r.status_code == 409 and "peek" in r.json()["detail"]
    assert load_manifest(CONFIG.vault_path).zotero is None


def test_settings_rows_write_the_block(fake, web):
    _link()

    web.post("/zotero/setting", json={"key": "backlinks", "value": False})
    web.post("/zotero/setting", json={"key": "inbox", "value": "00_inbox"})

    link = load_manifest(CONFIG.vault_path).zotero
    assert (link.backlinks, link.inbox) == (False, "00_inbox")
    assert web.post("/zotero/setting", json={"key": "library", "value": "x"}).status_code == 400


def test_grant_reports_whether_the_key_was_kept(fake, web, tmp_path, monkeypatch):
    _link()
    monkeypatch.setattr("silica.onboarding.wizard.resolve_env_path", lambda: tmp_path / ".env")

    assert web.post("/zotero/grant").json() == {"ok": True, "kept": True}


def test_a_cross_origin_post_is_refused(fake, web):
    r = web.post("/zotero/link", json={"on": True},
                 headers={"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"})

    assert r.status_code == 403


# --- Explore › Library --------------------------------------------------------------

@pytest.fixture
def library(fake, tmp_vault):
    _link()
    mem = fake.collection("03-agents-and-memory")
    inbox = fake.collection("00_inbox")
    fake.items["8H4AC8GW"]["data"]["collections"] = [mem["key"]]
    citing = fake.clone(fake.items["8H4AC8GW"], "CITING01", citationKey="xu2025", title="A-MEM")
    fake.clone(fake.items["8H4AC8GW"], "INBOX001", citationKey="new2026", title="New",
               collections=[inbox["key"]])
    tmp_vault.note(LEAF, "---\nzotero_key: 8H4AC8GW\narxiv: \"2310.08560\"\n---\n\npaper\n")
    tmp_vault.note("sources/@xu2025.md", "---\nzotero_key: CITING01\n---\n\n"
                   "## References\n\n[1] arXiv:2310.08560\n")
    return {"mem": mem["key"], "inbox": inbox["key"], "citing": citing}


def test_the_tree_counts_papers_in_the_vault_per_collection(library, web):
    data = web.get("/zotero/library").json()

    rows = {c["name"]: (c["in_vault"], c["total"]) for c in data["collections"]}
    assert rows["03-agents-and-memory"] == (2, 2)
    assert rows["00_inbox"] == (0, 1)
    assert (data["total"], data["in_vault"]) == (3, 2)


def test_a_collection_lists_state_and_how_often_the_library_cites_each_paper(library, web):
    rows = {r["key"]: r for r in web.get(f"/zotero/collection?key={library['mem']}").json()["rows"]}

    assert rows["8H4AC8GW"]["state"] == "in"
    assert rows["8H4AC8GW"]["cited_by"] == 1
    assert rows["8H4AC8GW"]["sur"] == "Packer" and rows["8H4AC8GW"]["year"] == "2024"
    assert rows["8H4AC8GW"]["leaf"] == LEAF
    assert rows["8H4AC8GW"]["citekey"] == "packerMemGPTLLMsOperating2024"


def test_the_inbox_collection_carries_two_suggestions(library, web, monkeypatch):
    set_zotero(CONFIG.vault_path, "inbox", "00_inbox")
    vecs = {"MemGPT: Towards LLMs as Operating Systems": [1, 0], "A-MEM": [0.9, 0.1], "New": [0.95, 0.05]}
    monkeypatch.setattr(zw, "_default_embed", lambda texts: [vecs[t.split("\n")[0]] for t in texts])

    data = web.get(f"/zotero/collection?key={library['inbox']}").json()

    [row] = data["rows"]
    assert data["inbox"] is True
    assert (row["key"], row["first"], row["votes"]) == ("INBOX001", library["mem"], 2)


def test_filing_from_the_library_moves_and_can_be_reverted(library, web, monkeypatch):
    set_zotero(CONFIG.vault_path, "inbox", "00_inbox")
    monkeypatch.setenv("SILICA_ZOTERO_API_KEY", "k" * 32)

    out = web.post("/zotero/file", json={"moves": [["INBOX001", library["mem"]]]}).json()

    assert out["moved"] == 1 and out["run"]
    from silica.cli import _handle_direct_shortcut
    _handle_direct_shortcut(f"/revert {out['run']}", [])


def test_groups_preview_then_write(library, web, monkeypatch):
    monkeypatch.setenv("SILICA_ZOTERO_API_KEY", "k" * 32)
    monkeypatch.setattr(zw, "compute_group_plan", lambda: {"memory · agents": ["8H4AC8GW", "CITING01"]})

    preview = web.get("/zotero/groups").json()
    assert preview["groups"] == [{"label": "memory · agents", "papers": 2, "now": 0}]

    out = web.post("/zotero/groups").json()
    assert out["changed"] == 2 and out["run"]


# --- the paper in the Node panel ---------------------------------------------------

def test_context_of_a_paper_carries_its_identity_and_its_citations(library, web, tmp_vault):
    tmp_vault.note("Concepts/Paging.md", "# Paging\n\n## Sources\n\n[[sources/@packerMemGPTLLMsOperating2024]]\n")
    tmp_vault.note("sources/@a.md", "---\nzotero_key: AAAA0001\n---\n\n## References\n\n[1] arXiv:2303.08774\n")
    tmp_vault.note(LEAF, "---\nzotero_key: 8H4AC8GW\ntitle: \"MemGPT: Towards LLMs as Operating Systems\"\n"
                   "authors:\n- \"Packer, Charles\"\nyear: 2024\narxiv: \"2310.08560\"\n---\n\n"
                   "## References\n\n[1] arXiv:2303.08774\n[2] arXiv:9999.00001\n")

    ctx = web.get("/context", params={"path": LEAF}).json()

    p = ctx["paper"]
    assert ctx["title"] == "MemGPT: Towards LLMs as Operating Systems"
    assert p["fm"]["zotero_key"] == "8H4AC8GW" and p["fm"]["year"] == 2024
    assert [r["path"] for r in p["cited_by"]] == ["sources/@xu2025.md"]
    assert p["cites"] == []
    assert p["missing"]["count"] == 2
    assert p["missing"]["top"][0]["label"] == "arXiv 2303.08774"     # cited by 2 library papers
    assert [n["path"] for n in p["notes"]] == ["Concepts/Paging.md"]
    assert p["collections"] == ["03-agents-and-memory"]


def test_a_papers_says_skips_its_author_block(library, web, tmp_vault):
    """The text before a paper's first section is its byline: on Cormack 2009
    the panel's first `Says` row read "G." (e2e run, 2026-10-05)."""
    tmp_vault.note(LEAF, "---\nzotero_key: 8H4AC8GW\ntitle: \"MemGPT\"\n---\n\n# MemGPT\n\n"
                   "C. Packer UC Berkeley " + "x " * 400 + "\n\n## Abstract\n\nMemGPT pages context.\n")

    says = web.get("/context", params={"path": LEAF}).json()["snippets"]

    assert [s["heading"] for s in says] == ["Abstract"]


def test_context_of_a_plain_note_has_no_paper(library, web, tmp_vault):
    tmp_vault.note("Concepts/Paging.md", "# Paging\nbody\n")

    assert "paper" not in web.get("/context", params={"path": "Concepts/Paging.md"}).json()


# --- the paper in the drawer -------------------------------------------------------

def test_the_drawer_titles_a_paper_by_its_title_and_carries_its_identity(web, tmp_vault):
    tmp_vault.note(LEAF, "---\nzotero_key: 8H4AC8GW\ntitle: \"MemGPT: Towards LLMs as Operating Systems\"\n"
                   "pdf_key: QH2BR53F\n---\n\nverbatim paper\n")

    d = web.get("/note", params={"path": LEAF}).json()

    assert d["title"] == "MemGPT: Towards LLMs as Operating Systems"
    assert d["paper"]["pdf_key"] == "QH2BR53F"


def test_a_footnote_renders_as_a_footnote_and_keeps_its_wikilink(web, tmp_vault):
    """A `## Sources` definition `[^L]: [[sources/X]]` rendered as a relative
    link that navigated away from the app: every note with a Sources block."""
    tmp_vault.note(LEAF, "---\nzotero_key: 8H4AC8GW\n---\n\npaper\n")
    tmp_vault.note("Concepts/Paging.md", "# Paging\n\nA claim.[^packer]\n\n## Sources\n\n"
                   "[^packer]: [[sources/@packerMemGPTLLMsOperating2024]]\n")

    html = web.get("/note", params={"path": "Concepts/Paging.md"}).json()["html"]

    assert 'class="footnote-ref"' in html
    assert 'data-path="sources/@packerMemGPTLLMsOperating2024.md"' in html
    assert 'href="[[' not in html and "%5B%5B" not in html


def test_paper_labels_name_each_leaf_by_surname_and_year(web, tmp_vault):
    tmp_vault.note(LEAF, "---\nzotero_key: 8H4AC8GW\nauthors:\n- \"Packer, Charles\"\nyear: 2024\n---\n\npaper\n")
    tmp_vault.note("Concepts/Paging.md", "# Paging\n")

    assert web.get("/zotero/labels").json() == {LEAF: "Packer 2024"}


def test_compare_takes_two_collections_with_the_notes_of_their_papers(library, web, tmp_vault, fake):
    """ADR-0055 "Scoping": two collections side by side. A paper leaf links to
    nothing, so a collection is its papers plus the notes that cite them, or the
    two sides could never touch."""
    other = fake.collection("04-graphs")
    fake.items["CITING01"]["data"]["collections"] = [other["key"]]
    tmp_vault.note("Concepts/Paging.md", "# Paging\n[[Concepts/Graph walk]]\n\n## Sources\n\n"
                   "[[sources/@packerMemGPTLLMsOperating2024]]\n")
    tmp_vault.note("Concepts/Graph walk.md", "# Graph walk\n\n## Sources\n\n[[sources/@xu2025]]\n")

    d = web.get("/compare", params={"a": "collection:03-agents-and-memory",
                                    "b": "collection:04-graphs"}).json()

    assert d["a"]["label"] == "03-agents-and-memory"
    assert set(d["a"]["paths"]) == {LEAF, "Concepts/Paging.md"}
    assert set(d["b"]["paths"]) == {"sources/@xu2025.md", "Concepts/Graph walk.md"}
