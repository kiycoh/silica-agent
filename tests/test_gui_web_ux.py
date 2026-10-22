"""The web UI against the qm audit (docs/audits/2026-09-20-qm-vs-silica-web-ui.md).

One test per acceptance criterion the audit names, in the audit's own words.
Source-grep where the criterion is a property of the static files, TestClient
where it is a property of a route or a stream frame. The joins come from
tests/webassets.py so a cut of app.js reads as one string.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading

import pytest

from tests.webassets import WEB, app_css, app_js

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

INDEX = (WEB / "index.html").read_text(encoding="utf-8")
WORK = (WEB / "work.js").read_text(encoding="utf-8")


@pytest.fixture
def client(tmp_vault, tmp_path, monkeypatch):
    from silica.ui.web import server

    server._reset_session()
    return TestClient(server.app), server


def _fn(js: str, name: str, indent: str = "") -> str:
    """The body of a function, up to the closing brace at its own indent."""
    i = js.index(name)
    end = "\n" + indent + "}\n"
    return js[i:js.index(end, i) + len(end)]


def _block(html: str, open_tag: str, close_tag: str) -> str:
    start = html.index(open_tag)
    return html[start:html.index(close_tag, start)]


# --- P5. Transcript viewport ------------------------------------------------

def test_a_two_pixel_upward_scroll_stops_following():
    js = app_js()
    assert "const FOLLOW_TOL = 1" in js, "the tolerance is not the study's one pixel"
    assert js.count("log.scrollTop = log.scrollHeight") == 1, \
        "a second site snaps the transcript to the bottom unconditionally"
    body = _fn(js, "function followBottom()")
    assert "if (following)" in body
    assert 'log.addEventListener("wheel"' in js and "e.deltaY < 0" in js
    # the reader's own upward move flips it, content growing under a set
    # position does not
    assert "log.scrollTop < lastSetTop - FOLLOW_TOL" in js


def test_the_jump_pill_appears_only_while_not_following_a_live_stream():
    js = app_js()
    body = _fn(js, "function syncJump()")
    assert "jumpBtn.hidden = !(streaming && !following)" in body
    assert 'id="jump-latest"' in INDEX


def test_the_last_transcript_row_is_fully_visible_at_scroll_end():
    css = app_css()
    scrim = re.search(r"#composer::before \{[^}]*height: ([\d.]+)rem", css)
    last = re.findall(r"\.msg:last-child \{[^}]*padding-bottom: ([\d.]+)rem", css)
    assert scrim and last, "the scrim or the last-row rule moved"
    assert float(last[-1]) >= float(scrim.group(1)), \
        "the last row ends under the composer's scrim"


def test_read_notes_are_unique_by_resolved_path():
    """The ledger names a note read and not cited once, however the tool's
    argument spelled it (ADR-0117 point 2 took the reads out of the footer)."""
    js = app_js()
    body = _fn(js, "function uniqueReads(refs)")
    assert "resolveRef(ref)" in body and "new Set()" in body
    assert "function resolveRef(ref)" in js
    assert "function readChips(" not in js, "the read row is back in the footer"


def test_the_copy_button_is_visible_when_focused_by_keyboard():
    css = app_css()
    assert re.search(r"\.msg:focus-within \.copy-btn,\s*\.copy-btn:focus-visible \{[^}]*opacity: 1", css)


def test_every_copy_button_anchors_its_own_hit_area():
    """The hit-area ::after is 100% of its containing block (app-work.css): a
    copy button left static on the ledger handed it the whole answer, and any
    click on the text copied the turn (2026-10-16)."""
    css = app_css()
    for rule in re.findall(r"[^{}]*\.copy-btn\s*\{[^}]*\}", css):
        assert "position: static" not in rule, rule.strip()


# --- P2. Enter during a turn queues; the draft persists -----------------------

def test_enter_while_streaming_queues_instead_of_dropping():
    js = app_js()
    body = js[js.index('$("#composer").addEventListener("submit"'):]
    body = body[:body.index("\n});\n")]
    assert "if (streaming) { queueMessage(t); syncSend(); return; }" in body
    assert "if (streaming) return;" not in body, "Enter is swallowed again"
    assert 'id="queue"' in INDEX and "sends when this turn ends" in INDEX


def test_the_queued_message_is_sent_once_the_turn_settles():
    js = app_js()
    fin = js[js.index("} finally {\n    streaming = false;"):]
    fin = fin[:fin.index("\n  function handle(ev)")]
    assert "sendQueued();" in fin, "the finally of runTurn never drains the queue"


def test_send_now_stops_then_sends():
    js = app_js()
    body = js[js.index('$("#q-now").addEventListener("click"'):]
    body = body[:body.index("\n});\n")]
    # The button only stops; the settle path sends. Stop precedes send by
    # construction, so the order cannot depend on timing.
    assert 'fetch("/stop"' in body
    assert "send(" not in body.split("fetch")[1]


def test_a_reload_restores_the_draft_under_what_was_typed_since():
    js = app_js()
    body = _fn(js, "function restoreDraft(sid)")
    assert 'typed.replace(/\\s+$/, "") + "\\n" + d' in body
    assert 'window.addEventListener("pagehide", saveDraft)' in js
    assert "const DRAFT_MS = 400" in js


# --- P3. Stop is an outcome; failure rides the message -------------------------

def test_a_stopped_turn_keeps_the_partial_answer_and_adds_an_outcome_row(client, monkeypatch):
    tc, server = client

    def stops(messages, model, tool_progress_callback=None, cancel_token=None, **kw):
        cancel_token.set()
        return "(silica: cancelled)"

    monkeypatch.setattr(server, "run_agent", stops)

    async def collect():
        return [item async for item in server.run_turn("hi")]

    items = asyncio.run(collect())
    done = items[-1]
    assert done["type"] == "done" and done.get("stopped") is True
    js = app_js()
    # the client draws the row and never the sentinel: the stopped branch
    # returns before the html upgrade that would replace the partial text
    branch = js[js.index("if (ev.stopped) {"):js.index("// Uninterrupted answer")]
    assert 'className = "turn-outcome"' in branch and "return;" in branch
    assert "ev.html" not in branch and "ev.answer" not in branch
    assert "stopped by you after" in branch and "continue from here" in branch


def test_no_route_serialises_str_exc_into_a_user_facing_field():
    text = (WEB.parent / "server.py").read_text(encoding="utf-8")
    # _skip_reason is the one exemption: it hands the converter's text to the
    # agent turn on a laptop, where the paths in it are the owner's own, and on
    # a hosted tenant it returns a fixed sentence instead (ADR-0085 point 5).
    start = text.index("def _skip_reason(")
    text = text[:start] + text[text.index("\n\n\n", start):]
    assert "str(exc)" not in text, "a route hands the raw exception to the person again"
    # A job's failure reaches the person too (ADR-0099), through `_sentence`,
    # the same exemption for the same reason as `_skip_reason`.
    jobs = (WEB.parent / "jobs.py").read_text(encoding="utf-8")
    start = jobs.index("def _sentence(")
    jobs = jobs[:start] + jobs[jobs.index("\n\n\n", start):]
    assert "str(exc)" not in jobs, "a job hands the raw exception to the person"
    from silica.ui.web.failures import explain
    out = explain(RuntimeError("HTTPError 502 upstream (request id 4f2a)"))
    assert out["error"].startswith("The model endpoint didn't answer")
    assert "4f2a" in out["detail"] and "4f2a" not in out["error"]


def test_a_hosted_failure_keeps_its_raw_text_from_the_browser(monkeypatch):
    """ADR-0085 point 5: a provider's error can carry a key fragment."""
    from silica.ui.web.failures import explain
    monkeypatch.setenv("SILICA_HOSTED", "1")
    out = explain(RuntimeError("401 Unauthorized: key sk-or-v1-abc123 is invalid"))
    assert out["detail"] == "" and "sk-or" not in out["error"]


