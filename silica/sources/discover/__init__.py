# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""Discover (ADR-0137): the person's feeds read into stories.

`feeds` reads one feed, `read` reads them all and groups articles into
stories, `story` writes a story when it is opened and saves it to the Inbox,
`rank` orders For you by the vault, `store` keeps it all outside the vault.
`catalog.json` is Kagi News's feed list (MIT, LICENSE-kite-feeds beside it),
rebuilt by scripts/discover_catalog.py.
"""
