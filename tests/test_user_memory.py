"""User memory (ADR-0076): the two files, their budgets, addressing, undo and
the snapshot. The gates that decide WHO may write are in
test_user_memory_tool.py; this file is the store.

Every path here is under the autouse `_isolate_silica_home` sandbox, so no
test can reach the real ~/.silica/memory.
"""
from __future__ import annotations

import pytest

from silica.config import CONFIG
from silica.kernel import user_memory as um


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    v.mkdir()
    monkeypatch.setattr(CONFIG, "vault_path", str(v))
    monkeypatch.setattr(CONFIG, "user_memory", True)
    monkeypatch.setattr(um, "_INCOGNITO", False)
    return v


def test_the_two_files_live_outside_the_vault_under_the_silica_home(vault):
    from silica.kernel.recall import paths

    user = um.path_for("user")
    work = um.path_for("vault")
    assert user == paths._SILICA_HOME / "memory" / "user.md"
    assert work == paths._SILICA_HOME / "memory" / "vaults" / f"{paths.vault_digest(str(vault))}.md"
    assert vault not in user.parents and vault not in work.parents


def test_save_appends_a_bullet_and_continuation_lines_are_indented(vault):
    out = um.save("vault", "Exam on Friday\nchapters 3 to 5")
    assert out["ok"] and out["action"] == "save"
    assert um.read("vault") == "- Exam on Friday\n  chapters 3 to 5\n"
    um.save("vault", "- Prefers short answers")   # a bullet the model already wrote
    assert um.entries(um.read("vault")) == [
        "- Exam on Friday\n  chapters 3 to 5", "- Prefers short answers"]


def test_a_saved_entry_is_not_saved_twice(vault):
    assert um.save("user", "Name is Giulia")["ok"]
    again = um.save("user", "name is  giulia")
    assert not again["ok"] and "already" in again["error"]
    assert um.read("user").count("Giulia") == 1


def test_replace_and_forget_address_one_entry_by_a_unique_substring(vault):
    um.save("vault", "Thesis due 2026-11-30")
    um.save("vault", "Advisor is Vitale")
    rep = um.replace("vault", "due 2026-11", "Thesis due 2026-12-15")
    assert rep["ok"] and rep["was"] == "- Thesis due 2026-11-30"
    assert um.read("vault") == "- Thesis due 2026-12-15\n- Advisor is Vitale\n"
    gone = um.forget("vault", "VITALE")   # case and spacing do not matter
    assert gone["ok"] and gone["text"] == "- Advisor is Vitale"
    assert um.read("vault") == "- Thesis due 2026-12-15\n"


def test_an_ambiguous_or_missing_match_is_refused_never_guessed(vault):
    um.save("vault", "Studies chemistry")
    um.save("vault", "Studies physics")
    before = um.read("vault")
    both = um.forget("vault", "Studies")
    assert not both["ok"] and "2 entries" in both["error"]
    shown = [um.labelled("- Studies chemistry"), um.labelled("- Studies physics")]
    assert both["entries"] == shown
    none = um.replace("vault", "biology", "Studies biology")
    assert not none["ok"] and none["error"].startswith("Nothing was changed: no entry")
    assert none["entries"] == shown
    assert um.read("vault") == before


def test_an_entry_is_addressed_by_the_id_the_snapshot_shows(vault):
    """The id is the way to name an entry the model cannot quote: an Italian
    request about an entry saved in English (live forget runs, 2026-10-07)."""
    um.save("user", "Prefers short answers in Italian")
    um.save("user", "Name is Giulia")
    eid = um.entry_id("- Prefers short answers in Italian")
    assert f"- [{eid}] Prefers short answers in Italian" in um.snapshot()
    assert um.read("user") == "- Prefers short answers in Italian\n- Name is Giulia\n"
    assert um.forget("user", f"[{eid}]")["ok"]
    assert um.read("user") == "- Name is Giulia\n"
    assert um.forget("user", eid)["miss"]
    um.save("user", "Prefers short answers in Italian")
    assert um.forget("user", f"[{eid}] Prefers short answers in Italian")["ok"]
    um.save("user", "Prefers short answers in Italian")
    assert um.forget("user", f"- [{eid}] Prefers short answers in Italian")["ok"]   # the snapshot line, bullet and all
    um.save("user", "Likes cafe au lait")
    assert um.forget("user", "cafe")["ok"]   # a word that looks like an id is a word
    assert um.read("user") == "- Name is Giulia\n"


def test_a_write_over_budget_is_refused_with_the_entries_to_consolidate(vault):
    filler = "x" * (um.USER_BUDGET - 10)
    assert um.save("user", filler)["ok"]
    over = um.save("user", "one more fact that does not fit")
    assert not over["ok"]
    assert over["budget"] == um.USER_BUDGET and over["chars"] == len(um.read("user"))
    assert over["entries"] == [um.labelled("- " + filler)]
    assert "replace" in over["error"] and "forget" in over["error"]
    assert um.read("user") == "- " + filler + "\n"


