# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""The graph's colours: one hue per community and zone, the edge layers and the
node defaults, on the dark floor and on paper.

Out of graph_export because five modules that draw the vault (the vault shape,
the mind map, the daily layer, the library's file layer, the viewer) read the
colours and nothing else of it, by private name, and the daily layer's import
made it and graph_export a cycle. They ride in the /graph payload, so a change
here is a change of its bytes.
"""
from __future__ import annotations

import colorsys


# Cluster colors: one distinct, vivid hue per community — the color encodes
# Louvain membership (real structure), so it must be unique per community and
# stable for a given id. Hues are confined to the arc of the mascot's body,
# its blue pole (212 deg) through to its warm magenta one (306 deg); the
# previous golden-angle walk covered the whole wheel and so emitted green,
# chartreuse and orange, hues that appear nowhere in the mark. The arc stops
# short of cyan deliberately: the mascot's body contains no cyan, only its eye
# does, and here cyan is already spoken for by EDGE_COLOR_SIMILAR — a k-NN
# mesh that outnumbers the wikilinks two to one and swallowed any node sharing
# its hue. Ending at 212 rather than 186 puts the nearest community 0.087 away
# from that mesh in OKLab instead of 0.042.
#
# Within the arc a golden-ratio sequence keeps consecutive ids far apart and
# lightness alternates between two bands, which buys back most of what the
# shorter arc costs: minimum pairwise dE across 12 communities is 0.023 against
# 0.029 for the full wheel. Neither is a real number to lean on. The arc is not
# what limits color-as-identity, count is — past roughly ten communities both
# sets fall below the perceptual floor, so read the labels, not the hues.
_COMMUNITY_ARC = (212.0, 306.0)
_COMMUNITY_LIGHTNESS = (0.70, 0.54)
# The same two-band alternation, reflected for a paper floor. Only the bands
# move: the arc, the golden-ratio walk and the saturation are what make two
# communities tell apart, and none of them cares which way the floor goes.
# Chosen the same way the dark pair was, by measuring against the floor they
# land on — 4.93:1 to 11.11:1 across the arc on the light --void, against
# 3.07:1 to 8.59:1 for the dark pair on the dark one.
_COMMUNITY_LIGHTNESS_ON_PAPER = (0.42, 0.30)
_GOLDEN_RATIO_INV = 0.6180339887


def community_color(i: int, on_paper: bool = False) -> str:
    lo, hi = _COMMUNITY_ARC
    hue = lo + ((i * _GOLDEN_RATIO_INV) % 1.0) * (hi - lo)
    bands = _COMMUNITY_LIGHTNESS_ON_PAPER if on_paper else _COMMUNITY_LIGHTNESS
    light = bands[i % 2]
    r, g, b = colorsys.hls_to_rgb(hue / 360.0, light, 0.66)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


# Zones walk the same arc as communities but never the same KEY: the phase shift puts zone i half an
# arc away from community i, so the two partitions cannot be read off one
# swatch (ADR-0023: no shared colour key). Lightness bands are shared, because a
# zone hue also lands on a node dot when the zone layer is on and the dot has to
# stay legible on --void.
_ZONE_PHASE = 0.5


def zone_color(i: int, on_paper: bool = False) -> str:
    lo, hi = _COMMUNITY_ARC
    hue = lo + ((i * _GOLDEN_RATIO_INV + _ZONE_PHASE) % 1.0) * (hi - lo)
    bands = _COMMUNITY_LIGHTNESS_ON_PAPER if on_paper else _COMMUNITY_LIGHTNESS
    light = bands[i % 2]
    r, g, b = colorsys.hls_to_rgb(hue / 360.0, light, 0.66)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))

# Edge colours. Only ONE layer is vivid, the inferred one; the written links are
# the unlit neutral they have always been. They carry arrows, width and the most
# opacity, so they never needed a hue to rank first, and two saturated meshes
# stacked on one frame is how both stop being legible.
#
# The neutrals were unlit blue-violet, keyed to the mascot. The substrate is
# graphite now (docs/specs/visual-identity.md), so they are 214deg at 10%, each
# holding its predecessor's relative luminance to four decimals. That invariant
# is the one this block already committed to, and it is why the mesh ranking
# below survives the recolour untouched.
#
# Ambiguous stays red on purpose — an alarm that matches the brand stops reading
# as an alarm — but off the neon and into the same lightness band as its
# neighbours.
EDGE_COLOR_EXTRACTED = "#868f9c"   # unlit graphite — resolved links
# A written `requires:` (ADR-0145 point 7): the theme's frost, the "on" ink of
# what the person wrote and accepted, a neutral so k-NN stays the one vivid layer.
EDGE_COLOR_REQUIRES = "#F4F5F7"
EDGE_COLOR_REQUIRES_PAPER = "#1A1815"
EDGE_COLOR_AMBIGUOUS = "#e2544f"   # alarm red, deliberately outside the arc
EDGE_COLOR_SIMILAR   = "#22b4cc"   # the signal — embedding k-NN (semantic map)

# The paper set. Every one of these is drawn ON the floor and read AGAINST it,
# so a light floor needs its own values or the whole mesh disappears — an edge
# is one or two pixels wide and has no second channel to fall back on. The
# ranking between the three layers is what is preserved, not the values: the
# written links stay the quiet neutral, ambiguous stays the alarm outside the
# arc, and similar stays the one vivid layer. Each is the same hue walked down
# into the range paper can hold, which for a hairline means roughly half the
# lightness of its crystal twin.
EDGE_COLOR_EXTRACTED_PAPER = "#7c7261"
EDGE_COLOR_AMBIGUOUS_PAPER = "#b3211b"
EDGE_COLOR_SIMILAR_PAPER   = "#0a6070"

NODE_DEFAULT_COLOR = {"background": "#545d67", "border": "#868f9c",
                      "background_paper": "#79705f",
                      "highlight": {"background": "#868f9c", "border": "#F4F5F7"}}
# Ghost is the one node that does not simply invert. On crystal it is the
# darkest thing on screen — "unlit, never black" — and the paper equivalent of
# unlit is not a dark dot but a pale one: 468 near-black dots on a white field
# would read as the loudest layer in the view, which is the opposite of what a
# ghost is. So it goes the other way, to the faintest thing the floor can hold.
NODE_GHOST_COLOR = {"background": "#141619", "border": "#545d67",
                    "background_paper": "#cbc6bd",
                    "highlight": {"background": "#22262a", "border": "#868f9c"}}


def similar_edge(eid: str, a: str, b: str, score: float) -> dict:
    """One SIMILAR edge: the k-NN layer's look, and its width from the score."""
    return {
        "id":    eid,
        "from":  a,
        "to":    b,
        "type":  "SIMILAR",
        # 0.35, where this layer has always drawn. It dropped to 0.26
        # while the wikilinks were vivid too and the two meshes were
        # fighting; with the written links back to gray there is nothing
        # to stay out of the way of, and at 0.26 the k-NN reads as haze
        # rather than as lines. The rank still holds — a wikilink is
        # 0.72 and nearly three times the width.
        "color": {"color": EDGE_COLOR_SIMILAR,
                  "paper": EDGE_COLOR_SIMILAR_PAPER, "opacity": 0.35},
        # Width still carries the similarity score, but inside a band
        # that ends BELOW a wikilink's 1.6. It used to run to 3.0, so
        # the strongest inferred edges drew wider than every written one
        # — the rank exactly inverted — which went unnoticed while the
        # viewer ignored width entirely.
        "width": round(0.5 + 0.6 * score, 2),
        "score": round(score, 4),
    }
