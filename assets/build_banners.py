#!/usr/bin/env python3
"""Emit the two README banners, the favicon both front ends use, and the web
GUI's one remaining raster.

    python3 assets/build_banners.py

An SVG loaded as an image (which is what the README does, through GitHub's camo
proxy) runs in secure static mode and cannot fetch external resources, so
<image href="silica-mark.svg"/> would render nothing. The mark has to live
inside each banner as a copy, which is why the banners are generated and not
hand-edited: the perimeter is a computed crystal outline, and keeping two
copies of it in sync by hand is how they drift apart.

The plate is a rectangle whose four corners are cut rather than radiused, and
cut twice, at the angles a silicon carbide crystal takes off its own edge. Over
it runs a hexagonal net, the lattice the mark is built on. The outline is
point-symmetric about the centre, so the four corners cannot drift apart.

The favicon is one file copied to the two places that serve one, rather than
three hand-kept copies of the same drawing. It is the mark's small-size cut,
not the mark: the full one carries enough nested rings to turn to mush at 16px.

The chat empty state still comes from the mascot PNG, because Sili survives
there and not as the hero. That needs Pillow, which is not a silica dependency,
because this script is run by hand on the one machine that edits the art.
"""

import math
import re
from pathlib import Path

HERE = Path(__file__).parent
MARK = "silica-mark.svg"
MASCOT_PNG = "sili_mascot.png"
STATIC = HERE.parent / "silica" / "ui" / "web" / "static"
# (file, width) - the chat empty state, and nothing else now
RASTERS = (("sili.webp", 360),)
# one drawing per destination that needs it, copied rather than kept by hand:
# the small cut for both tabs, the full mark for the site's social card
FAVICON = "silica-mark-favicon.svg"
WEB = HERE.parent / "web"
ART_COPIES = ((FAVICON, STATIC / "favicon.svg"), (FAVICON, WEB / "favicon.svg"),
              (MARK, WEB / "silica-mark.svg"))

W, H = 1200, 300
CY = H / 2

# Half the perimeter, clockwise from the top of the left edge: the other half is
# this rotated 180 degrees about the centre, which is what keeps the four
# corners identical. A rectangle, but the corners are cut and not radiused, and
# cut twice: 62 then 37 degrees, the angles a crystal takes off its own edge.
UPPER = ((0, 58), (18, 26), (50, 0), (1150, 0), (1182, 26), (1200, 58))

# x, y, side of the slot the mark sits in, then where the type column starts.
# The block is centred on measured extents and not on its boxes: the mark's
# hexagon stops 11px inside its own slot, and the type column measures 557px
# wide in Inter (read off getBBox in a browser, since nothing here has font
# metrics). That puts ink on 168px of margin at both ends. Re-measure when the
# copy changes: a shorter line moves the balance, it does not keep it.
MARK_BOX = (157, 33, 234)
TEXT_X = 471

CORNER_R = 3.0  # crystal edges are sharp, but a hairline stroke needs the join

# Affirmative, and deliberately not a promise to end hallucination: the README
# argues twenty lines below that hallucination has a statistical floor, with the
# paper to prove it. Grounding is what the product actually delivers, so that is
# what the first line claims.
#
# The second line is the write gate, because the banner is the one surface that
# travels alone. It is GitHub's social preview and it is whatever embeds the
# README, and neither of those carries the paragraph that explains the gate.
# Grounding is a claim every retrieval tool makes; surviving its own writes is
# not, so the line that has no context to lean on is the one that has to say it.
# Both lines measure 550px at this size, so they end flush and TEXT_X still
# centres the block. Anything longer moves the balance and has to be re-measured.
COPY = ("Any model, grounded in the documents you keep.",
        "Links you had not seen, writes that never break it.")

# The mark's own gradient, laid across the plate so the edge is lit by the same
# light as the logo inside it.
EDGE_STOPS = (
    (0.00, "#1637c9"), (0.15, "#4fb4ff"), (0.32, "#2364f2"), (0.50, "#4a45e8"),
    (0.66, "#8b3ff5"), (0.83, "#bb7dff"), (1.00, "#7a24c4"),
)

