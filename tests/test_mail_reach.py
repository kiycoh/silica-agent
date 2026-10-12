"""ADR-0090 point 3, "Out of recall, on purpose": a mail note sits under
sources/, so every derived index skips it, and the chat reaches it only when
it asks for it.

Pinned because the reason is not the leaf rule's own (a mail note has no
distilled note to protect) but the missing gate on tainted writes. If the
leaf rule changes, as S3 of the 2026-09-17 audit proposes, these fail and
the mail decision is taken again rather than inherited."""
from __future__ import annotations

import json

import pytest

from silica.sources import mail
from silica.sources.registry import stage
from silica.tools import TOOLS
from silica.tools import atomic, composed  # noqa: F401  (registers the tools)
from tests.mail_fixtures import ME, ME_FULL, PROF, msg, write_eml


@pytest.fixture
def note(tmp_vault, tmp_path) -> str:
    tmp_vault.note("Concepts/Thesis.md", "# Thesis\n\nchapter plan and advisor meetings\n")
    mail.remember_addresses([ME])
    eml = write_eml(tmp_path / "thread.eml", msg(
        frm=ME_FULL, to=PROF, mid="<reach@x.example>", subject="Zanzibar chapter",
        plain="The zanzibar chapter goes to the advisor on Friday."))
    out = stage(mail.MAIL, str(eml))
    assert out["status"] == "ok"
    return out["note_path"]


def test_a_mail_note_is_in_no_index_built_on_the_file_list(note):
    """list_files feeds search, embeddings, co-occurrence, the autolink title
    index and the vault map: one exclusion covers them all."""
    from silica.driver import get_driver

    d = get_driver()
    assert all(r.path != note for r in d.list_files())
    assert not d.search_names("Zanzibar chapter")


def test_semantic_search_never_holds_a_mail_note(note, monkeypatch):
    class _Emb:
        def embed(self, texts, query=False):
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr("silica.agent.providers.get_embedder", lambda cfg: _Emb())
    from silica.kernel.recall.embed import get_store
    from silica.tools.composed import silica_embed_refresh

    assert "error" not in silica_embed_refresh(force=True)
    assert not any(note.rsplit("/", 1)[-1][:-3] in p for p in get_store().paths())


def test_the_chat_reaches_a_mail_note_when_it_asks(note):
    hits = json.loads(TOOLS["silica_search_context"].run(query="zanzibar advisor"))
    assert any(h["path"] == note for h in hits["hits"])
    body = TOOLS["silica_read_note"].run(name=note.rsplit("/", 1)[-1][:-3])
    assert "Friday" in body