def test_a_transport_failure_never_draws_an_assistant_row():
    js = app_js()
    catch = js[js.index("  } catch (e) {\n    // The transport failed"):]
    catch = catch[:catch.index("} finally {")]
    assert "flowMsg(" not in catch, "the transport error is drawn as the agent's words again"
    assert "attachFailure(lastUserBody" in catch
    assert "ERROR_PLAIN" not in js, "the client guesses at raw text again"


def test_stop_is_disabled_until_the_cancel_is_acknowledged():
    js = app_js()
    body = _fn(js, "function requestStop()")
    assert "b.disabled = true" in body and "send.disabled = true" in body
    assert "STOP_GRACE_MS" in body and "still stopping" in body
    # one routine for the dock's strip and the chat's send button
    assert 'if (e.target.closest(".rl-stop")) requestStop();' in js
    assert 'if ($("#send").dataset.mode !== "stop") return;' in js


# --- P1. Run line at the composer ---------------------------------------------

def test_the_run_line_shows_the_running_tool_verb_and_target():
    body = _fn(WORK, "function paintRunLine(run)", "  ")
    assert "find((r) => r.running)" in body
    assert 'set(".rl-verb", verb)' in body and 'set(".rl-target", target)' in body
    # the chat's run line is the running turn's ledger (ADR-0117 point 4), built
    # after load, so work.js finds it at paint time
    assert '<span class="run-line" data-state="live">' in _fn(app_js(), "function makeLedger(msg, live)")
    assert "const runLines = runLinesNow();" in body


def test_no_elapsed_ticks_anywhere_after_done():
    assert "top-elapsed" not in INDEX and "top-run" not in INDEX
    assert "paintTopRun" not in WORK
    # the tick stops with the run, so nothing keeps counting after done
    assert "if (run && run.running) tick = setInterval(renderRunSide, 500)" in WORK


