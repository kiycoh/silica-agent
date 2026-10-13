"""Every failure the web UI states is authored here, once.

A raw exception used to reach the person three ways: as the agent's own words
in the transcript, as a `TypeError` line, or as an amber toast, depending on
which path it took (audit 2026-09-20, P3). The client kept a regex table to
paper over that after the fact. The table lives here instead, on the side
that raises, so a route cannot serialise `str(exc)` into a user-facing field:
`explain()` returns the sentence as `error` and the raw text as `detail`, and
the client draws the first and discloses the second.

One class per sentence. A new failure class gets a sentence here, never a
branch in a client.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from silica.config import hosted

# (pattern, sentence). First match wins; order matters only where two could
# both match (a 401 inside a "connection" message is a credentials problem).
_CLASSES: list[tuple[re.Pattern[str], str]] = [
    # First: the refusal's raw text is a JSON body that carries note paths,
    # and a note named "Connection pooling" would otherwise read as an
    # endpoint failure (ADR-0092).
    (re.compile(r"not deleted: \d+ notes? links? to"),
     "Not deleted: other notes link to this note. Deleting it is yours to do, from its card or with /delete."),
    (re.compile(r"\b(401|403|unauthorized|forbidden|api[_ -]?key)\b", re.I),
     "The provider rejected the credentials. Check the API key for this endpoint in settings."),
    (re.compile(r"\b(429|rate limit)\b", re.I),
     "The provider is rate-limiting. Wait a moment and try again."),
    (re.compile(r"\b(50[0-9]|timeout|timed out|connection|ECONNREFUSED|unreachable)\b", re.I),
     "The model endpoint didn't answer. Try again, or check the endpoint in settings."),
    (re.compile(r"lint failed", re.I),
     "The write was rolled back: the note would have broken a vault rule. The vault is unchanged."),
    (re.compile(r"not found in vault|no such note", re.I),
     "That note isn't in the vault under that name."),
    (re.compile(r"context length|too many tokens|max_tokens", re.I),
     "The conversation outgrew the model's context. Start a new chat, or narrow the question."),
]

FALLBACK = "Something went wrong on this turn. The detail says what."

# Text that is already a sentence someone wrote for the person: a command's
# usage line, or a route's own "not available" verdict. Rewriting those into
# the fallback would replace the one message that says what to type.
# "not run: declined" is a connector call the user refused (ADR-0091): an
# outcome they chose, which the fallback would misread as a fault.
_AUTHORED = re.compile(r"\bUsage:|not available in this session|has nothing to search for|^not run: ")


# A usage limit reached (ADR-0102 point 6): one sentence per layer, each naming
# when it resets. `{when}` is filled here in UTC, and the frame also carries the
# instant and the exact words filled in, so the browser can put the student's
# own clock in their place: the server's zone is the host's, never the reader's.
_LIMIT_SENTENCES = {
    "window": "You have used this 5-hour window.",
    "week": "You have used this week's allowance.",
    "month": "You have used this month's allowance.",
}
# UsageLimited's own text (silica.agent.usage_limits), which is all a tool's
# failure carries by the time a sub-agent's refusal reaches the transcript.
_LIMIT_TEXT = re.compile(r"usage limit reached: (window|week|month), resets (\S+)")
_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")
# A 403 from OpenRouter as text alone: transcription reaches it outside
# call_llm and reports "transcription server returned 403", and a model call's
# error re-raised as text keeps litellm's "OpenrouterException". A website's 403
# under /fetch is neither, and must not read as a spent allowance. A bad key is
# a 401 and never matches: it keeps the credentials sentence (point 6).
_OPENROUTER_403 = re.compile(r"(transcription|openrouter)[^\n]*\b403\b", re.I)


def _when(layer: str, resets_at: str | None) -> str:
    """The reset as the sentence says it, in UTC: "at 14:20 UTC" for the
    window, which is never more than 5 hours away; "on Monday at 00:00 UTC" for
    the week; "on 1 November at 00:00 UTC" for the month. English names by
    hand, because strftime's follow the process locale."""
    try:
        at = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return ""
    clock = f"{at:%H:%M} UTC"
    if layer == "window":
        return f"at {clock}"
    if layer == "week":
        return f"on {_DAYS[at.weekday()]} at {clock}"
    return f"on {at.day} {_MONTHS[at.month - 1]} at {clock}"


def limit_sentence(layer: str, resets_at: str | None) -> tuple[str, str]:
    """(sentence, the reset's words inside it) for a limit reached."""
    when = _when(layer, resets_at) if resets_at else ""
    head = _LIMIT_SENTENCES.get(layer, _LIMIT_SENTENCES["month"])
    return (f"{head} It resets {when}." if when else head), when


def sentence(raw: str) -> str:
    """The authored sentence for a raw failure text."""
    if _AUTHORED.search(raw):
        return raw
    if m := _LIMIT_TEXT.search(raw):
        return limit_sentence(m[1], m[2] if m[2] != "never" else None)[0]
    for pat, text in _CLASSES:
        if pat.search(raw):
            return text
    return FALLBACK


def _limit_of(exc: BaseException | str, raw: str) -> tuple[str, str | None] | None:
    """(layer, resets_at) when `exc` is a usage limit: refused before the call,
    or, hosted, a 403 OpenRouter returned while `budgets` shows a layer spent."""
    layer = getattr(exc, "layer", None)
    if isinstance(layer, str) and layer in _LIMIT_SENTENCES:
        return layer, getattr(exc, "resets_at", None)
    if m := _LIMIT_TEXT.search(raw):
        return m[1], (m[2] if m[2] != "never" else None)
    status = getattr(exc, "status_code", None)
    forbidden = status == 403 or (status is None and bool(_OPENROUTER_403.search(raw)))
    # Hosted only: a laptop's 403 is a key its owner can fix in settings, and
    # the read below would cost a request on every failure of a local model.
    # The read also covers transcription (ADR-0102 point 8), which reaches
    # OpenRouter outside call_llm and meets the limit as a 403.
    if not (hosted() and forbidden):
        return None
    from silica.agent.usage_limits import spent_layer

    hit = spent_layer()
    return (hit.layer, hit.resets_at) if hit is not None else None


def explain(exc: BaseException | str) -> dict[str, str]:
    """`{"error": <sentence>, "detail": <raw>}` for a response body or a
    stream frame. `error` is what the person reads; `detail` is what the
    person debugging reads, and it is never the same field.

    Hosted, `detail` is empty (ADR-0085 point 5): a provider's error text can
    carry a fragment of the platform's key, and the person reading it there is
    a student, not the operator, who has the log.

    A usage limit adds `limit` (window, week or month), `resets_at` (ISO 8601
    UTC) and `when`, the words of `error` that name the reset in UTC, which the
    browser swaps for the student's own time (ADR-0102 point 6). Hosted, a 403
    reads the key once to learn which layer is spent: this can block for up to
    usage_limits.TIMEOUT_S, so an async caller runs it off the loop."""
    raw = str(exc)
    out = {"error": sentence(raw), "detail": "" if hosted() else raw}
    if (lim := _limit_of(exc, raw)) is not None:
        text, when = limit_sentence(*lim)
        out.update(error=text, limit=lim[0], resets_at=lim[1] or "", when=when)
    return out
