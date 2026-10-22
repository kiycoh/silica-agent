#!/usr/bin/env python3
"""Emit the mark, the two README banners, the favicon both front ends use, the
inline copy of the mark in the site's nav, and the web GUI's one remaining
raster. The GUI draws the mark in 2D from its four pieces (RUN_MARK in
app-chat.js), which this script does not write.

    python3 assets/build_banners.py

The mark is the seed, approved 2026-10-21: one square in the corner and three
shells grown from it, the way a vault grows one verified write on top of the
last. It is computed here from a handful of numbers rather than drawn in a
file, so the master, the banners and the inline copies cannot drift apart.

An SVG loaded as an image (which is what the README does, through GitHub's camo
proxy) runs in secure static mode and cannot fetch external resources, so
<image href="silica-mark.svg"/> would render nothing. The mark has to live
inside each banner as a copy, which is why the banners are generated and not
hand-edited.

The banner is the lockup and nothing else: no plate, no ground, no strapline,
on transparent, so it sits on whatever page embeds it instead of carrying its
own rectangle everywhere it travels.

The favicon is the mark itself, one file copied to the places that serve it.
The old mark needed a small-size cut because its nested rings turned to mush at
16px; this one is drawn on a 16-unit grid, so every edge already lands on a
whole pixel at 16, 32, 48 and 64.

The chat empty state still comes from the mascot PNG, because Sili survives
there and not as the hero. That needs Pillow, which is not a silica dependency,
because this script is run by hand on the one machine that edits the art.
"""

import re
from pathlib import Path

HERE = Path(__file__).parent
MARK = "silica-mark.svg"
MASCOT_PNG = "sili_mascot.png"
STATIC = HERE.parent / "silica" / "ui" / "web" / "static"
WEB = HERE.parent / "web"
# (file, width) - the chat empty state, and nothing else now
RASTERS = (("sili.webp", 360),)
# the two tabs and the site's social card, all served the one master
ART_COPIES = (STATIC / "favicon.svg", WEB / "favicon.svg", WEB / "silica-mark.svg")

# The identity's two inks and its one hue at two stops: no single chromatic
# value clears AA on both a near-white and a near-black ground. The seed carries
# the signal, the shells and the words carry the ink. See
# docs/specs/visual-identity.md.
PAPER = dict(ink="#1A1815", sig="#0A6070")
GRAPHITE = dict(ink="#F4F5F7", sig="#22B4CC")

# One unit, 16 in the 256 box: seed 5u, gap 1u, shell 2u, three shells and 1u
# of padding, so the mark is 14u square and each edge is a whole pixel at 16px.
U = 16
SEED, GAP, SHELL, SHELLS = 5 * U, U, 2 * U, 3
PAD = U
GROUND = PAD + SEED + SHELLS * (GAP + SHELL)  # the line the seed, shells and words stand on
# Light on dark spreads, and the 1u gaps close first, so the graphite banner is
# eroded by one unit of the 256 box on every edge. The files that serve both
# grounds (the favicon and the two inline copies) keep one geometry: at 64px and
# under the correction is a quarter of a pixel or less.
THIN = 1