def test_stop_sits_within_the_composer_column():
    # In the chat, Stop is the send button for the length of a turn (ADR-0117
    # point 4); the dock keeps it in its run line, on the composer's measure.
    css = app_css()
    line = re.search(r"\.run-line \{([^}]*)\}", css).group(1)
    assert "max-width: var(--measure)" in line
    assert re.search(r'<div class="run-line" id="dock-run-line"[^>]*>(?:(?!</div>).)*class="rl-stop"', INDEX, re.S)
    assert 'id="run-line"' not in INDEX, "the chat grew its strip back"
    assert re.search(r'<button type="submit" id="send"[^>]*>.*?class="send-stop"', INDEX, re.S)
    assert 'id="stop"' not in INDEX, "a Stop still lives outside the run line"


def test_ninety_seconds_without_a_frame_names_the_stall():
    assert "const STALL_S = 90" in WORK
    body = _fn(WORK, "function paintRunLine(run)", "  ")
    assert "The endpoint is slow, not gone" in body
    assert 'line.dataset.state = stalled ? "stalled" : "live"' in body
    # a stall is a state of the line, never a failure of the run
    assert "failed" not in body


def test_stop_signals_the_cancel_token_and_the_done_frame_says_so(client):
    tc, server = client
    server.current_cancel = threading.Event()
    assert tc.post("/stop").status_code == 200
    assert server.current_cancel.is_set()


# --- P4. Review-requested write card; cards that survive reload ---------------

def test_a_note_with_review_in_frontmatter_produces_a_review_card_in_the_turn(client, tmp_vault):
    tc, server = client
    from silica.kernel.write import session_changes

    rel = "Etica.md"
    tmp_vault.note(rel, "---\ntype: Note\n---\n\nold\n")
    session_changes.touched(rel, "---\ntype: Note\n---\n\nold\n")
    tmp_vault.note(rel, "---\ntype: Note\nreview: could not find the new sentence, support 0.42\n---\n\nold\nnew\n")
    rows = tc.get("/changes").json()
    assert rows and rows[0]["review"].startswith("could not find")
    js = app_js()
    body = _fn(js, "function stampReviewCards()")
    assert 'card.classList.add("review")' in body and "review requested" in body
    assert 'className = "wc-keep"' in body and 'textContent = "keep"' in body


def test_keep_clears_review_through_the_gate_and_the_count_drops(client, tmp_vault):
    tc, server = client
    from silica.kernel.write import session_changes

    rel = "Etica.md"
    before = "---\ntype: Note\n---\n\nold\n"
    tmp_vault.note(rel, before)
    session_changes.touched(rel, before)
    tmp_vault.note(rel, "---\ntype: Note\nreview: unsure\n---\n\nold\nnew\n")
    assert tc.get("/changes").json()[0]["review"] == "unsure"
    r = tc.post("/changes/keep", json={"path": rel})
    assert r.status_code == 200
    assert tc.get("/changes").json()[0]["review"] == ""
    text = session_changes.current_text(rel)
    assert "review:" not in text and "\nnew\n" in text, "keep took more than the mark"


def test_revert_copy_names_what_it_takes_back_and_what_stays():
    js = app_js()
    body = _fn(js, "function stampReviewCards()")
    assert "revert takes back this one patch" in body
    # a note the turn created is deleted by its revert, and the copy says so
    assert "delete removes this note the turn created; /undo brings it back" in body
    assert "; the other write" in body and "stay" in body


def test_a_reopened_session_shows_the_same_writes_and_reads_as_the_live_one(client, monkeypatch):
    tc, server = client
    server.messages.append({"role": "user", "content": "patch it"})
    server.messages.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "silica_patch_note", "arguments": json.dumps({"name": "Etica", "heading": "H", "snippet": "s"})}},
        {"id": "c2", "type": "function", "function": {"name": "silica_read_note", "arguments": json.dumps({"name": "Morale"})}},
    ]})
    server.messages.append({"role": "tool", "tool_call_id": "c1", "content": "ok"})
    server.messages.append({"role": "tool", "tool_call_id": "c2", "content": "body"})
    server.messages.append({"role": "assistant", "content": "done"})
    data = tc.get("/messages").json()
    tools = [t for m in data for t in m["tools"]]
    assert {"name": "patch note", "effect": "written", "notes": ["Etica"]}.items() <= tools[0].items()
    assert tools[1]["effect"] == "read" and tools[1]["notes"] == ["Morale"]
    # the replay builds the footer with the one builder the live turn uses
    js = app_js()
    assert "function turnFooter(touched, failed)" in js
    replay = _fn(js, "async function loadVault()")
    assert "turnFooter(turn.touched, turn.failed)" in replay and "claimRefs(t, turn.touched, turn.failed)" in replay
    live = js[js.index("async function runTurn("):js.index("function send(text")]
    assert "turnFooter(touched, failed)" in live


# --- P7. URL is the state -----------------------------------------------------

def test_a_reload_reopens_the_same_session():
    js = app_js()
    assert 'h += "/" + encodeURIComponent(currentSessionId)' in js
    body = _fn(js, "function applyHashSession()")
    assert "openSession(arg, true)" in body
    assert "applyHashSession();" in _fn(js, "async function loadSessions()")


