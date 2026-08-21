# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Guards for the 2026-08-19 debt paydown: `AI: partial` provenance, leaked
tool-call container coercion, inline-code-span sparing in the OFM strip,
quiz-log staleness in the report memo key, the reminder delivery lock, and
the wizard's live /models validation."""

import threading
import time

from silica.kernel.link.ofm import ofm_lint
from silica.kernel.write.contested import (
    TIER_HUMAN,
    reliability_tier,
)
from silica.kernel.write.templates import ensure_ai_flag


LEGACY = "---\ntags: [x]\n---\n\n# Mine\n\nMy own prose.\n"


# --- AI: partial ------------------------------------------------------------

def test_partial_stamp_lands_and_is_idempotent():
    stamped = ensure_ai_flag(LEGACY, value="partial")
    assert "AI: partial" in stamped.split("---")[1]
    assert ensure_ai_flag(stamped, value="partial") == stamped
    # A later full-authorship stamp never upgrades an earlier partial.
    assert ensure_ai_flag(stamped) == stamped


def test_partial_note_keeps_the_human_tier():
    stamped = ensure_ai_flag(LEGACY, value="partial")
    assert reliability_tier(stamped) == TIER_HUMAN
    # Case/space tolerant: hand-edited variants read the same.
    assert reliability_tier(LEGACY.replace("---\n\n", "AI: Partial\n---\n\n", 1)) == TIER_HUMAN


def test_full_ai_stamp_still_demotes():
    assert reliability_tier(ensure_ai_flag(LEGACY)) < TIER_HUMAN


def test_lint_accepts_partial_and_still_rejects_other_strings():
    def violations(ai_line):
        content = f"---\ntags: [x]\nAI: {ai_line}\nlast modified: 2026-08-19\n---\n\n# T\n\nbody [[Hub]]\n"
        return [v for v in ofm_lint(content)["violations"] if "AI" in str(v)]

    assert violations("partial") == []
    assert violations("true") == []
    assert violations("maybe") != []


# --- leaked tool-call params ------------------------------------------------

def test_leaked_containers_parse_and_scalars_stay_strings():
    import json

    from silica.agent.llm import recover_leaked_tool_calls

    body = (
        '<DSMLtool_calls><DSMLinvoke name="t">'
        '<DSMLparameter name="refs">["a", "b"]</DSMLparameter>'
        '<DSMLparameter name="k">5</DSMLparameter>'
        '<DSMLparameter name="q">plain text</DSMLparameter>'
        '<DSMLparameter name="bad">[unclosed</DSMLparameter>'
        "</DSMLinvoke></DSMLtool_calls>"
    )
    _content, calls = recover_leaked_tool_calls(body)
    assert len(calls) == 1
    args = json.loads(calls[0][2])
    assert args["refs"] == ["a", "b"]      # container: parsed
    assert args["k"] == "5"                # scalar: left for pydantic lax
    assert args["q"] == "plain text"
    assert args["bad"] == "[unclosed"      # broken JSON: kept verbatim


# --- OFM strip vs inline code spans ----------------------------------------

def test_strip_ofm_spares_inline_code_spans():
    from silica.ui.web.server import _strip_ofm_meta

    src = "keep `a %% b` and drop %%this%% but keep ``x %% y``\n"
    out = _strip_ofm_meta(src)
    assert "`a %% b`" in out
    assert "``x %% y``" in out
    assert "%%this%%" not in out

    fenced = "```\ncode %% stays\n```\nprose %%goes%% here\n"
    out = _strip_ofm_meta(fenced)
    assert "code %% stays" in out
    assert "%%goes%%" not in out


def test_strip_ofm_spares_block_ids_in_spans():
    from silica.ui.web.server import _strip_ofm_meta

    out = _strip_ofm_meta("code `line ^ref` prose ^blockid\n")
    assert "`line ^ref`" in out
    assert "^blockid" not in out


# --- report memo key vs quiz log -------------------------------------------

def test_report_memo_key_moves_when_the_quiz_log_does(tmp_path, monkeypatch):
    from silica.kernel.report import quiz
    from silica.kernel.report.graph_report import compute

    log = tmp_path / "quiz.jsonl"
    monkeypatch.setattr(quiz, "log_path", lambda: log)

    before = compute._index_stores_sig(False, False, analytics=True)
    log.write_text('{"path": "a.md", "correct": true}\n')
    after = compute._index_stores_sig(False, False, analytics=True)
    assert before != after
    # Without analytics the quiz log is not part of the key at all.
    assert compute._index_stores_sig(False, False) == ()


