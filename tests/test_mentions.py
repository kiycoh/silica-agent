"""ADR-0097 point 2: the notes a message names reach the model as resolved paths."""
from __future__ import annotations

from silica.agent.mentions import CAP, named_notes, refs, resolve, strip

VAULT = {
    "Concepts/ML/Machine learning.md": "Machine learning",
    "Concepts/AI/Deep learning.md": "Deep learning",
    "Projects/A/Index.md": "Index",
    "Projects/B/Index.md": "Index",
    "Teorema di Bayes.md": "Teorema di Bayes",
}


def test_refs_reads_targets_and_skips_embeds_attachments_and_repeats():
    text = ("[[Machine learning]] and [[Machine learning|ML]], "
            "[[Deep learning#History]], ![[figure.png]], [[paper.pdf]], [[Note.md]], "
            "[[Industria 4.0]], [[Python 3.11]]")
    assert refs(text) == ["Machine learning", "Deep learning", "Note.md", "Industria 4.0", "Python 3.11"]


def test_resolve_by_path_partial_path_and_name():
    assert resolve("Concepts/ML/Machine learning", VAULT) == ["Concepts/ML/Machine learning.md"]
    assert resolve("concepts/ml/machine learning.md", VAULT) == ["Concepts/ML/Machine learning.md"]
    assert resolve("ML/Machine learning", VAULT) == ["Concepts/ML/Machine learning.md"]
    assert resolve("teorema di bayes", VAULT) == ["Teorema di Bayes.md"]
    assert resolve("Index", VAULT) == ["Projects/A/Index.md", "Projects/B/Index.md"]
    assert resolve("Nowhere", VAULT) == []


def test_the_block_names_found_missing_and_ambiguous():
    block = named_notes("Compare [[Machine learning]] with [[Deep learning]], see [[Index]] and [[Foo]]", VAULT)
    assert block.startswith("<named-notes>\n") and block.endswith("\n</named-notes>")
    assert "Concepts/ML/Machine learning.md · Concepts/AI/Deep learning.md" in block
    assert "Not found in the vault: [[Foo]]." in block
    assert "[[Index]] (Projects/A/Index.md, Projects/B/Index.md)" in block


def test_no_link_no_block_and_no_vault_read(monkeypatch):
    import silica.agent.mentions as m

    def boom():
        raise AssertionError("a message with no link must not read the vault")

    monkeypatch.setattr(m, "_vault_titles", boom)
    assert named_notes("what is machine learning?") == ""


def test_the_cap_says_how_many_it_left():
    text = " ".join(f"[[n{i}]]" for i in range(CAP + 3))
    block = named_notes(text, {f"n{i}.md": f"n{i}" for i in range(CAP + 3)})
    assert block.count(".md") == CAP
    assert "And 3 more links not resolved." in block


def test_an_unreadable_vault_sends_the_turn_without_a_block(monkeypatch):
    import silica.agent.mentions as m

    def broken():
        raise RuntimeError("no vault")

    monkeypatch.setattr(m, "_vault_titles", broken)
    assert named_notes("[[Machine learning]]") == ""


def test_strip_gives_back_what_the_user_typed():
    typed = "Explain [[Machine learning]]"
    sent = typed + "\n\n" + named_notes(typed, VAULT) + "\n\n[image attached: a.png]"
    assert strip(sent) == typed + "\n\n[image attached: a.png]"


def test_a_gui_turn_carries_the_paths_and_a_reopened_chat_shows_the_words(tmp_vault, monkeypatch):
    import json

    from fastapi.testclient import TestClient

    from silica.ui.web import server

    tmp_vault.note("Concepts/ML/Machine learning.md", "# Machine learning\n")
    server._reset_session()
    tc = TestClient(server.app)
    seen = {}

    def fake_run_agent(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        seen["content"] = messages[-1]["content"]
        messages.append({"role": "assistant", "content": "Ok."})
        return "Ok."

    monkeypatch.setattr(server, "run_agent", fake_run_agent)
    r = tc.post("/chat", json={"text": "Spiega [[Machine learning]]"})
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert events[-1]["type"] == "done"
    assert seen["content"].startswith("Spiega [[Machine learning]]\n\n<named-notes>")
    assert "Concepts/ML/Machine learning.md" in seen["content"]
    user = next(m for m in tc.get("/messages").json() if m["role"] == "user")
    assert user["content"] == "Spiega [[Machine learning]]"
