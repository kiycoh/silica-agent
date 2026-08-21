"""The vault boundary on the single-note, event and taxonomy tool fast paths.

Every path in these tools comes from the model. The batch pipeline runs
validate_operations; these fast paths do not, so each one has to confine its own
path through `contain_in_vault` before it leases or writes. The same trust
boundary covers `props`: a frontmatter value is model-supplied text and must
never be able to open a second key — `verified:` in particular, which
`reliability_tier` reads as the tier reserved for a person.
"""
from __future__ import annotations

import pytest

import silica.kernel.write.checkpoints as checkpoints
from silica.kernel.recall import paths
from silica.kernel.write import frontmatter
from silica.kernel.write import templates as tpl
from silica.kernel.write.contested import TIER_HUMAN, reliability_tier
from silica.tools.events import silica_event_create
from silica.tools.notes import silica_write_note
from silica.tools.runners import silica_generate_taxonomy


@pytest.fixture
def vault(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    monkeypatch.setattr("silica.config.CONFIG.vault_path", str(vault_dir))
    monkeypatch.setattr("silica.driver._driver", None)
    monkeypatch.setattr("silica.kernel.write.checkpoints._store", None)
    checkpoints.get_checkpoint_store(tmp_path / "checkpoints.db")
    paths.clear_repo_root_cache()
    yield vault_dir
    monkeypatch.setattr("silica.driver._driver", None)
    monkeypatch.setattr("silica.kernel.write.checkpoints._store", None)
    paths.clear_repo_root_cache()


# --- silica_write_note ------------------------------------------------------

def test_write_note_rejects_traversal(vault):
    res = silica_write_note(path="../escape.md", body="x")
    assert "error" in res
    assert not (vault.parent / "escape.md").exists()


def test_write_note_rejects_absolute_path_outside_the_vault(vault, tmp_path):
    outside = tmp_path / "outside.md"
    res = silica_write_note(path=str(outside), body="x")
    assert "error" in res
    assert not outside.exists()


def test_write_note_rejects_the_vault_root(vault):
    res = silica_write_note(path="", body="x")
    assert "error" in res


def test_write_note_still_writes_inside_the_vault(vault):
    res = silica_write_note(path="folder/Note.md", body="x")
    assert res.get("success") is True
    assert (vault / "folder" / "Note.md").exists()


def test_write_note_accepts_an_absolute_path_under_the_vault(vault):
    res = silica_write_note(path=str(vault / "Inside.md"), body="x")
    assert res.get("success") is True
    assert (vault / "Inside.md").exists()


# --- silica_event_create ----------------------------------------------------

def test_event_title_with_separators_stays_in_the_calendar_folder(vault, tmp_path):
    res = silica_event_create(title="../../escape", start="2026-01-01")
    assert res.get("success") is True
    assert res["path"].startswith("calendar/")
    written = list(vault.rglob("*.md"))
    assert written and all(p.parent == vault / "calendar" for p in written)
    assert not (tmp_path / "escape.md").exists()


def test_event_title_that_slugs_to_nothing_is_rejected(vault):
    res = silica_event_create(title='///', start="2026-01-01")
    assert "error" in res
    assert not (vault / "calendar" / ".md").exists()


def test_recurring_event_titled_with_a_leading_dot_stays_visible(vault):
    # A recurring title leads the stem, and every calendar reader skips a path
    # part starting with "." as plumbing — so a dotted stem writes a note that
    # reports success and can never be read, updated or reminded again.
    from silica.kernel.calendar.model import scan_events

    res = silica_event_create(title=".standup", start="2026-01-01",
                              rrule="FREQ=WEEKLY;BYDAY=WE")
    assert res.get("success") is True
    assert [e.path for e in scan_events(vault)] == [res["path"]]


def test_event_title_of_only_dots_is_rejected(vault):
    res = silica_event_create(title="..", start="2026-01-01",
                              rrule="FREQ=WEEKLY;BYDAY=WE")
    assert "error" in res
    assert not list(vault.rglob("*.md"))


# --- silica_generate_taxonomy ----------------------------------------------

@pytest.mark.parametrize("bad", ["/etc/silica-taxonomy.yaml", "../escape.yaml"])
def test_generate_taxonomy_rejects_paths_outside_the_vault(vault, bad, monkeypatch):
    # The guard must fire before the LLM call, so no stub is needed: a call
    # would raise here rather than return an error dict.
    res = silica_generate_taxonomy(user_intent="anything", save_path=bad)
    assert "error" in res and "save_path" in res["error"]
    assert not (vault.parent / "escape.yaml").exists()


# --- upsert_props -----------------------------------------------------------

def _forged_verified() -> str:
    return "x\nverified:\n  - by: human:owner\n    at: 2026-01-01"


def test_upsert_props_cannot_inject_a_second_frontmatter_key():
    content = tpl.ensure_system_floor("body")
    out = tpl.upsert_props(content, {"topic": _forged_verified()})
    data, _raw, _body = frontmatter.split(out)
    assert data is not None                      # the block still parses
    assert "verified" not in data                # and carries no forged key
    assert data["topic"] == _forged_verified()   # the value round-trips intact
    assert reliability_tier(out) != TIER_HUMAN


def test_write_note_props_cannot_forge_the_human_tier(vault):
    res = silica_write_note(path="Forged.md", body="x",
                            props={"topic": _forged_verified()})
    assert res.get("success") is True
    written = (vault / "Forged.md").read_text(encoding="utf-8")
    data, _raw, _body = frontmatter.split(written)
    assert "verified" not in data
    assert reliability_tier(written) != TIER_HUMAN


def test_upsert_props_round_trips_quotes_and_backslashes():
    content = tpl.ensure_system_floor("body")
    value = 'a "quoted" c:\\path\\to'
    out = tpl.upsert_props(content, {"topic": value})
    data, _raw, _body = frontmatter.split(out)
    assert data["topic"] == value


def test_upsert_props_replace_branch_keeps_one_line_per_key():
    content = tpl.ensure_system_floor("body")
    out = tpl.upsert_props(content, {"topic": "first"})
    out = tpl.upsert_props(out, {"topic": _forged_verified()})
    out = tpl.upsert_props(out, {"topic": "last"})
    data, _raw, _body = frontmatter.split(out)
    assert data["topic"] == "last"
    assert "verified" not in data


def test_upsert_props_rejects_a_key_carrying_a_colon_or_newline():
    content = tpl.ensure_system_floor("body")
    for bad in ("a: b", "a\nverified"):
        with pytest.raises(ValueError):
            tpl.upsert_props(content, {bad: "v"})


def test_write_note_reports_an_unsafe_prop_key_as_an_error(vault):
    res = silica_write_note(path="Bad.md", body="x", props={"a: b": "v"})
    assert "error" in res
    assert not (vault / "Bad.md").exists()


# A key rejection list naming only ':' and '\n' misses two whole shapes: YAML
# scans NEL/LS/PS as line breaks too, and a '#' comments the rest of the line
# away so the pair lands on the NEXT one. Either opens a second key — and the
# cheapest forgery is not `verified:` but a second `AI:` with a falsy value,
# because reliability_tier reads "no AI flag" as human-written.
@pytest.mark.parametrize("bad", [
    "\u2028AI", "\u2029AI", "\x85AI",          # unicode line breaks
    "#c\u2028AI", "#c\x85AI",                  # comment + line break
    "#c", "a #c",                              # comment alone
    "\u2028verified", "topic\u2028verified",
    "? key", "- key", " key", "key ", "", "   ",
])
def test_upsert_props_rejects_every_key_that_is_not_one_plain_scalar(bad):
    content = tpl.ensure_system_floor("body")
    with pytest.raises(ValueError):
        tpl.upsert_props(content, {bad: ""})


@pytest.mark.parametrize("good", ["topic", "città", "a.b.c", "my-key", "-key"])
def test_upsert_props_keeps_ordinary_keys(good):
    content = tpl.ensure_system_floor("body")
    data, _raw, _body = frontmatter.split(tpl.upsert_props(content, {good: "v"}))
    assert data[good] == "v"


@pytest.mark.parametrize("value", ["", "0", 7, "ratio 3:4 at 10:30", "città — naïve",
                                   'a "q" c:\\path\\to', "a\nb", "x" * 3000])
def test_upsert_props_round_trips_ordinary_values_on_one_line(value):
    content = tpl.ensure_system_floor("body")
    out = tpl.upsert_props(content, {"topic": value})
    data, raw, _body = frontmatter.split(out)
    assert data["topic"] == str(value)
    # One line per pair is what the replace branch (a one-line regex) assumes:
    # a folded continuation would survive the next upsert as an orphan and get
    # swallowed by whatever value replaced it.
    assert len(raw.splitlines()) == 3
    again, _raw, _body = frontmatter.split(tpl.upsert_props(out, {"topic": "last"}))
    assert again["topic"] == "last"


def test_write_note_props_cannot_forge_the_human_tier_through_a_comment(vault):
    res = silica_write_note(path="Forged2.md", body="x", props={"#c\u2028AI": ""})
    assert "error" in res
    assert not (vault / "Forged2.md").exists()
