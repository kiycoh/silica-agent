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

from silica.config import hosted

# (pattern, sentence). First match wins; order matters only where two could
# both match (a 401 inside a "connection" message is a credentials problem).
_CLASSES: list[tuple[re.Pattern[str], str]] = [
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


def sentence(raw: str) -> str:
    """The authored sentence for a raw failure text."""
    if _AUTHORED.search(raw):
        return raw
    for pat, text in _CLASSES:
        if pat.search(raw):
            return text
    return FALLBACK



def explain(exc: BaseException | str) -> dict[str, str]:
    """`{"error": <sentence>, "detail": <raw>}` for a response body or a
    stream frame. `error` is what the person reads; `detail` is what the
    person debugging reads, and it is never the same field.

    Hosted, `detail` is empty (ADR-0085 point 5): a provider's error text can
    carry a fragment of the platform's key, and the person reading it there is
    a student, not the operator, who has the log."""
    raw = str(exc)
    return {"error": sentence(raw), "detail": "" if hosted() else raw}
