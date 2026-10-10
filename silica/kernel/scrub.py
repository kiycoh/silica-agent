# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Credential redaction for output boundaries.

Endpoints reach the user through details silica composes itself and through
exception text it never wrote (httpx carries the full request URL, query
included). A proxy that takes `?api-key=` or a hosted endpoint with the key in
the query therefore puts that key into the doctor report, the log, and the JSON
payload the agent can quote back into a note.

One function, called at the boundary that renders, never per call site: the
point is to cover the strings nobody anticipated, which is exactly where an
opt-in redaction fails.
"""
from __future__ import annotations

import re

_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]{0,19}://)[^/\s@]+@")
_QUERY_SECRET = re.compile(
    r"([?&#](?:access[_-]?token|auth[_-]?token|token|bearer|api[_-]?key|key"
    r"|password|secret|signature|sig|session(?:id)?|cookie|credential)=)[^&#\s]*",
    re.IGNORECASE,
)
# A key handed to a server as a CLI flag (`llama-server --api-key sk-…`):
# SILICA_*_SERVE_CMD lines reach the log next to a URL that IS scrubbed, and a
# redaction that stops one token short reads as complete. The leading dashes
# keep prose like "the key = value pair" out of it.
_FLAG_SECRET = re.compile(
    r"(--?[A-Za-z0-9_-]*(?:api[_-]?key|token|password|secret)[= ])\S+",
    re.IGNORECASE,
)


def scrub_credentials(text: object) -> str:
    """Redact URL userinfo, sensitive query values and key-bearing CLI flags.
    Call at output boundaries.

    ponytail: regex over a fixed parameter-name list, so a provider that names
    its key something unusual passes through. The alternative is an allowlist of
    what may be printed at all, which is a much larger change; widen the list if
    a real endpoint escapes it.
    """
    return _FLAG_SECRET.sub(
        r"\1***", _QUERY_SECRET.sub(r"\1***", _USERINFO.sub(r"\1***@", str(text)))
    )