# Host Grotesk 1.003 (SIL OFL 1.1), the wordmark's face wherever the word is
# written (ADR-0122, amendment 2026-10-22), so the drawer's SILICA, this lockup
# and the site's nav are one face. Outlined at 100 units with fontTools, no
# kerning, so the word never falls back on someone else's machine: "silica" at
# weight 560 and "agent" at 340, both tracked -0.01em, pen at 0 on the baseline.
# Ink right edges, the l's ascender and the 560 cut's space measured off the font.
WORD = "M25 1.2Q15.5 1.2 9.2 -3.3Q3 -7.8 2.1 -15.7H14Q14.7 -12.1 17.8 -10Q20.9 -7.9 25.4 -7.9Q30.1 -7.9 32.5 -9.7Q35 -11.5 35 -14.2Q35 -16.3 33.3 -17.5Q31.7 -18.7 27.4 -19.3Q25.7 -19.6 23.4 -20Q21.1 -20.4 19.4 -20.7Q15.6 -21.4 12.2 -23.1Q8.8 -24.9 6.7 -27.9Q4.6 -30.8 4.6 -35.3Q4.6 -41.9 9.6 -46.3Q14.5 -50.8 23.6 -50.8Q32.7 -50.8 38.2 -46.8Q43.7 -42.7 44.4 -35H33.1Q32.8 -38.4 30.1 -40.1Q27.4 -41.7 23.6 -41.7Q19.6 -41.7 17.3 -40.1Q15 -38.5 15 -36Q15 -31.8 22.1 -30.5Q23.1 -30.4 24.7 -30.1Q26.2 -29.9 27.7 -29.6Q29.3 -29.4 30.4 -29.2Q33.1 -28.8 35.8 -27.9Q38.5 -27.1 40.8 -25.5Q43.1 -24 44.5 -21.5Q45.8 -19 45.8 -15.2Q45.8 -10.4 43.4 -6.7Q40.9 -3 36.3 -0.9Q31.7 1.2 25 1.2ZM53.3 0V-49.6H64.9V0ZM53.2 -58.3V-70.3H65.2V-58.3ZM73.5 0V-72H85.1V0ZM93.7 0V-49.6H105.3V0ZM93.6 -58.3V-70.3H105.6V-58.3ZM137.2 1.2Q129.8 1.2 124 -2.1Q118.2 -5.4 114.9 -11.3Q111.6 -17.1 111.6 -24.8Q111.6 -32.5 114.9 -38.3Q118.2 -44.2 124 -47.5Q129.8 -50.8 137.2 -50.8Q146.5 -50.8 152.8 -45.9Q159.2 -41.1 161 -32.7H148.8Q147.9 -36.5 144.7 -38.7Q141.5 -40.9 137.1 -40.9Q133.5 -40.9 130.3 -39Q127.2 -37.1 125.3 -33.5Q123.4 -30 123.4 -24.8Q123.4 -19.7 125.3 -16.1Q127.2 -12.5 130.3 -10.6Q133.5 -8.7 137.1 -8.7Q141.5 -8.7 144.7 -10.8Q147.9 -13 148.8 -16.9H161Q159.3 -8.8 152.9 -3.8Q146.5 1.2 137.2 1.2ZM184.2 1.2Q178.5 1.2 174.4 -0.8Q170.4 -2.9 168.3 -6.3Q166.2 -9.7 166.2 -13.7Q166.2 -21.6 171.9 -25.8Q177.6 -30.1 186.8 -30.1Q191.1 -30.1 194.3 -29.5Q197.5 -28.9 199.6 -28.2V-30.5Q199.6 -36.1 196.5 -38.8Q193.4 -41.5 188.4 -41.5Q184.6 -41.5 181.8 -40Q178.9 -38.5 178.2 -34.6H167Q167.6 -39.9 170.6 -43.5Q173.6 -47.1 178.3 -49Q182.9 -50.8 188.4 -50.8Q199.3 -50.8 205.2 -45.4Q211.2 -40 211.2 -30.5V-18Q211.2 -14.3 211.6 -12.5Q212.1 -10.7 213.1 -10.3Q214.2 -9.8 215.9 -9.8H216.4V0H213.9Q208.8 0 205.4 -1.5Q202 -2.9 200.6 -7.3Q198.2 -3.6 194.1 -1.2Q190 1.2 184.2 1.2ZM186.6 -7.8Q190.2 -7.8 192.8 -9.5Q195.4 -11.2 197 -14.1Q198.6 -17 199 -20.4Q196.9 -21 193.9 -21.6Q191 -22.2 188 -22.2Q183.1 -22.2 180.5 -20.1Q177.9 -18 177.9 -14.6Q177.9 -11.6 180.2 -9.7Q182.5 -7.8 186.6 -7.8Z"
AGENT = "M22 1.2Q15.8 1.2 11.8 -0.9Q7.7 -3 5.6 -6.4Q3.6 -9.9 3.6 -13.7Q3.6 -21.8 9.5 -26.1Q15.4 -30.4 25.1 -30.4Q29.2 -30.4 32.7 -29.8Q36.3 -29.1 38.5 -28.4V-30.5Q38.5 -37.3 35 -40.7Q31.6 -44.2 25.7 -44.2Q20.7 -44.2 17.1 -42.2Q13.5 -40.1 12.6 -35.3H4.7Q5.3 -40.7 8.4 -44.1Q11.5 -47.5 16 -49.2Q20.6 -50.8 25.7 -50.8Q36.1 -50.8 41.2 -45.2Q46.3 -39.6 46.3 -30.5V-14.5Q46.3 -10.9 46.8 -9.3Q47.2 -7.7 48.3 -7.3Q49.5 -6.9 51.4 -6.9H52.2V0H50.5Q45.8 0 42.8 -1.7Q39.8 -3.3 38.9 -8.4Q36.6 -4.4 32.6 -1.6Q28.5 1.2 22 1.2ZM23 -5.3Q27.7 -5.3 31.1 -7.8Q34.6 -10.2 36.5 -14.1Q38.3 -18 38.4 -22.3Q36.1 -23 32.6 -23.6Q29.2 -24.2 25.7 -24.2Q18.1 -24.2 14.8 -21.3Q11.5 -18.3 11.5 -14Q11.5 -10.3 14.5 -7.8Q17.5 -5.3 23 -5.3ZM84.7 23.2Q78.1 23.2 72.8 20.9Q67.5 18.7 63.9 14.5Q60.4 10.3 59.4 4.4H67.1Q68.7 9.7 73.4 13.1Q78.1 16.4 84.6 16.4Q89.6 16.4 93.7 14.3Q97.7 12.1 100 7.8Q102.3 3.5 102.3 -2.8V-10.2Q100.8 -7.3 98.1 -4.8Q95.4 -2.2 91.5 -0.6Q87.7 1.1 82.7 1.1Q75.8 1.1 70.1 -2.3Q64.4 -5.7 61.1 -11.6Q57.8 -17.5 57.8 -25Q57.8 -32.4 61.1 -38.3Q64.4 -44.1 70.1 -47.5Q75.8 -50.8 82.8 -50.8Q89.6 -50.8 94.7 -47.8Q99.8 -44.7 102.3 -40.4V-49.6H110.1V-2.9Q110.1 5.2 106.9 11Q103.6 16.9 97.9 20Q92.1 23.2 84.7 23.2ZM83.9 -5.7Q89 -5.7 93.1 -8.1Q97.2 -10.6 99.6 -14.9Q102 -19.3 102 -24.9Q102 -30.7 99.6 -35Q97.2 -39.3 93.1 -41.6Q89 -44 83.9 -44Q78.7 -44 74.6 -41.6Q70.5 -39.3 68.1 -35Q65.8 -30.7 65.8 -25Q65.8 -19.3 68.1 -14.9Q70.5 -10.6 74.6 -8.1Q78.7 -5.7 83.9 -5.7ZM143.2 1.2Q136.1 1.2 130.6 -2.1Q125.2 -5.3 122.1 -11.1Q118.9 -17 118.9 -24.8Q118.9 -32.7 122 -38.5Q125.1 -44.4 130.6 -47.6Q136.2 -50.8 143.3 -50.8Q150.6 -50.8 155.7 -47.6Q160.9 -44.3 163.7 -39.1Q166.4 -33.8 166.4 -27.7Q166.4 -26.7 166.4 -25.6Q166.4 -24.5 166.3 -23.1H124.9V-29.4H158.8Q158.5 -36.3 154.1 -40.3Q149.6 -44.2 143.1 -44.2Q138.7 -44.2 134.9 -42.3Q131.2 -40.3 128.8 -36.6Q126.5 -32.8 126.5 -27.2V-24.5Q126.5 -18.1 128.9 -13.8Q131.3 -9.6 135.1 -7.5Q138.9 -5.4 143.2 -5.4Q148.5 -5.4 152.2 -7.9Q155.8 -10.4 157.4 -14.7H165Q163.8 -10.2 160.8 -6.6Q157.9 -2.9 153.4 -0.9Q149 1.2 143.2 1.2ZM176.7 0V-49.6H184.5V-41.1Q187 -45.7 191.5 -48.2Q195.9 -50.8 201.4 -50.8Q210.1 -50.8 215.4 -45.5Q220.7 -40.2 220.7 -29.1V0H212.9V-28.3Q212.9 -36.2 209.5 -40.1Q206.2 -44 199.8 -44Q193.2 -44 188.9 -39.3Q184.5 -34.6 184.5 -25.7V0ZM248.2 0Q244.3 0 241.5 -1Q238.8 -2 237.4 -4.8Q236 -7.5 236 -12.6V-42.9H227.4V-49.6H236V-61.3H243.8V-49.6H258.3V-42.9H243.8V-14Q243.8 -10.5 244.3 -9Q244.9 -7.4 246.5 -7Q248.1 -6.7 251.4 -6.7H257.4V0Z"
WORD_X1, AGENT_X1, ASCENDER, SPACE = 216.44, 258.27, 72.0, 30.7
# The g of "agent" goes below the ground line, and the banner was 256 tall
# whatever the words did: Archivo's g lost 16 of its 32 units to the edge, Host
# Grotesk's would lose 25 of 41.
DESCENT = 23.2