def test_back_returns_to_the_previous_session():
    js = app_js()
    opener = _fn(js, "async function openSession(id, fromUrl = false)")
    assert "syncUrl(!fromUrl)" in opener, "a click on a row pushes no history entry"
    assert opener.index("syncUrl(!fromUrl)") < opener.index('showTab("chat")'), \
        "showTab replaces the hash first, so the push finds nothing to push"
    assert "history.pushState" in _fn(js, "function syncUrl(push)")
    handler = _fn(js, "function tabFromHash()")
    assert "openSession(arg, true)" in handler


def test_middle_click_on_a_session_row_opens_a_tab():
    js = app_js()
    body = _fn(js, "async function loadSessions()")
    assert 'document.createElement("a")' in body and 'el.href = "#chat/"' in body
    handler = js[js.index('$("#sessions").addEventListener("click"'):]
    handler = handler[:handler.index("\n});\n")]
    assert "if (!plainClick(e)) return;" in handler, "a modifier click is intercepted"
    assert "e.preventDefault();" in handler


def test_a_stale_session_id_is_a_notice_over_a_working_page(client):
    tc, server = client
    assert tc.post("/session/load", json={"id": "deadbeef0000"}).status_code == 404
    js = app_js()
    body = _fn(js, "async function openSession(id, fromUrl = false)")
    assert "That chat wasn't found" in body
    assert "log.innerHTML" not in body.split("That chat wasn't found")[0]


# --- P16, after ADR-0122: identity and health at the drawer's head ----------

def _head() -> str:
    return _block(INDEX, '<div id="side-head">', "</div>")


def test_top_elapsed_is_gone_from_the_chrome():
    assert "top-elapsed" not in INDEX and "top-run" not in INDEX


def test_the_head_holds_no_number_that_cannot_change_during_a_session():
    head = re.sub(r"<!--.*?-->", "", _head(), flags=re.S)
    assert 'id="vault-chip"' in head
    for dead in ("top-counts", "top-elapsed", "vh-facts"):
        assert dead not in head, f"{dead} is back in the chrome"
    # the broken-link count lives in the panel behind the vault, not at rest
    assert "top-broken" not in head and "notices" not in head.replace("top-notices", "")
    # and nothing stands under the list: the foot left with the vault's move
    # up (ADR-0122), and the usage bars with it, to Settings
    assert 'id="side-foot"' not in INDEX


def test_new_chat_is_a_labelled_row_heading_the_views():
    # It was "+ new" on the Chats title; ADR-0084 made it the drawer's first
    # row, as "New" heads Claude's, and the rail carries it folded.
    sidebar = _block(INDEX, '<aside id="sidebar">', "</aside>")
    assert re.search(r'<button[^>]*id="new-chat"[^>]*>.*?<span>New chat</span></button>', sidebar)
    assert 'id="new-chat"' not in _block(INDEX, 'id="side-chats"', "</section>")
    assert sidebar.index('id="new-chat"') < sidebar.index('<nav id="views"')
    js = app_js()
    assert '$("#new-chat").addEventListener("click", newChat)' in js
    assert '$("#rail-new").addEventListener("click", newChat)' in js
    # the mark left the drawer with ADR-0122 and SILICA, the word alone, came
    # back as its head (amendment 2026-10-22); the landing keeps the mark,
    # beside its greeting (ADR-0048, amendment 2026-10-22)
    assert 'id="brand-logo"' not in INDEX and "#brand-logo" not in js
    assert '<span class="brand-word">SILICA' in _head()
    assert '<svg class="vh-mark"' in INDEX


def test_the_vault_chip_opens_notices_and_broken_links_in_one_panel():
    panel = _block(INDEX, 'id="vault-panel"', 'id="settings-btn"')
    assert 'id="boot-notices"' in panel and 'id="top-broken"' in panel
    # what the strip's right end held, behind the one vault (ADR-0084, A)
    whole = _block(INDEX, 'id="vault-panel"', 'id="help-panel"')
    assert 'id="settings-btn"' in whole and 'id="help-btn"' in whole and 'id="vault-switch"' in whole
    loose = _block(INDEX, "</main>", '<aside id="note-panel"')
    assert loose.count('role="dialog"') == 2, "help and the vault panel, and nothing else"
    # both feet open it: the drawer's, and the rail's while the drawer is folded
    js = app_js()
    assert 'vaultChip.addEventListener("click", toggleVaultPanel)' in js
    assert '$("#rail-vault").addEventListener("click", toggleVaultPanel)' in js


def test_settings_and_help_keep_a_gesture_each():
    # Each lost a click when it moved behind the vault (ADR-0084), so each has
    # a key; `?` never where it could be a character being typed.
    js = app_js()
    assert 'e.key === "," && (e.ctrlKey || e.metaKey)' in js
    assert 'e.target.closest("input, textarea, select, [contenteditable], .cm-editor")' in js


# --- P17 + P18 + P19, after ADR-0122. One list at a time ---------------------

