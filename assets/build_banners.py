#!/usr/bin/env python3
"""Re-inline assets/sili_max.svg into both README banners.

An SVG loaded as an image (which is what the README does, through GitHub's camo
proxy) runs in secure static mode and cannot fetch external resources, so
<image href="sili_max.svg"/> would render nothing. The mascot has to live
inside each banner as a copy. Run this after editing the mascot:

    python3 assets/build_banners.py
"""

import re
from pathlib import Path

HERE = Path(__file__).parent
MASCOT = "sili_max.svg"
# the mascot carries its own colour now, so neither banner repaints it: the
# old ink swap would flatten every facet into one silhouette
BANNERS = ("banner.svg", "banner-light.svg")
# where the mascot sits on the 1200x300 canvas. Keep this in step with the
# banners: a mismatch here silently reverts their geometry the next time this
# runs.
BOX = 'x="102" y="30" width="189" height="240"'
# Centring the 519x650 art in the slot puts its *bounding box* in the middle,
# which reads as off-centre: the creature's mass sits high and right of centre
# (alpha-weighted centroid 269.1,307.8 against a box centre of 259.5,325) and
# the sparse particles below it carry no visual weight. This viewBox is the
# smallest one centred on that centroid that still contains every pixel, so the
# slot centre falls on the mass instead of on the outline.
VIEWBOX = "0 -34.4 538.2 684.4"


def mascot_block() -> str:
    src = (HERE / MASCOT).read_text()
    outer = re.search(r'<svg[^>]*?\bwidth="(\d+)"[^>]*?\bheight="(\d+)"[^>]*>', src)
    if not outer:
        raise SystemExit(f"{MASCOT}: no width/height on the root <svg>")
    if (outer.group(1), outer.group(2)) != ("519", "650"):
        raise SystemExit(f"{MASCOT}: VIEWBOX was measured on a 519x650 mascot")

    body = src[outer.end():].rsplit("</svg>", 1)[0].strip()
    # the tracer emits 8-decimal coordinates; at ~192px wide one is sub-pixel
    body = re.sub(r"-?\d+\.\d+", lambda m: ("%.1f" % float(m.group())).replace(".0", ""), body)

    paths = body.count("<path")
    return (
        "    <!-- Sili, the mascot: the single hero, anchored to the left inset.\n"
        f"         {paths} vector outlines, inlined from {MASCOT} by\n"
        "         build_banners.py. Edit that file, not this block. -->\n"
        f'    <svg {BOX} viewBox="{VIEWBOX}">\n'
        + "\n".join("      " + line for line in body.split("\n"))
        + "\n    </svg>"
    )


def main() -> None:
    block = mascot_block()
    for name in BANNERS:
        path = HERE / name
        before = path.read_text()
        after, n = re.subn(
            r"    <!-- Sili, the mascot.*?</svg>",
            lambda _: block, before, count=1, flags=re.S)
        if n != 1:
            raise SystemExit(f"{name}: mascot block not found")
        path.write_text(after)
        verb = "unchanged" if after == before else "updated"
        print(f"{name}: {verb} ({len(after) // 1024} KB)")


if __name__ == "__main__":
    main()