# Lato Light @ 100px, tracking 0.16em, extracted with fontTools SVGPathPen so it
# never falls back to Arial on someone else's machine. Light and not Black: the
# mark is hairline art, and a heavy wordmark reads as a different brand sharing
# the canvas.
WORDMARK = "M44.8 -62.2Q44.4 -61.3 43.5 -61.3Q42.9 -61.3 41.8 -62.2Q40.8 -63.2 39.1 -64.3Q37.3 -65.4 34.7 -66.3Q32.1 -67.3 28.2 -67.3Q24.4 -67.3 21.4 -66.2Q18.5 -65.1 16.5 -63.2Q14.6 -61.3 13.5 -58.8Q12.5 -56.3 12.5 -53.6Q12.5 -50 14 -47.6Q15.6 -45.2 18.1 -43.6Q20.6 -42 23.7 -40.8Q26.9 -39.7 30.2 -38.6Q33.6 -37.5 36.8 -36.2Q40 -34.9 42.5 -32.9Q45 -30.9 46.5 -27.9Q48 -25 48 -20.7Q48 -16.2 46.5 -12.3Q45 -8.3 42.1 -5.5Q39.2 -2.6 35 -0.9Q30.8 0.8 25.4 0.8Q18.4 0.8 13.3 -1.7Q8.2 -4.2 4.5 -8.5L5.9 -10.7Q6.5 -11.4 7.2 -11.4Q7.7 -11.4 8.4 -10.8Q9.1 -10.2 10.1 -9.3Q11.1 -8.5 12.5 -7.4Q13.9 -6.4 15.8 -5.5Q17.6 -4.7 20 -4.1Q22.4 -3.5 25.5 -3.5Q29.7 -3.5 33 -4.7Q36.2 -6 38.5 -8.2Q40.8 -10.4 42 -13.4Q43.2 -16.4 43.2 -19.9Q43.2 -23.7 41.7 -26.1Q40.2 -28.5 37.7 -30.1Q35.1 -31.8 32 -32.9Q28.8 -34 25.5 -35Q22.1 -36.1 18.9 -37.4Q15.8 -38.7 13.2 -40.7Q10.8 -42.7 9.2 -45.7Q7.7 -48.8 7.7 -53.3Q7.7 -56.9 9.1 -60.2Q10.4 -63.5 13 -66Q15.6 -68.5 19.4 -70Q23.2 -71.5 28.2 -71.5Q33.6 -71.5 38 -69.8Q42.4 -68 46 -64.5Z M85.1 0H80V-70.8H85.1Z M160.7 -4.4V0H122V-70.8H127.2V-4.4Z M193.7 0H188.6V-70.8H193.7Z M281.7 -11.9Q282.2 -11.9 282.6 -11.6L284.6 -9.4Q282.4 -7.1 279.8 -5.2Q277.2 -3.3 274.1 -2Q271.1 -0.7 267.4 0.1Q263.7 0.8 259.3 0.8Q251.9 0.8 245.8 -1.8Q239.7 -4.4 235.3 -9.1Q230.9 -13.8 228.5 -20.5Q226 -27.2 226 -35.4Q226 -43.5 228.5 -50.1Q231 -56.8 235.5 -61.5Q240 -66.3 246.3 -68.9Q252.6 -71.5 260.2 -71.5Q267.5 -71.5 273.1 -69.3Q278.7 -67 283.3 -63L281.8 -60.7Q281.4 -60.1 280.5 -60.1Q279.9 -60.1 278.5 -61.2Q277.2 -62.3 274.8 -63.6Q272.4 -65 268.8 -66.1Q265.2 -67.2 260.2 -67.2Q253.8 -67.2 248.5 -65Q243.2 -62.8 239.4 -58.7Q235.5 -54.6 233.4 -48.7Q231.2 -42.8 231.2 -35.4Q231.2 -27.9 233.4 -22Q235.6 -16.1 239.4 -12Q243.2 -8 248.4 -5.8Q253.5 -3.6 259.6 -3.6Q263.4 -3.6 266.3 -4.1Q269.3 -4.6 271.8 -5.6Q274.3 -6.6 276.5 -8.1Q278.6 -9.5 280.7 -11.5Q280.9 -11.7 281.2 -11.8Q281.4 -11.9 281.7 -11.9Z M352.4 -25.7 338 -61.5Q337.2 -63.2 336.6 -65.7Q336.2 -64.5 335.9 -63.4Q335.6 -62.3 335.2 -61.4L320.8 -25.7ZM368.1 0H364.1Q363.4 0 363 -0.4Q362.5 -0.8 362.2 -1.4L354 -21.9H319.2L310.9 -1.4Q310.7 -0.8 310.2 -0.4Q309.7 0 309 0H305.1L334.1 -70.8H339.1Z"