def test_the_drawer_shows_one_of_two_lists_and_the_notes_first():
    """The tree and the chats split the column 3:2 under a third compartment,
    and neither had room (ADR-0122). A segment picks the list; the one shown
    takes the height left. A first visit shows the notes, the list every view
    reads (ADR-0072), and the choice is remembered."""
    rail = _block(INDEX, '<aside id="sidebar">', "</aside>")
    panes = re.findall(r'<section class="side-pane" id="(side-\w+)"', rail)
    assert panes == ["side-vault", "side-chats"], panes
    seg = _block(rail, '<div id="side-tabs"', "</div>")
    assert re.findall(r'data-pane="(side-\w+)"', seg) == panes
    assert re.search(r'<section class="side-pane" id="side-chats"[^>]*\bhidden\b', rail)
    # the trash is a group of the notes list, as pinned is (ADR-0128), not a compartment
    trash = _block(rail, '<details id="trash-group"', "</details>")
    assert "<details" not in rail.replace(trash, ""), "a compartment is back"
    js = app_js()
    assert 'let sidePane = localStorage.getItem("side-pane") === "side-chats" ? "side-chats" : "side-vault";' in js
    body = _fn(js, "function showSidePane(id)")
    assert 'localStorage.setItem("side-pane", id);' in body and "setActive(b, b.dataset.pane === id)" in body
    assert '"#side-tabs"]) glide($(id));' in js


def test_this_chat_left_and_its_marks_ride_the_chats():
    """"This chat" repeated the transcript: the write cards, the ledger line's
    steps and reads (ADR-0117). What it carried that nothing else did was two
    states readable while shut (audit P17): a write waiting for review and a
    turn running. They ride the Chats segment, the rail's Chats icon and, for
    the review, the current chat's row (ADR-0122 point 5)."""
    assert 'id="side-chat"' not in INDEX and 'id="chat-head"' not in INDEX
    assert 'id="work-body"' not in INDEX and 'id="changes"' not in INDEX
    js = app_js()
    changes = _fn(js, "async function loadChanges()")
    assert "chatsReview = rows.filter((r) => r.review).length;" in changes
    assert "syncChatsMark();" in changes and "#changes" not in changes
    mark = _fn(js, "function syncChatsMark()")
    assert 'row.classList.toggle("review", chatsReview > 0);' in mark
    assert 'seg.dataset.mark = mark(runLive && sidePane !== "side-chats");' in mark
    assert "rm.dataset.mark = mark(runLive);" in mark
    assert "markRun(run.running);" in WORK


def test_railmini_icons_match_the_lists_one_to_one():
    rail = _block(INDEX, '<aside id="railmini"', "</aside>")
    icons = re.findall(r'data-sec="(side-\w+)"', rail)
    sidebar = _block(INDEX, '<aside id="sidebar">', "</aside>")
    panes = re.findall(r'<section class="side-pane" id="(side-\w+)"', sidebar)
    assert icons == panes
    # the folded rail's states, said by the Chats icon's label and drawn as a bar
    js = app_js()
    assert 'rm.setAttribute("aria-label", "chats" + said);' in js
    assert re.search(r'const said = chatsReview \? ` .{1,6} \$\{chatsReview\} to review` : runLive \? " .{1,6} a turn running" : "";', js)
    assert '.rm[data-mark="review"]::after' in app_css()


def test_the_current_chat_is_marked_in_the_rail_and_in_the_document_title():
    js = app_js()
    body = _fn(js, "async function loadSessions()")
    assert 'el.setAttribute("aria-current", "page")' in body
    assert "setDocumentTitle();" in body
    assert re.search(r'document\.title = currentTitle \? currentTitle \+ " .{1,6} silica" : "silica"', js)
    assert re.search(r"\.tree-note\.open,\s*\.session\.active,[^{]*\{[^}]*background: var\(--sel\)", app_css())


def test_a_running_chat_shows_a_working_dot_in_the_list(client):
    tc, server = client
    body = _fn(app_js(), "async function loadSessions()")
    assert 'if (s.live) el.appendChild(mkEl("span", "s-dot", ""))' in body
    rows = tc.get("/sessions").json()
    assert all("live" in r for r in rows)


def test_sessions_are_bucketed_by_day_with_relative_time():
    js = app_js()
    assert "function dayBucket(ts)" in js and "function relTime(ts)" in js
    body = _fn(js, "async function loadSessions()")
    assert 'mkEl("div", "side-bucket", b)' in body
    assert "when.title = s.updated ? new Date(s.updated * 1000).toLocaleString()" in body


def test_delete_asks_first_and_names_the_consequence(client, tmp_path):
    tc, server = client
    js = app_js()
    body = _fn(js, "function openSessionMenu(row)")
    assert re.search(r'armThenRun\(del, "delete", "click again .{1,6} this chat and its record are gone"', body)
    # the route refuses an unknown id and a running chat
    assert tc.post("/session/delete", json={"id": "deadbeef0000"}).status_code == 404