# The word stands on the mark's ground line and its ascender meets the top of
# the first shell, so the type and the mark share two edges instead of floating
# at an eyeballed centre; it starts one shell and its gap (3u) after the mark.
WORD_K = (SEED + GAP + SHELL) / ASCENDER
WORD_X = GROUND + 3 * U
AGENT_X = WORD_X + (WORD_X1 + SPACE) * WORD_K


def poly(points) -> str:
    return "M" + " L".join(f"{x:g} {y:g}" for x, y in points) + " Z"


def mark_paths(thin: float = 0) -> tuple[str, str]:
    """(shells, seed) as path data. Each shell is an L standing on the ground
    line and the left edge; eroding moves every edge in by `thin`, ends too."""
    x, y, t = PAD, GROUND, thin
    seed = poly([(x + t, y - SEED + t), (x + SEED - t, y - SEED + t), (x + SEED - t, y - t), (x + t, y - t)])
    shells, edge = [], SEED
    for _ in range(SHELLS):
        a, b = edge + GAP, edge + GAP + SHELL  # the shell's inner and outer reach
        shells.append(poly([(x + t, y - b + t), (x + b - t, y - b + t), (x + b - t, y - t),
                            (x + a + t, y - t), (x + a + t, y - a - t), (x + t, y - a - t)]))
        edge = b
    return " ".join(shells), seed