DARK = dict(
    name="banner.svg",
    base=("#0f1524", "#0b0c1a", "#1d0f20"),
    spot=(0.16, 0.082, 0.042, 0.012),
    lattice="#8ea2ff", lattice_opacity="0.09",
    edge_opacity="0.85", inner_opacity=("0.30", "0.14"),
    bevel="#ffffff", bevel_opacity="0.055",
    wordmark="#edf0f8", body="#a8adc9",
)
LIGHT = dict(
    name="banner-light.svg",
    base=("#e2e5f2", "#eff0f8", "#e7e0ee"),
    spot=(0.17, 0.088, 0.034, 0.009),
    lattice="#3a3f8f", lattice_opacity="0.085",
    edge_opacity="0.9", inner_opacity=("0.26", "0.12"),
    bevel="#ffffff", bevel_opacity="0.5",
    wordmark="#15182e", body="#4e5473",
)


def outline() -> list[tuple[float, float]]:
    """The crystal perimeter, clockwise from the left point."""
    return [*UPPER, *((W - x, H - y) for x, y in UPPER)]


def inset(poly: list[tuple[float, float]], d: float) -> list[tuple[float, float]]:
    """Miter offset inwards by d. Every edge stays parallel to its original."""
    def shifted(a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(dx, dy)
        # clockwise winding with y pointing down: the interior is to the right
        nx, ny = -dy / n * d, dx / n * d
        return (a[0] + nx, a[1] + ny), (dx / n, dy / n)

    out = []
    for i, b in enumerate(poly):
        (p, u) = shifted(poly[i - 1], b)
        (q, v) = shifted(b, poly[(i + 1) % len(poly)])
        cross = u[0] * v[1] - u[1] * v[0]
        if abs(cross) < 1e-9:  # collinear: the offset point is on both lines
            out.append(q)
            continue
        t = ((q[0] - p[0]) * v[1] - (q[1] - p[1]) * v[0]) / cross
        out.append((p[0] + u[0] * t, p[1] + u[1] * t))
    return out


def draw(poly: list[tuple[float, float]]) -> str:
    """Path data, every corner taken off by a hair."""
    def lerp(a, b, r):
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(dx, dy)
        r = min(r, n * 0.4)
        return a[0] + dx / n * r, a[1] + dy / n * r

    parts = []
    for i, v in enumerate(poly):
        prev, nxt = poly[i - 1], poly[(i + 1) % len(poly)]
        a, b = lerp(v, prev, CORNER_R), lerp(v, nxt, CORNER_R)
        parts.append(f"{'M' if i == 0 else 'L'}{a[0]:.1f} {a[1]:.1f}"
                     f"Q{v[0]:.1f} {v[1]:.1f} {b[0]:.1f} {b[1]:.1f}")
    return "".join(parts) + "Z"


def bevel(poly: list[tuple[float, float]], depth: float) -> str:
    """A band hugging the top edge, so the grown faces catch the light."""
    top = [p for p in poly if p[1] <= CY]
    inner = [p for p in inset(poly, depth) if p[1] <= CY]
    pts = top + inner[::-1]
    return "M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in pts) + "Z"


def lattice(r: float = 23.0) -> str:
    """A silicon carbide net: flat-top hexagons, the same habit as the plate."""
    edges = set()
    col = 0
    cx = -r
    while cx < W + r:
        cy = -r if col % 2 == 0 else -r + math.sqrt(3) * r / 2
        while cy < H + r:
            corners = [(cx + r * math.cos(math.radians(60 * k)),
                        cy + r * math.sin(math.radians(60 * k))) for k in range(6)]
            for j, a in enumerate(corners):
                b = corners[(j + 1) % 6]
                key = tuple(sorted(((round(a[0], 1), round(a[1], 1)),
                                    (round(b[0], 1), round(b[1], 1)))))
                edges.add(key)  # shared walls would otherwise stroke twice
            cy += math.sqrt(3) * r
        cx += 1.5 * r
        col += 1
    return "".join(f"M{a[0]:.1f} {a[1]:.1f}L{b[0]:.1f} {b[1]:.1f}"
                   for a, b in sorted(edges))


def mark_block() -> str:
    """The mark, inlined. Its ids are prefixed: they land in the banner's
    document, where a bare id="c" would collide with the next thing named c."""
    src = (HERE / MARK).read_text()
    body = src.split(">", 1)[1].rsplit("</svg>", 1)[0].strip()
    for i in re.findall(r'id="([^"]+)"', body):
        body = body.replace(f'id="{i}"', f'id="mark-{i}"')
        body = body.replace(f"url(#{i})", f"url(#mark-{i})")
    return body


def banner(t: dict) -> str:
    poly = outline()
    s0, s1, s2 = t["base"]
    o0, o1, o2, o3 = t["spot"]
    stops = "\n      ".join(
        f'<stop offset="{o}" stop-color="{c}"/>' for o, c in EDGE_STOPS)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="Silica. {COPY[0]} {COPY[1]}">
  <title>Silica</title>

  <defs>
    <!-- the mark read at a tenth of its value: indigo under its blue facets,
         plum under its violet ones. Kept near the ends of the range, so the
         mark stays the only lit thing and the ground never competes. -->
    <linearGradient id="base" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{s0}"/>
      <stop offset="0.52" stop-color="{s1}"/>
      <stop offset="1" stop-color="{s2}"/>
    </linearGradient>
    <!-- one soft field, behind the mark only: the light marks the subject
         instead of wallpapering the canvas -->
    <radialGradient id="spot" cx="0.5" cy="0.5" r="0.5">
      <stop offset="0" stop-color="#5b4bd6" stop-opacity="{o0}"/>
      <stop offset="0.35" stop-color="#5b4bd6" stop-opacity="{o1}"/>
      <stop offset="0.62" stop-color="#5b4bd6" stop-opacity="{o2}"/>
      <stop offset="0.84" stop-color="#5b4bd6" stop-opacity="{o3}"/>
      <stop offset="1" stop-color="#5b4bd6" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="edge" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="{W}" y2="{H}">
      {stops}
    </linearGradient>
    <linearGradient id="lit" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="46">
      <stop offset="0" stop-color="{t['bevel']}" stop-opacity="{t['bevel_opacity']}"/>
      <stop offset="1" stop-color="{t['bevel']}" stop-opacity="0"/>
    </linearGradient>
    <!-- the lattice runs the whole plate, but weakest where the type is and
         strongest in the margin past it -->
    <linearGradient id="fade" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="{W}" y2="0">
      <stop offset="0" stop-color="#fff" stop-opacity="0.3"/>
      <stop offset="0.5" stop-color="#fff" stop-opacity="0.6"/>
      <stop offset="1" stop-color="#fff" stop-opacity="1"/>
    </linearGradient>
    <mask id="net">
      <rect width="{W}" height="{H}" fill="url(#fade)"/>
    </mask>
    <clipPath id="plate">
      <path d="{draw(poly)}"/>
    </clipPath>
  </defs>

  <g clip-path="url(#plate)">
    <rect width="{W}" height="{H}" fill="url(#base)"/>
    <ellipse cx="{MARK_BOX[0] + MARK_BOX[2] // 2}" cy="150" rx="330" ry="240" fill="url(#spot)"/>
    <path d="{lattice()}" mask="url(#net)" fill="none" stroke="{t['lattice']}"
          stroke-opacity="{t['lattice_opacity']}" stroke-width="1"/>
    <!-- the top faces, lit from above -->
    <path d="{bevel(poly, 30)}" fill="url(#lit)"/>
    <!-- the plate is the mark's hexagon, drawn twice more on the way in, the
         way the mark nests its own -->
    <path d="{draw(inset(poly, 11))}" fill="none" stroke="url(#edge)"
          stroke-opacity="{t['inner_opacity'][0]}" stroke-width="1"/>
    <path d="{draw(inset(poly, 22))}" fill="none" stroke="url(#edge)"
          stroke-opacity="{t['inner_opacity'][1]}" stroke-width="1"/>

    <!-- the mark: the single hero. Inlined from
         {MARK} by build_banners.py. Edit that file, not this block. -->
    <svg x="{MARK_BOX[0]}" y="{MARK_BOX[1]}" width="{MARK_BOX[2]}" height="{MARK_BOX[2]}" viewBox="0 0 512 512">
      {mark_block()}
    </svg>

    <g transform="translate({TEXT_X} 132) scale(0.88)" fill="{t['wordmark']}">
      <path d="{WORDMARK}"/>
    </g>

    <!-- what Silica is, then the three properties that make it that. Sized so
         the longest line keeps ~12% of the column in reserve, since this is
         live text and a machine without Inter falls back wider. -->
    <text font-family="'Inter','Segoe UI',Helvetica,Arial,sans-serif"
          font-size="25" font-weight="400" fill="{t['body']}">
      <tspan x="{TEXT_X + 4}" y="190">{COPY[0]}</tspan>
      <tspan x="{TEXT_X + 4}" y="224">{COPY[1]}</tspan>
    </text>
  </g>

  <path d="{draw(poly)}" fill="none" stroke="url(#edge)"
        stroke-opacity="{t['edge_opacity']}" stroke-width="1.6"
        stroke-linejoin="round"/>
</svg>
"""


def build_rasters() -> None:
    """Export the GUI's WebPs from the mascot PNG, alpha kept."""
    from PIL import Image

    src = Image.open(HERE / MASCOT_PNG).convert("RGBA")
    for name, width in RASTERS:
        out = STATIC / name
        height = round(src.height * width / src.width)
        src.resize((width, height), Image.LANCZOS).save(out, "WEBP", quality=90, method=6)
        print(f"{name}: {width}x{height} ({out.stat().st_size // 1024} KB)")


def check() -> None:
    """The two failures that are silent: an inset that runs the wrong way round
    the winding, and a termination that has eaten into the mark. Both come out
    as art that still renders, so nothing else would catch them."""
    poly = outline()
    d = 22
    box = inset(poly, d)
    assert (round(min(x for x, _ in box), 6), round(min(y for _, y in box), 6),
            round(max(x for x, _ in box), 6), round(max(y for _, y in box), 6)
            ) == (d, d, W - d, H - d), "inset went outwards"

    def inside(px, py):  # even-odd crossing count
        hits = 0
        for (ax, ay), (bx, by) in zip(poly, poly[1:] + poly[:1]):
            if (ay > py) != (by > py) and px < ax + (py - ay) / (by - ay) * (bx - ax):
                hits += 1
        return hits % 2 == 1

    x, y, size = MARK_BOX
    corners = [(x, y), (x + size, y), (x, y + size), (x + size, y + size)]
    assert all(inside(*c) for c in corners), "the plate now clips the mark"


def header_mark() -> str:
    """The favicon cut down again, for the site's nav at 20px.

    Two things go, both mechanically: the gradient, because the page's own
    direction contract allows exactly one gradient on it and that one is the
    spine, and the masked ring moire, because at 20px it renders as a halo
    around the nodes instead of as rings. What is left inherits currentColor
    like the mark it replaced. Derived, not redrawn, so it cannot drift."""
    src = (HERE / FAVICON).read_text()
    body = src.split(">", 1)[1].rsplit("</svg>", 1)[0]
    body = re.sub(r'<g mask="url\(#h\)".*?</g>', "", body, flags=re.S)
    body = re.sub(r"<defs>.*?</defs>", "", body, flags=re.S)  # gradient and mask, now unused
    body = body.replace("url(#c)", "currentColor").replace("#4fb4ff", "currentColor")
    if "url(#" in body:
        raise SystemExit(f"{FAVICON}: a reference outlived its def")
    # and it is inked heavier: at 20px the favicon's weights read lighter than
    # the 600-weight wordmark beside it, which makes the pair look unfinished
    body = re.sub(r'stroke-width="([\d.]+)"',
                  lambda m: f'stroke-width="{float(m.group(1)) * 1.6:.0f}"', body)
    body = re.sub(r'r="([\d.]+)"',
                  lambda m: f'r="{float(m.group(1)) * 1.25:.0f}"', body)
    return ('        <!-- the mark, monochrome, generated from '
            f'assets/{FAVICON} by assets/build_banners.py -->\n'
            '        <svg class="brand-mark" viewBox="0 0 512 512" width="20" height="20"'
            ' aria-hidden="true" focusable="false">'
            + body + "</svg>")


def patch_site_mark() -> None:
    page = HERE.parent / "web" / "index.html"
    before = page.read_text()
    after, n = re.subn(r"        <!-- the mark, monochrome.*?</svg>",
                       lambda _: header_mark(), before, count=1, flags=re.S)
    if n != 1:
        raise SystemExit("web/index.html: brand mark block not found")
    page.write_text(after)
    print(f"web/index.html: {'unchanged' if after == before else 'updated'} (brand mark)")


def copy_art() -> None:
    for name, out in ART_COPIES:
        art = (HERE / name).read_bytes()
        verb = "unchanged" if out.exists() and out.read_bytes() == art else "updated"
        out.write_bytes(art)
        print(f"{out.relative_to(HERE.parent)}: {verb} ({len(art) // 1024} KB)")


def main() -> None:
    check()
    copy_art()
    patch_site_mark()
    build_rasters()
    for theme in (DARK, LIGHT):
        path = HERE / theme["name"]
        before = path.read_text() if path.exists() else ""
        after = banner(theme)
        path.write_text(after)
        verb = "unchanged" if after == before else "updated"
        print(f"{theme['name']}: {verb} ({len(after) // 1024} KB)")


if __name__ == "__main__":
    main()