def test_rename_and_delete_reach_the_narration_store(client, monkeypatch):
    tc, server = client
    from silica.agent import narration as _narr

    sid = _narr.NARRATOR.ensure_session(driver="gui")
    _narr.NARRATOR.turn({"role": "user", "content": "first question"})
    server.current_session_id = sid
    assert tc.get("/sessions").json()[0]["title"] == "first question"
    assert tc.post("/session/rename", json={"id": sid, "title": "renamed"}).status_code == 200
    assert tc.get("/sessions").json()[0]["title"] == "renamed"
    # the rename is one more beat, and the conversation is untouched
    assert _narr.messages_from_beats(list(_narr.read_beats(_narr.narration_dir() / f"{sid}.jsonl")))[0]["content"] == "first question"
    assert tc.post("/session/delete", json={"id": sid}).status_code == 200
    assert not (_narr.narration_dir() / f"{sid}.jsonl").exists()
    assert tc.get("/sessions").json() == []


def test_tree_rows_drop_the_extension_and_folders_carry_counts():
    from silica.ui.web.graph_view import render_tree

    html = render_tree([{"id": "a/b.md", "path": "a/b.md", "type": "note"},
                        {"id": "a/c.md", "path": "a/c.md", "type": "note"}], actions=True)
    assert '<span class="tree-n">2</span>' in html
    assert ">b</a>" in html and "b.md</a>" not in html


def test_the_open_note_is_marked_in_the_tree():
    js = app_js()
    body = _fn(js, "function markOpenNote()")
    assert 'el.classList.toggle("open"' in body
    assert "markOpenNote();" in _fn(js, "function showDrawer(title)")
    assert "markOpenNote();" in _fn(js, "function closeNote()")


def test_one_filter_heads_the_list_and_reads_both_lists():
    """The filter used to sit inside Vault with two scope chips (notes, chats)
    and reach UP into Chats. Since ADR-0122 the segment above it picks the
    list SHOWN, and the filter still narrows both: the matches of the list not
    in view are a button to it in the status line, or a chat named like the
    typed word would be a match nobody sees."""
    rail = _block(INDEX, '<aside id="sidebar">', "</aside>")
    assert 'id="side-scope"' not in rail and "data-scope" not in rail
    assert rail.index('id="side-tabs"') < rail.index('id="side-search"') < rail.index('id="side-vault"')
    js = app_js()
    assert "sideScope" not in js
    body = _fn(js, "function applySidebarFilter()")
    assert "search inside the notes" in body and "match" in body
    assert 'go.addEventListener("click", () => showSidePane(pane));' in body
    assert "if (pane === sidePane)" in body
    # one string narrows notes, pinned AND chats: no branch zeroes it
    assert 'sideScope === "notes" ? raw : ""' not in body
    assert 'querySelectorAll(".session")' in body and 'querySelectorAll(".tree-note")' in body


# --- P20. One composer ---------------------------------------------------------

def test_one_composer_component_serves_chat_and_dock():
    dock = _block(INDEX, '<div id="dock"', "</div>\n\n  <!-- dictation")
    assert 'class="run-line" id="dock-run-line"' in dock
    js = app_js()
    body = js[js.index('$("#dock-composer").addEventListener("submit"'):]
    body = body[:body.index("\n});\n")]
    assert "if (streaming) { queueMessage(t); return; }" in body
    assert 'document.querySelectorAll(".run-line")' in WORK


# --- P9 + P13. Focus contract; disabled with a reason -------------------------

def test_every_dialog_uses_the_one_focus_utility():
    js = app_js()
    dialogs = re.findall(r'id="([\w-]+)"[^>]*role="dialog"', INDEX)
    assert len(dialogs) >= 6, dialogs
    listed = re.search(r'const DIALOGS = \[([^\]]*)\]', js).group(1)
    for d in dialogs:
        if d == "ctx-panel" or d == "st-sheet":
            continue
        assert f'"#{d}"' in listed, f"{d} is outside the one trap"
    assert '"#ctx-panel"' in listed and '"#st-sheet"' in listed
    assert js.count("function trapFocus(") == 1


def test_the_drawers_verbs_live_behind_the_title():
    """ADR-0059: at rest the header holds the title, the pin and close. The
    verbs are the title's menu; the pin stays out of it, since pinned is a
    state and a state has to show at rest. ADR-0075 cut the menu to chat and
    the map (the diff when there is one) and put the two views' glyphs beside
    the title, outside the menu for the pin's reason: the dot is a state.
    ADR-0119 adds two items, a test on the note and its teach-back, because
    Learn's home no longer lists every note; the old /explain ask stays out."""
    head = _block(INDEX, '<aside id="note-panel"', '<div id="note-diff"')
    assert ('<button type="button" id="note-title" aria-haspopup="menu" aria-expanded="false" '
            'aria-controls="note-menu">') in head
    menu = _block(head, '<div id="note-menu" role="menu"', "</header>")
    assert re.findall(r'role="menuitem" id="([\w-]+)"', menu) == ["note-chat", "note-test", "note-teach", "note-map", "note-diff-open"]
    for gone in ("note-summarize", "note-explain", "note-quiz", "note-relate", "note-learn"):
        assert gone not in INDEX, gone
    for glyph in ("note-pin", "note-info", "note-read"):
        assert f'id="{glyph}"' in head and f'id="{glyph}"' not in menu, glyph
    assert head.index('id="note-title"') < head.index('id="note-info"') < head.index('id="note-pin"')
    js = app_js()
    assert "toggleEdit" not in js and 'e.key.toLowerCase() !== "e"' not in js  # Ctrl+E is the browser's
    # the caret comes from a click in the text, never from opening a note
    assert ".focus()" not in _fn(js, "async function openNote(path, line, facts) {")
    esc = js[js.index("One Escape handler for the whole app"):]
    esc = esc[:esc.index("\n});")]
    assert esc.index("closeNoteMenu(true)") < esc.index("hideSelBar()") < esc.index("closeNote();")
    # details go back to the text before the drawer closes (ADR-0075)
    assert esc.index("hideSelBar()") < esc.index("setDetails(false)") < esc.index("closeNote();")