def words() -> tuple[float, str]:
    """(lockup width, the words as paths) for the horizontal lockup."""
    scale = f"scale({WORD_K:.4f})"
    out = (f'<path transform="translate({WORD_X} {GROUND}) {scale}" d="{WORD}"/>'
           f'<path transform="translate({AGENT_X:.2f} {GROUND}) {scale}" d="{AGENT}"/>')
    return round(AGENT_X + AGENT_X1 * WORD_K + PAD, 2), out


def svg(w: float, body: str, label: str = "Silica", h: float = 256) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:g} {h:g}" width="{w:g}" height="{h:g}" '
            f'role="img" aria-label="{label}"><title>{label}</title>{body}</svg>\n')


def symbol() -> str:
    """The master: it follows prefers-color-scheme, as a tab icon has to."""
    shells, seed = mark_paths()
    style = (f"<style>.i{{fill:{PAPER['ink']}}}.s{{fill:{PAPER['sig']}}}@media(prefers-color-scheme:dark)"
             f"{{.i{{fill:{GRAPHITE['ink']}}}.s{{fill:{GRAPHITE['sig']}}}}}</style>")
    return svg(256, f'{style}<path class="i" d="{shells}"/><path class="s" d="{seed}"/>')


def banner(t: dict, thin: float) -> str:
    """The banner is embedded as an <img>, so currentColor has nothing to
    inherit and each theme states its own ink."""
    shells, seed = mark_paths(thin)
    w, ws = words()
    h = round(GROUND + DESCENT * WORD_K + PAD, 2)  # the g's tail, then the same 1u as above the mark
    return svg(w, f'<path fill="{t["ink"]}" d="{shells}"/><path fill="{t["sig"]}" d="{seed}"/>'
                  f'<g fill="{t["ink"]}">{ws}</g>', "Silica Agent", h)


