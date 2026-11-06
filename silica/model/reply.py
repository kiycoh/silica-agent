# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""A model's JSON reply, as models write it: fenced, wrapped in prose, closed
twice or cut mid-array. Stdlib only, so `kernel/text` builds the distiller's
protocol on it and a caller that only parses pays for no provider stack.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


class TruncatedArray(ValueError):
    """A JSON array was cut mid-stream, but its leading objects were recovered.

    Raised by decode_json instead of the bare JSONDecodeError when the payload
    looks like an op array whose tail is missing (max_tokens truncation). The
    complete leading objects are in `.ops`, the unrecoverable remainder in
    `.tail`. Subclassing ValueError keeps every existing `except Exception`
    call-site failing exactly as before — partial data is only ever used by
    callers that catch TruncatedArray by name.
    """

    def __init__(self, ops: list, tail: str):
        super().__init__(
            f"truncated JSON array: recovered {len(ops)} complete leading "
            f"objects, {len(tail)} chars unrecoverable"
        )
        self.ops = ops
        self.tail = tail


def _salvage_array(text: str) -> tuple[list, str] | None:
    """Recover the complete leading `{...}` objects of a truncated JSON array.

    String-aware scan (a `}` inside a string value never closes an object).
    Collection stops at the first object that fails to parse on its own — the
    recovered prefix is always a clean prefix of the intended array. Returns
    (objects, unconsumed_tail), or None when nothing was recoverable.
    """
    start = text.find('[')
    if start == -1:
        return None
    ops: list = []
    in_string = escape = False
    depth = 0
    obj_start = -1
    end_of_last = start
    for i in range(start + 1, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in '{[':
            if depth == 0 and ch == '{':
                obj_start = i
            depth += 1
        elif ch in '}]':
            if depth == 0:
                break  # the array closed by itself; anything further is tail
            depth -= 1
            if depth == 0 and obj_start != -1:
                try:
                    ops.append(json.loads(text[obj_start:i + 1]))
                except json.JSONDecodeError:
                    return (ops, text[obj_start:]) if ops else None
                end_of_last = i
                obj_start = -1
    tail = text[end_of_last + 1:]
    return (ops, tail) if ops else None


def decode_json(raw: str, strict: bool = False) -> tuple[Any, bool]:
    """(value, was_clean): the JSON `raw` holds, and whether it held nothing else.
    `strict` raises on a reply that needed anything stripped."""
    cleaned = raw.strip()
    if cleaned.startswith('\ufeff'):
        cleaned = cleaned[1:]
    
    fence_pattern = re.compile(r'^```(?:json)?\s*\n(.*?)\n```$', re.DOTALL | re.IGNORECASE)
    inner_fence_pattern = re.compile(r'```(?:json)?\s*\n(.*?)\n```', re.DOTALL | re.IGNORECASE)
    
    was_strict_clean = True
    processed = cleaned
    
    m = fence_pattern.match(cleaned)
    if m:
        processed = m.group(1).strip()
        was_strict_clean = False
    else:
        m = inner_fence_pattern.search(cleaned)
        if m:
            processed = m.group(1).strip()
            was_strict_clean = False
            
    parsed = None
    parse_err = None
    try:
        parsed = json.loads(processed)
    except json.JSONDecodeError as e:
        start_idx = -1
        for idx, ch in enumerate(raw):
            if ch in '{[':
                start_idx = idx
                break
        end_idx = -1
        for idx in range(len(raw) - 1, -1, -1):
            if raw[idx] in '}]':
                end_idx = idx
                break
        
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            candidate = raw[start_idx:end_idx+1]
            try:
                parsed = json.loads(candidate)
                was_strict_clean = False
            except json.JSONDecodeError as inner_e:
                parse_err = inner_e
        else:
            parse_err = e

    if parsed is None:
        # One whole value, then nothing but closers: a model that closed its
        # object twice ('..."}\n}', Qwen3.5-9B on a page reading, 2026-10-13)
        # wrote everything asked for, and "Extra data" dropped all of it. Only
        # closers may follow: a second value after the first is still an error.
        try:
            value, end = json.JSONDecoder().raw_decode(processed)
        except json.JSONDecodeError:
            pass
        else:
            if not processed[end:].strip(" \t\r\n}]"):
                parsed, parse_err, was_strict_clean = value, None, False

    if parsed is None:
        salvage = _salvage_array(processed)
        if salvage is not None:
            raise TruncatedArray(*salvage)
        if parse_err is not None:
            raise parse_err
        raise ValueError("JSON Parse Error")

    if strict and not was_strict_clean:
        raise ValueError("Strict mode violation: markdown fences, preambles, or postambles were stripped from the output.")

    return parsed, was_strict_clean


def parse_reply(raw: str | None, default: Any = None) -> Any:
    """The JSON value of a reply, or `default` when the reply is not JSON. What
    a failed parse means belongs to the caller, so `default` says it; a cut
    array counts as not JSON here, and only the distiller salvages its head."""
    try:
        return decode_json(raw or "")[0]
    except (ValueError, RecursionError) as e:
        logger.debug("model reply is not JSON: %s", e)
        return default