def test_the_closed_drawer_is_inert():
    assert re.search(r'<aside id="note-panel"[^>]*\binert\b', INDEX)
    js = app_js()
    assert "notePanel.inert = true;" in _fn(js, "function closeNote()")
    assert "notePanel.inert = false;" in _fn(js, "function showDrawer(title)")


def test_no_control_carries_information_only_in_a_title():
    """A disabled control's reason, and a card action's hint, ride data-tip:
    the shared tooltip shows them on hover AND on focus."""
    js = app_js()
    assert 'tip.setAttribute("role", "tooltip")' in js
    assert 'document.addEventListener("focusin"' in js
    # the drawer's pin is a glyph alone, so its state rides the tip
    pin = _fn(js, "function syncPinButton()")
    assert "btn.dataset.tip =" in pin and "btn.title" not in pin
    card = _fn(js, "function writeCard(ref, effect, verb, refused)")
    assert "b.dataset.tip" in card and "b.title" not in card


def test_no_write_card_removes_its_action_without_a_reason():
    js = app_js()
    body = _fn(js, "function writeCard(ref, effect, verb, refused)")
    failed = body[body.index("if (!copy.act) {"):]
    failed = failed[:failed.index("return card;")]
    assert "b.disabled = true" in failed and "nothing landed, nothing to revert" in failed


# --- P11, P12, P15 -------------------------------------------------------------

def test_a_late_response_from_the_previous_vault_never_paints():
    js = app_js()
    assert "function ticket(name)" in js and "let vaultEpoch = 0" in js
    for fn in ("async function loadVaultInfo()", "async function loadSessions()", "async function loadChanges()"):
        body = _fn(js, fn)
        assert "const still = ticket(" in body and "if (!still()) return;" in body, fn
    assert "vaultEpoch++" in js, "a vault switch bumps no epoch"


def test_a_failed_tool_row_keeps_its_reason_on_replay(client):
    tc, server = client
    server.messages.append({"role": "user", "content": "read it"})
    server.messages.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "silica_read_note", "arguments": json.dumps({"name": "Ghost"})}},
    ]})
    server.messages.append({"role": "tool", "tool_call_id": "c1", "content": json.dumps({"error": "'Ghost' not found in vault"})})
    server.messages.append({"role": "assistant", "content": "sorry"})
    tools = [t for m in tc.get("/messages").json() for t in m["tools"]]
    assert tools[0]["error"] is True
    assert tools[0]["reason"] == "That note isn't in the vault under that name."
    replay = _fn(app_js(), "async function loadVault()")
    assert 't.error && t.reason' in replay


def test_the_reminders_poll_is_gated_on_visibility():
    js = app_js()
    body = _fn(js, "async function pollReminders()")
    assert 'document.visibilityState !== "visible"' in body and "heldReminders.push" in body
    assert "setInterval(pollReminders, 30000)" in js


# ---------------------------------------------------------------------------
# Surfaces, not compartments (2026-09-22 experiment): three rounded panes on the
# canvas, separated by gutters and by nothing drawn; the answer flows on the
# view's own surface; one segment recipe; sentence case on the chrome.

def test_the_three_panes_are_surfaces_on_a_gutter_canvas():
    css = app_css()
    assert "--gutter: 12px;" in css
    main = css[css.index("main {\n  flex: 1;"):]
    main = main[:main.index("}")]
    assert "gap: var(--gutter);" in main and "padding: var(--gutter);" in main
    view = css[css.index(".view {\n  flex: 1;"):]
    view = view[:view.index("}")]
    assert "background: var(--sheet);" in view and "border-radius: var(--r);" in view
    rail = css[css.index("#sidebar {\n  position: relative;"):]
    rail = rail[:rail.index("}")]
    assert "background: var(--slate);" in rail and "border-radius: var(--r);" in rail
    assert "border-right: 1px solid var(--line-2);" not in rail
    note = css[css.index("#note-panel {"):]
    note = note[:note.index("}")]
    assert "top: var(--gutter);" in note and "right: var(--gutter);" in note
    assert "border-left: 1px solid var(--line-2);" not in note
    assert "transform: translateX(calc(100% + var(--gutter)));" in note
    # every inset that reserved the note's width reserves its gutter too
    assert "margin-right: calc(var(--note-w, 630px) + var(--gutter));" in css
    assert "right: calc(var(--note-w, 630px) + 2 * var(--gutter));" in css


