"""Every failure the web UI states has its one shape here.

A raw exception used to reach the person three ways: as the agent's own words
in the transcript, as a `TypeError` line, or as an amber toast, depending on
which path it took (audit 2026-09-20, P3). The client kept a regex table to
paper over that after the fact. The table lives here instead, on the side
that raises, so a route cannot serialise `str(exc)` into a user-facing field:
`explain()` returns the sentence as `error` and the raw text as `detail`, and
the client draws the first and discloses the second.

One class per sentence. A new failure class gets a sentence here, never a
branch in a client.

A route's own "no" is the other half. Its words were written for the person, so
`explain()`'s table must not rewrite them: `refusal()` gives them the same shape
with `error` the words as they are, and `failureSaid` in app-core.js is the one
reading of either. Two bodies stay out of it, and the reader takes them too: a
guard's 403, pinned word for word since it is read by no page, and a status with
nothing to say of its own, which is how a withheld route passes for a missing one.
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
    (re.compile(r"reached its output limit"),
     "The model ran out of room before it finished, so nothing was kept."),
]

FALLBACK = "Something went wrong on this turn. The detail says what."

# Text that is already a sentence someone wrote for the person: a command's
# usage line, or a route's own "not available" verdict. Rewriting those into
# the fallback would replace the one message that says what to type.
# "not run: declined" is a connector call the user refused (ADR-0091): an
# outcome they chose, which the fallback would misread as a fault.
_AUTHORED = re.compile(r"\bUsage:|not available in this session|has nothing to search for|^not run: "
                       r"|^the server restarted while|is no longer in this step: rewrite")


# A usage limit reached (ADR-0102 point 6): one sentence per layer, each naming
# when it resets. `{when}` is filled here in UTC, and the frame also carries the
# instant and these words, so the browser can write the sentence again with the
# student's own clock: the server's zone is the host's, never the reader's.
LIMIT_PHRASE = {"window": "this 5-hour window", "week": "this week's allowance", "month": "this month's allowance"}
LIMIT_SENTENCE = {layer: f"You have used {phrase}." for layer, phrase in LIMIT_PHRASE.items()}
# The reset's sentence after it, `{when}` filled here in UTC and on the page in
# the reader's own clock.
LIMIT_RESETS = "It resets {when}."
# UsageLimited's own text (silica.model.usage_limits), which is all a tool's
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
    head = LIMIT_SENTENCE.get(layer, LIMIT_SENTENCE["month"])
    return (f"{head} {LIMIT_RESETS.format(when=when)}" if when else head), when


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
    if isinstance(layer, str) and layer in LIMIT_SENTENCE:
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
    from silica.model.usage_limits import spent_layer

    hit = spent_layer()
    return (hit.layer, hit.resets_at) if hit is not None else None


class Refused(Exception):
    """A route's "no" in words written for the person: the text of what it caught,
    named as such by raising this, where `HTTPException(400, str(exc))` would
    serialise whatever an exception happens to say. `code` is for a page that must
    act on which "no" this is: it branches on that, never on the sentence."""

    def __init__(self, status: int, said: BaseException | str, code: str = ""):
        super().__init__(str(said))
        self.status, self.code = status, code


def refusal(said: str, code: str = "") -> dict[str, str]:
    """The body of a refusal, `explain()`'s shape. `detail` repeats the words
    because it held them before `error` did, and it is what a reader outside this
    repo was given to read."""
    return {"error": said, "detail": said, **({"code": code} if code else {})}


def said(words: str) -> dict[str, str]:
    """`explain()`'s shape for a command's own "no": its words were written for the
    person, so no table rewrites them, and hosted its `detail` stays empty as every
    stream frame's does."""
    return {"error": words, "detail": "" if hosted() else words}


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