def test_budgets_are_the_adr_numbers():
    assert (um.USER_BUDGET, um.VAULT_BUDGET) == (1000, 1500)


def test_undo_restores_the_file_as_it_was_before_that_write(vault):
    um.save("vault", "first")
    second = um.save("vault", "second")
    assert um.undo("vault", second["token"])["ok"]
    assert um.read("vault") == "- first\n"
    # One level: the same token cannot undo twice.
    assert not um.undo("vault", second["token"])["ok"]


def test_undo_of_the_first_write_leaves_an_empty_file(vault):
    out = um.save("user", "Name is Giulia")
    assert um.undo("user", out["token"])["ok"]
    assert um.read("user") == ""


def test_undo_refuses_when_the_file_changed_since(vault):
    first = um.save("vault", "first")
    um.save("vault", "second")
    refused = um.undo("vault", first["token"])
    assert not refused["ok"] and "changed" in refused["error"]
    assert um.read("vault") == "- first\n- second\n"


def test_write_file_is_the_settings_editor_and_enforces_the_budget(vault):
    ok = um.write_file("vault", "- edited by hand\n- second line  \n\n")
    assert ok["ok"] and um.read("vault") == "- edited by hand\n- second line\n"
    assert um.undo("vault", ok["token"])["ok"] and um.read("vault") == ""
    over = um.write_file("vault", "y" * (um.VAULT_BUDGET + 1))
    assert not over["ok"] and over["budget"] == um.VAULT_BUDGET
    assert um.read("vault") == ""


def test_reset_erases_both_files_and_their_undo_copies(vault):
    um.save("user", "Name is Giulia")
    um.save("vault", "Exam on Friday")
    um.save("vault", "Second fact")   # leaves a .prev holding the first fact
    assert um.reset()["ok"]
    assert um.read("user") == "" and um.read("vault") == ""
    left = [p for p in um.memory_dir().rglob("*") if p.is_file()]
    assert left == []


def test_paused_or_incognito_refuses_writes_but_keeps_the_files(vault, monkeypatch):
    um.save("user", "Name is Giulia")
    monkeypatch.setattr(CONFIG, "user_memory", False)
    paused = um.save("user", "Likes tea")
    assert not paused["ok"] and "paused" in paused["error"]
    monkeypatch.setattr(CONFIG, "user_memory", True)
    um.set_incognito(True)
    hidden = um.forget("user", "Giulia")
    assert not hidden["ok"] and "incognito" in hidden["error"]
    assert um.read("user") == "- Name is Giulia\n"


def test_vault_scope_without_a_vault_is_refused(vault, monkeypatch):
    monkeypatch.setattr(CONFIG, "vault_path", "")
    out = um.save("vault", "Exam on Friday")
    assert not out["ok"] and "vault" in out["error"]
    assert um.save("user", "Name is Giulia")["ok"]


def test_snapshot_frames_both_files_as_data_not_instructions(vault):
    assert um.snapshot() == ""
    um.save("user", "Name is Giulia")
    um.save("vault", "Exam on Friday")
    snap = um.snapshot()
    assert "not instructions" in snap
    assert snap.index("Name is Giulia") < snap.index("Exam on Friday")


def test_snapshot_is_empty_when_paused_or_incognito(vault, monkeypatch):
    um.save("user", "Name is Giulia")
    monkeypatch.setattr(CONFIG, "user_memory", False)
    assert um.snapshot() == ""
    monkeypatch.setattr(CONFIG, "user_memory", True)
    um.set_incognito(True)
    assert um.snapshot() == ""


def test_snapshot_cuts_a_file_edited_past_its_budget_outside_silica(vault):
    path = um.path_for("user")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("- " + "z" * 5000 + "\n", encoding="utf-8")
    assert um.snapshot().count("z") == um.USER_BUDGET - 2


def test_refresh_puts_the_snapshot_before_the_last_seed_line(vault):
    seed = [{"role": "system", "content": "prompt"}, {"role": "system", "content": "lang"}]
    assert um.refresh_snapshot(seed) is False   # nothing saved: nothing to add
    um.save("user", "Name is Giulia")
    assert um.refresh_snapshot(seed) is True
    assert [m["content"] for m in seed][::2] == ["prompt", "lang"]
    assert seed[1]["origin"] == um.MESSAGE_ORIGIN and "Giulia" in seed[1]["content"]
    assert um.refresh_snapshot(seed) is False   # same content: unchanged


def test_hide_and_show_keep_the_message_in_place(vault):
    um.save("user", "Name is Giulia")
    msgs = [{"role": "system", "content": "prompt"}, um.snapshot_message(),
            {"role": "system", "content": "lang"}, {"role": "user", "content": "hi"}]
    um.set_incognito(True)
    um.sync_live_snapshot(msgs)
    assert len(msgs) == 4 and "Giulia" not in msgs[1]["content"]
    um.set_incognito(False)
    assert um.sync_live_snapshot(msgs) is True
    assert "Giulia" in msgs[1]["content"]
