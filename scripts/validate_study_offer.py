#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Whether the study offer's rule fires before real misses, on a real ledger.

The rule under a hard chat answer (silica/kernel/learn/offer.py) follows the
research report difficulty-detection-and-study-aids, whose evidence comes from
other people's students. This checks it on this vault's own: at every graded
answer, would the rule have fired on what came before, and was the answer then
wrong? A rule worth its line fires before misses more often than it stays
silent before them. Then how often a shown offer was acted on.

    uv run python scripts/validate_study_offer.py [VAULT]

VAULT defaults to SILICA_VAULT. Reopen the rule's thresholds at 200 graded
answers after a firing or 2027-01-31, whichever comes first (MISSES, RECENT
and the confidence bands are the report's proposals, not measured here).
"""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    if len(sys.argv) > 1:
        os.environ["SILICA_VAULT"] = sys.argv[1]
    from silica.kernel.learn import offer
    from silica.kernel.report import quiz

    entries = quiz.entries()
    result = offer.replay(entries)
    result["graded_answers"] = len(entries)
    print(json.dumps(result, indent=1))
    fired = result["fired"]["wrong"] + result["fired"]["right"]
    if fired < 30:
        print(f"\n{fired} answers came after a firing: too few to read a rate (want 30 or more).")
    elif (result["miss_rate_when_fired"] or 0) <= (result["miss_rate_when_silent"] or 0):
        print("\nThe rule fires no more before misses than it stays silent before them: revisit it.")
    else:
        print("\nThe rule fires before misses more often than it stays silent before them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