def small_mark(indent: str) -> str:
    """The mark at 16px, for the site's nav (the GUI's drawer head lost its
    mark to the vault with ADR-0122): 16 puts
    a unit on one pixel, where the old mark's 20 would put it on a pixel and a
    quarter and blur every edge. Inked by the page, so the seed takes the accent
    only where the page's CSS gives .seed one, and is ink otherwise."""
    shells, seed = mark_paths()
    return (f'{indent}<!-- the mark, generated by assets/build_banners.py -->\n'
            f'{indent}<svg class="brand-mark" viewBox="0 0 256 256" width="16" height="16" aria-hidden="true"'
            f' focusable="false"><path fill="currentColor" d="{shells}"/>'
            f'<path class="seed" fill="currentColor" d="{seed}"/></svg>')


def check() -> None:
    """The failure that renders anyway: an edge off the 16-unit grid still
    draws, just blurred at 16px, which is the size the mark exists for."""
    shells, seed = mark_paths()
    assert all(float(v) % U == 0 for v in re.findall(r"[\d.]+", shells + seed)), "an edge left the grid"


def emit(path: Path, text: str) -> None:
    before = path.read_text() if path.exists() else ""
    path.write_text(text)
    print(f"{path.relative_to(HERE.parent)}: {'unchanged' if text == before else 'updated'} ({len(text) // 1024} KB)")


def patch(page: Path, opening: str, block: str) -> None:
    """Swap the generated block for a new one. The page is hand-written around
    it, so the block is found by its opening comment and nothing else moves."""
    before = page.read_text()
    after, n = re.subn(re.escape(opening) + r".*?</svg>", lambda _: block, before, count=1, flags=re.S)
    if n != 1:
        raise SystemExit(f"{page.relative_to(HERE.parent)}: generated block not found")
    emit(page, after)


def build_rasters() -> None:
    """Export the GUI's WebPs from the mascot PNG, alpha kept."""
    from PIL import Image

    src = Image.open(HERE / MASCOT_PNG).convert("RGBA")
    for name, width in RASTERS:
        out = STATIC / name
        height = round(src.height * width / src.width)
        src.resize((width, height), Image.LANCZOS).save(out, "WEBP", quality=90, method=6)
        print(f"{name}: {width}x{height} ({out.stat().st_size // 1024} KB)")


def main() -> None:
    check()
    emit(HERE / MARK, symbol())
    for out in ART_COPIES:
        emit(out, symbol())
    patch(WEB / "index.html", "        <!-- the mark, generated", small_mark(" " * 8))
    build_rasters()
    emit(HERE / "banner.svg", banner(GRAPHITE, THIN))
    emit(HERE / "banner-light.svg", banner(PAPER, 0))


if __name__ == "__main__":
    main()