# --- reminder delivery lock -------------------------------------------------

def test_delivery_lock_serializes_two_tickers(tmp_path):
    from silica.kernel.calendar.reminders import delivery_lock

    order: list[str] = []

    def hold_then_release():
        with delivery_lock(tmp_path):
            order.append("a-in")
            time.sleep(0.15)
            order.append("a-out")

    t = threading.Thread(target=hold_then_release)
    t.start()
    time.sleep(0.05)  # let the thread take the lock first
    with delivery_lock(tmp_path):
        order.append("b-in")
    t.join()
    assert order == ["a-in", "a-out", "b-in"]


# --- model_limits TTL -------------------------------------------------------

def test_model_limits_ttl_serves_hits_and_expires(monkeypatch):
    import time as _time

    import silica.agent.providers as providers

    calls = {"n": 0}

    def fetch(p, m):
        calls["n"] += 1
        return (100, 10)

    monkeypatch.setattr(providers, "_model_limits_fetch", fetch)
    clock = {"t": 1000.0}
    monkeypatch.setattr(_time, "monotonic", lambda: clock["t"])
    providers._model_limits_memo.clear()

    assert providers.model_limits("x", "m") == (100, 10)
    assert providers.model_limits("x", "m") == (100, 10)
    assert calls["n"] == 1  # within the TTL: memo hit, no probe

    clock["t"] += providers._MODEL_LIMITS_TTL_S + 1
    assert providers.model_limits("x", "m") == (100, 10)
    assert calls["n"] == 2  # expired: refetched (a reloaded model is seen)
    providers._model_limits_memo.clear()


# --- energy series ----------------------------------------------------------

def test_energy_series_appends_only_on_change(tmp_path):
    from silica.kernel.report.graph_report.render import append_energy_point

    s = tmp_path / "energy.jsonl"
    rec = {"value": 5.0, "terms": {"orphans": 2}, "at": "2026-08-19T12:00:00"}
    assert append_energy_point(s, rec, None, None) is True          # first point
    assert append_energy_point(s, rec, 5.0, {"orphans": 2}) is False  # no move
    rec2 = dict(rec, value=4.5)
    assert append_energy_point(s, rec2, 5.0, {"orphans": 2}) is True
    assert len(s.read_bytes().splitlines()) == 2


# --- codepack usage tripwire ------------------------------------------------

def test_code_pack_usage_stamp(tmp_path, monkeypatch):
    import silica.kernel.recall.paths as paths
    from silica.tools import codedocs_tool

    monkeypatch.setattr(paths, "index_dir_for", lambda v: tmp_path)
    codedocs_tool._stamp_code_pack_use()
    codedocs_tool._stamp_code_pack_use()
    assert len((tmp_path / "codepack_usage.log").read_text().splitlines()) == 2


# --- wizard live /models ----------------------------------------------------

def test_live_models_drops_stale_ids(monkeypatch):
    import silica.onboarding.wizard as wizard

    curated = ["groq/alive-model", "groq/dead-model"]
    monkeypatch.setattr(
        wizard, "endpoint_model_ids", lambda base, api_key="": ["alive-model", "new-model"])
    assert wizard._live_hosted_models("groq", "k", curated) == ["groq/alive-model"]


def test_live_models_normalizes_gemini_prefix(monkeypatch):
    import silica.onboarding.wizard as wizard

    monkeypatch.setattr(
        wizard, "endpoint_model_ids",
        lambda base, api_key="": ["models/gemini-2.5-flash"])
    assert wizard._live_hosted_models(
        "gemini", "k", ["gemini/gemini-2.5-flash"]) == ["gemini/gemini-2.5-flash"]


def test_live_models_full_rot_offers_live_ids(monkeypatch):
    import silica.onboarding.wizard as wizard

    monkeypatch.setattr(
        wizard, "endpoint_model_ids", lambda base, api_key="": ["m1", "m2"])
    assert wizard._live_hosted_models("openai", "k", ["openai/gone"]) == [
        "openai/m1", "openai/m2"]


def test_live_models_unreachable_falls_back_to_curated(monkeypatch):
    import silica.onboarding.wizard as wizard

    curated = ["xai/grok-2-latest"]
    monkeypatch.setattr(wizard, "endpoint_model_ids", lambda base, api_key="": [])
    assert wizard._live_hosted_models("xai", "k", curated) == curated