def test_the_answer_flows_on_the_surface_and_the_question_heads_it():
    """No card on either side since ADR-0117: the answer flows on the surface
    and the question is a heading over a rule on the text column, starting
    where the answer's text starts."""
    css = app_css()
    answer = css[css.index(".msg.silica>.body {"):]
    answer = answer[:answer.index("}")]
    assert "background:" not in answer, "the answer grew a card inside the sheet again"
    user = css[css.index(".msg.user>.body {"):]
    user = user[:user.index("}")]
    assert "background:" not in user, "the question grew its bubble back"
    assert "border-top: 1px solid var(--line);" in user
    assert "max-width: var(--text-w, 43rem);" in user and "margin-inline: 0;" in user


def test_the_view_showing_is_marked_as_the_current_place():
    """Since ADR-0084 the views are rows of the drawer, and the one showing
    wears the --sel box the open note and the current chat wear, with its glyph
    in the accent. The segments keep the gliding pill, inside a view and, since
    ADR-0122, the drawer's Notes and Chats, which is why the views are out of
    the glide list and the lists are in it."""
    css = app_css()
    p = css[css.index("#views .tab.active {"):]
    p = p[:p.index("}")]
    assert "background: var(--sel);" in p
    assert "text-shadow" not in p
    assert 'for (const id of ["#surface-tabs", "#cal-mode", "#metrics-tabs", "#side-tabs"]) glide($(id));' in app_js()


def test_the_chrome_reads_in_sentence_case_on_the_type_floor():
    css = app_css()
    assert "--fs-meta: 12px;" in css and "--fs-ctl: 13px;" in css
    for sel in ("#side-tabs button {", ".side-bucket {", ':is(#note-menu, #attach-menu, #vault-add-menu) [role="menuitem"] {', ".sources-label {",
                ".thinking summary {", "details.fm summary {", ".sp-title {"):
        # a selector may be ruled more than once across the cut sheets; none of
        # its blocks may set caps, and the one that sizes it sizes it on the scale
        blocks = [b[:b.index("}")] for b in css.split(sel)[1:]]
        assert blocks, sel
        assert not any("text-transform: uppercase" in b for b in blocks), sel
        assert any("var(--fs-" in b for b in blocks), sel
    ctl = css[css.index("#dock-send {\n  font-family: var(--sans);"):]
    ctl = ctl[:ctl.index("}")]
    assert "font-size: var(--fs-ctl);" in ctl and "uppercase" not in ctl


def test_a_note_is_read_before_its_properties(client, tmp_vault):
    tc, _server = client
    tmp_vault.note("Foo.md", "---\ntags: [x]\n---\nfirst line of the note")
    html = tc.get("/note", params={"path": "Foo.md"}).json()["html"]
    assert html.index("first line of the note") < html.index('<details class="fm"')
    assert '<details class="fm" open' not in html


def test_selected_is_a_frosted_box_and_never_a_bar():
    """One selection grammar (2026-09-22): the --sel fill, heavier text, and no
    inset accent bar, active pill or glow anywhere it says "this one"."""
    css = app_css()
    frame = (WEB / "graph-frame.css").read_text(encoding="utf-8")
    assert "--sel:" in css and "--sel-hover:" in css and "--sel:" in frame
    assert "inset 2px 0 0 var(--accent)" not in css
    assert "box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 14%" not in css
    for sel in (".session.active:focus-within {", ".lr-sg.on {", ".lr-pl.on {", ".stab.active {",
                ".node-result.sel {", "#cal-mode button.active {"):
        blk = css[css.index(sel):]
        blk = blk[:blk.index("}")]
        assert "var(--sel)" in blk, sel
    assert ".result-item.sel{background:var(--sel)" in frame
    assert ".legend-item.active{background:var(--sel)" in frame
    assert "#gstat .seg button.active{background:var(--sel)" in frame


def test_the_note_is_a_reading_sheet_with_one_inset():
    css = app_css()
    for sel in ("#note-edit .cm-content {", "#note-diff {"):
        blk = css[css.index(sel):]
        blk = blk[:blk.index("}")]
        assert "1.25rem" in blk, sel
    head = css[css.index("#note-panel>header {"):]
    head = head[:head.index("}")]
    assert "border-bottom" not in head
    na = css[css.index(".na {"):]
    na = na[:na.index("}")]
    assert "border: none;" in na
    callout = css[css.index(":is(.msg, #peek-body, #note-edit) .callout {"):]
    callout = callout[:callout.index("}")]
    assert "border-left" not in callout
    js = (WEB / "app-notes.js").read_text(encoding="utf-8")
    assert 'localStorage.getItem("note-props")' in js and 'fm.addEventListener("toggle"' in js
