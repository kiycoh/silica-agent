/* The wait, drawn as ASCII wake's hand running over a row of cells (bencho.dev, MIT;
   ADR-0146 point 3). Every .frame-loading carries one in place of the spinner, the view's own
   message under it. The spinner went because it spun the accent, and teal is what you can
   press; the wake is the landing's own figure of a hand at work, in ink.
   One loop serves every row and runs only while a row is on screen: a row inside a hidden
   overlay or an inactive view has no offsetParent, and a change to `hidden` or `class` anywhere
   is what can put one on screen, so that is what wakes the loop. */
(() => {
  "use strict";
  const RAMP = [" ", "·", "░", "▒", "▓", "█"];
  const CELLS = 16, CW = 9, CH = 16;
  // cells a second the hand covers, and the cooling rate behind it: the trail is about four
  // cells long, which reads as a hand moving rather than a bar filling
  const SPEED = 18, COOL = 6;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const state = new WeakMap();
  let raf = 0, last = 0;

  function start(cv) {
    const r = Math.min(2, window.devicePixelRatio || 1);
    cv.width = CELLS * CW * r; cv.height = CH * r;
    const g = cv.getContext("2d");
    g.setTransform(r, 0, 0, r, 0, 0);
    const cs = getComputedStyle(cv);
    g.font = `500 13px ${cs.fontFamily}`; g.textAlign = "center"; g.textBaseline = "middle";
    // a still row under reduced motion: the hand stopped a third of the way in, its trail behind
    const heat = new Float32Array(CELLS);
    if (reduce) for (let i = 0; i < 6; i++) heat[5 - i] = Math.exp(-i * .45);
    return { g, ink: cs.color, heat, pos: 0, drawn: false };
  }
  function draw(s) {
    const { g, heat } = s;
    g.clearRect(0, 0, CELLS * CW, CH); g.fillStyle = s.ink;
    for (let i = 0; i < CELLS; i++) {
      const k = Math.round(heat[i] * (RAMP.length - 1));
      if (k) g.fillText(RAMP[k], (i + .5) * CW, CH / 2);
    }
  }
  function frame(now) {
    const dt = Math.min(.05, (now - last) / 1000); last = now;
    let moving = false;
    for (const host of document.querySelectorAll(".wake")) {
      if (host.offsetParent === null) { state.delete(host); continue; }
      // the canvas is made here and not written in the page: the shell's markup holds no canvas
      // since the WebGL presence left it (ADR-0048), and a test keeps it that way
      const cv = host.firstElementChild || host.appendChild(document.createElement("canvas"));
      let s = state.get(host);
      if (!s) { s = start(cv); state.set(host, s); }
      if (reduce) { if (!s.drawn) { draw(s); s.drawn = true; } continue; }
      // past the last cell the hand runs on unseen for four cells, so the row empties before
      // it comes round again
      s.pos = (s.pos + dt * SPEED) % (CELLS + 4);
      const keep = Math.exp(-dt * COOL);
      for (let i = 0; i < CELLS; i++) { s.heat[i] *= keep; if (Math.abs(i - s.pos) < .7) s.heat[i] = 1; }
      draw(s); moving = true;
    }
    raf = moving ? requestAnimationFrame(frame) : 0;
  }
  function wake() { if (!raf) { last = performance.now(); raf = requestAnimationFrame(frame); } }
  new MutationObserver(wake).observe(document.body, { subtree: true, attributes: true, attributeFilter: ["hidden", "class"] });
  wake();
})();
