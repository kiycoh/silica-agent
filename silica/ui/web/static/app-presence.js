// --- the presence: the mark, lit from its seed ----------------------------------------
// The lockup's own mark drawn in depth, standing for the assistant (ADR-0048,
// amendment 2026-10-21): three glass shells round a solid seed, seen straight on
// and never moving, with light that leaves the seed's corner and crosses the
// shells one at a time. It replaced the quartz orb that sat above the lockup,
// because the orb and the mark were two marks on one screen. On the landing it
// lies on the lockup's static mark at the same 64 px; in the dock it is the face
// of the folded disc and the head of the open row (42 px, scaled to 36). In the
// chat the run line carries it in 2D instead (app-chat.css .rl-mark). It leaves
// idle only for work happening on this screen, and the states are the orb's.

// ---- the mark, in grid units (u = 16 on the 256 master) ---------------------------
// The seed is 5u at the corner (1,1); shell k spans [3k+4, 3k+6] on both axes.
// ring = max(x,y) - 1 is the Chebyshev distance from the seed's outer corner and
// its level sets are the shells themselves, so a square wave in ring crosses one
// shell at a time. The seed stands 5u and the glass 3u: the light has to come out
// of something that rises above what it lights.
const MARK_PIECES = [{ rects: [[1, 1, 6, 6]], poly: [[1, 1], [6, 1], [6, 6], [1, 6]], h: 5 }];
for (let k = 1; k <= 3; k++) {
  const lo = 3 * k + 4, hi = 3 * k + 6;
  MARK_PIECES.push({ rects: [[1, lo, hi, hi], [lo, 1, hi, lo]],
    poly: [[lo, 1], [hi, 1], [hi, hi], [1, hi], [1, lo], [lo, lo]], h: 3 });
}

// Extruded polygons, seven floats a vertex (position, normal, piece). The seed
// comes first and the glass after it, because they draw in two passes.
function markMesh() {
  const v = [];
  const push = (x, y, z, n, p) => v.push(x, y, z, n[0], n[1], n[2], p);
  let glassAt = 0;
  MARK_PIECES.forEach((pc, pi) => {
    if (pi === 1) glassAt = v.length / 7;
    const z = pc.h, up = [0, 0, 1], dn = [0, 0, -1];
    for (const [x0, y0, x1, y1] of pc.rects) {
      push(x0, y0, z, up, pi); push(x1, y0, z, up, pi); push(x1, y1, z, up, pi);
      push(x0, y0, z, up, pi); push(x1, y1, z, up, pi); push(x0, y1, z, up, pi);
      push(x0, y0, 0, dn, pi); push(x1, y1, 0, dn, pi); push(x1, y0, 0, dn, pi);
      push(x0, y0, 0, dn, pi); push(x0, y1, 0, dn, pi); push(x1, y1, 0, dn, pi);
    }
    const P = pc.poly;
    for (let i = 0; i < P.length; i++) {
      const [ax, ay] = P[i], [bx, by] = P[(i + 1) % P.length];
      const len = Math.hypot(bx - ax, by - ay), n = [(by - ay) / len, -(bx - ax) / len, 0];
      push(ax, ay, 0, n, pi); push(bx, by, 0, n, pi); push(bx, by, z, n, pi);
      push(ax, ay, 0, n, pi); push(bx, by, z, n, pi); push(ax, ay, z, n, pi);
    }
  });
  return { data: new Float32Array(v), glassAt, count: v.length / 7 };
}

// ---- the camera ---------------------------------------------------------------------
// Straight on, through the mark's centre, and never moving (owner, 2026-10-21: no
// float and no tilt, which the orb had): the mark stays upright as it is drawn and
// its depth shows only as perspective, every wall leaning in toward the centre.
// Three oblique cameras were tried first and every one read as the logo tilted.
const MARK_EYE = [8, 8, 48];
const MARK_LIGHT = (() => { const l = [-0.62, -0.38, 0.68], n = Math.hypot(...l); return l.map((x) => x / n); })();
const MARK_VP = (() => {
  const mul = (a, b) => {
    const o = new Float32Array(16);
    for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) {
      let s = 0;
      for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k];
      o[c * 4 + r] = s;
    }
    return o;
  };
  // a 24 degree field, column-major as WebGL reads it
  const t = 1 / Math.tan((12 * Math.PI) / 180), n = 1, f = 120;
  const proj = new Float32Array([t, 0, 0, 0, 0, t, 0, 0, 0, 0, (f + n) / (n - f), -1, 0, 0, (2 * f * n) / (n - f), 0]);
  // looking straight down -z with +y up, the view is only a translation
  const view = new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, -MARK_EYE[0], -MARK_EYE[1], -MARK_EYE[2], 1]);
  const vp = mul(proj, view);
  // Fit the outline the eye sees, the top faces, to 7/8 of the canvas: the static
  // mark fills 14 of its 16 units, so on the landing's 64 px the drawn mark lands
  // on the static one's pixels and the word's baseline on its foot.
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const [x, y, z] of [[1, 1, 5], [6, 6, 5], [1, 1, 3], [15, 15, 3]]) {
    const w = vp[3] * x + vp[7] * y + vp[11] * z + vp[15];
    const sx = (vp[0] * x + vp[4] * y + vp[8] * z + vp[12]) / w;
    const sy = (vp[1] * x + vp[5] * y + vp[9] * z + vp[13]) / w;
    x0 = Math.min(x0, sx); x1 = Math.max(x1, sx); y0 = Math.min(y0, sy); y1 = Math.max(y1, sy);
  }
  const s = (2 * 0.875) / Math.max(x1 - x0, y1 - y0);
  return mul(new Float32Array([s, 0, 0, 0, 0, s, 0, 0, 0, 0, 1, 0, (-s * (x0 + x1)) / 2, (-s * (y0 + y1)) / 2, 0, 1]), vp);
})();

const MARK_VERT = `#version 300 es
in vec3 aPos; in vec3 aNor; in float aPiece;
uniform mat4 uVP;
out vec3 vPos; out vec3 vN;
flat out int vPiece;
void main() {
  vPos = aPos; vN = aNor; vPiece = int(aPiece + .5);
  gl_Position = uVP * vec4(aPos, 1.);
}`;

const MARK_FRAG = `#version 300 es
precision highp float;
in vec3 vPos; in vec3 vN;
flat in int vPiece;
uniform vec3 uInk, uAcc, uHot, uLit, uBg, uAsh;
uniform float uDrain, uT, uSeed;
uniform vec4 uP[8];    // a pulse: start time, strength, speed (u/s), reach (u, 0 = all)
uniform vec3 uEye, uLight;
out vec4 o;

// a front that arrives sharp and leaves a long tail behind it
float g(float x) { return x < 0. ? exp(-x * x * .9) : exp(-x * .45); }

float wave(float ring) {
  float a = 0.;
  for (int i = 0; i < 8; i++) {
    vec4 p = uP[i];
    float tau = uT - p.x;
    if (p.y <= 0. || tau < 0.) continue;
    float reach = p.w > 0. ? exp(-ring / p.w) : 1.;
    a += p.y * reach * g(tau * p.z - ring);
  }
  return a;
}

void main() {
  vec3 n = normalize(vN);
  float ring = max(vPos.x, vPos.y) - 1.;
  bool top = n.z > .5;
  vec3 acc = mix(uAcc, uAsh, uDrain), hot = mix(uHot, uAsh, uDrain), shine = mix(uLit, uAsh, uDrain);
  float a = wave(ring) + (vPiece == 0 ? uSeed * .35 : 0.);
  a = clamp(a, 0., 1.) * (1. - .6 * uDrain);

  if (vPiece > 0) {
    // glass: nearly clear where it faces the eye, bright at grazing angles, and
    // lit from inside as the wave crosses it
    vec3 v = normalize(uEye - vPos);
    float fr = pow(1. - abs(dot(n, v)), 2.);
    float al = clamp((top ? .14 : .24) + .6 * fr + .8 * a, 0., 1.);
    vec3 c = mix(mix(uInk, acc, .45), mix(shine, hot, .5), a);
    o = vec4(c * al, al);
    return;
  }
  // the seed is solid: its top carries the logo's accent, its sides step toward
  // the ground, less where the light falls on them and less while it glows
  float lam = clamp(dot(n, uLight), 0., 1.);
  vec3 col = mix(acc, hot, a);
  float side = top ? 0. : .55 * mix(.72, .36, lam);
  o = vec4(mix(col, uBg, side * (1. - .4 * a)), 1.);
}`;

// The pulses each state sends out of the seed: `every` seconds, at `amp`, moving
// `v` units a second. `drain` walks the colours to grey. Stalled is slow and
// drained, never broken, because the run line's sentence beside it says "slow,
// not gone"; error is the stall taken to its end, grey and still.
const MARK_STATES = {
  idle:     { every: 5.5, amp: 0.55, v: 6, drain: 0 },
  thinking: { every: 1.05, amp: 1, v: 11, drain: 0 },
  acting:   { every: 1.7, amp: 0.6, v: 8, drain: 0 },
  success:  { every: 0, amp: 1.5, v: 16, drain: 0 },
  stalled:  { every: 4.5, amp: 0.35, v: 3.5, drain: 0.7 },
  error:    { every: 0, amp: 0, v: 1, drain: 1 },
};

const markProbe = document.createElement("span");
markProbe.hidden = true;
document.body.appendChild(markProbe);

function mountPresence(canvas) {
  if (!canvas) return null;
  // Without WebGL2 (a GPU blocklist, a remote desktop) the canvas goes: on the
  // landing the static mark under it shows again, in a composer the row is its
  // controls.
  const gl = canvas.getContext("webgl2", { premultipliedAlpha: true, antialias: true });
  if (!gl) { canvas.remove(); return null; }
  const compile = (type, src) => {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) console.warn("[presence]", gl.getShaderInfoLog(s));
    return s;
  };
  const prog = gl.createProgram();
  gl.attachShader(prog, compile(gl.VERTEX_SHADER, MARK_VERT));
  gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, MARK_FRAG));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    console.warn("[presence]", gl.getProgramInfoLog(prog));
    canvas.remove();
    return null;
  }
  gl.useProgram(prog);
  const mesh = markMesh();
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ARRAY_BUFFER, mesh.data, gl.STATIC_DRAW);
  [["aPos", 3, 0], ["aNor", 3, 12], ["aPiece", 1, 24]].forEach(([name, size, off]) => {
    const loc = gl.getAttribLocation(prog, name);
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, size, gl.FLOAT, false, 28, off);
  });
  const u = {};
  for (const n of ["uVP", "uInk", "uAcc", "uHot", "uLit", "uBg", "uAsh", "uDrain", "uT", "uSeed", "uP", "uEye", "uLight"])
    u[n] = gl.getUniformLocation(prog, n);
  gl.uniformMatrix4fv(u.uVP, false, MARK_VP);
  gl.uniform3fv(u.uEye, MARK_EYE);
  gl.uniform3fv(u.uLight, MARK_LIGHT);
  // A lost context is not worth a rebuild for an ornament: hide the canvas, and
  // the landing's static mark shows through again.
  canvas.addEventListener("webglcontextlost", () => { running = false; canvas.hidden = true; });

  const rgb = (css) => { const m = css.match(/[\d.]+/g) || [0, 0, 0]; return [m[0] / 255, m[1] / 255, m[2] / 255]; };
  const token = (name) => { markProbe.style.color = `var(${name})`; return rgb(getComputedStyle(markProbe).color); };
  // The ground is whatever opaque surface the canvas sits on, read rather than
  // named because the view's plane is a theme decision.
  function readColors() {
    gl.uniform3fv(u.uInk, token("--frost"));
    gl.uniform3fv(u.uAcc, token("--accent"));
    gl.uniform3fv(u.uHot, token("--mark-hot"));
    gl.uniform3fv(u.uLit, token("--mark-lit"));
    gl.uniform3fv(u.uAsh, token("--ash"));
    let bg = [0, 0, 0];
    for (let el = canvas.parentElement; el; el = el.parentElement) {
      const c = getComputedStyle(el).backgroundColor, m = c.match(/[\d.]+/g);
      if (m && (m.length < 4 || +m[3] > 0.5)) { bg = rgb(c); break; }
    }
    gl.uniform3fv(u.uBg, bg);
  }

  // prefers-reduced-motion turns the motion off rather than shortening it: a
  // state is a still, the one frame where its first pulse lies on the middle
  // shell, drawn once on the change.
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const born = performance.now();
  const clock = () => (performance.now() - born) / 1000;
  let cur = "idle", back = 0, pulses = [], lastPulse = -Infinity, drain = 0, speak = 0;
  let running = false, raf = 0, dirty = true, last = 0, stillAt = 0;

  function emit(t, amp, v, reach = 0) {
    pulses.push({ t0: t, amp, v, reach });
    if (pulses.length > 8) pulses.shift(); // the shader reads eight
    lastPulse = t;
  }
  function still() {
    const S = MARK_STATES[cur];
    pulses = S.amp ? [{ t0: 0, amp: S.amp, v: S.v, reach: 0 }] : [];
    stillAt = 10 / S.v;
    drain = S.drain;
  }

  function frame() {
    raf = 0;
    if (!running) return;
    const now = reduced ? stillAt : clock();
    const dt = Math.min(Math.max(now - last, 0), 0.25);
    last = now;
    const S = MARK_STATES[cur];
    if (!reduced) {
      if (S.every && now - lastPulse > S.every) emit(now, S.amp, S.v);
      // text arriving: each delta adds and the sum decays, and every whole unit
      // sends a pulse, at most one every 0.3 s, so a stream reads as a steady
      // beat from the seed and a pause lets it fall quiet
      speak *= Math.exp(-dt / 0.6);
      if (speak >= 1 && now - lastPulse > 0.3) { emit(now, 0.75, 13); speak -= 1; }
      drain += (S.drain - drain) * (1 - Math.exp(-dt / 0.45));
      pulses = pulses.filter((p) => (now - p.t0) * p.v < 60);
    }
    if (dirty || !reduced) {
      dirty = false;
      // the seed's own glow: the pulses passing through it, and a slow breath
      let seed = 0;
      for (const p of pulses) {
        const x = (now - p.t0) * p.v - 2.5;
        if (now >= p.t0) seed += p.amp * (p.reach ? Math.exp(-2.5 / p.reach) : 1) * (x < 0 ? Math.exp(-x * x * 0.9) : Math.exp(-x * 0.45));
      }
      const breath = cur === "error" ? 0 : 0.14 + (reduced ? 0 : 0.1 * Math.sin((now * Math.PI * 2) / 6));
      const P = new Float32Array(32);
      pulses.forEach((p, i) => P.set([p.t0, p.amp * (1 - 0.6 * drain), p.v, p.reach], i * 4));
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.uniform1f(u.uDrain, drain);
      gl.uniform1f(u.uT, now);
      gl.uniform1f(u.uSeed, seed + breath * (1 - drain));
      gl.uniform4fv(u.uP, P);
      // the glass pass leaves depth writes off, and a masked depth buffer does not clear
      gl.depthMask(true);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.enable(gl.DEPTH_TEST); gl.enable(gl.CULL_FACE); gl.cullFace(gl.BACK); gl.disable(gl.BLEND);
      gl.drawArrays(gl.TRIANGLES, 0, mesh.glassAt);
      // glass: the far walls first, then the near ones over them, both blended
      // and neither writing depth, so a shell never hides the one behind it
      gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA); gl.depthMask(false);
      gl.cullFace(gl.FRONT); gl.drawArrays(gl.TRIANGLES, mesh.glassAt, mesh.count - mesh.glassAt);
      gl.cullFace(gl.BACK); gl.drawArrays(gl.TRIANGLES, mesh.glassAt, mesh.count - mesh.glassAt);
    }
    if (!reduced) raf = requestAnimationFrame(frame);
  }
  const kick = () => { dirty = true; if (running && !raf) raf = requestAnimationFrame(frame); };

  // The backing store follows the CSS box. Measured from both observers, not the
  // resize one alone: a composer's mark is display:none while its view is
  // hidden, and in Chrome that first display:none -> shown transition reached
  // the intersection observer and not the resize one, so the mark drew 300x150
  // into a 32px box.
  function fit() {
    // offsetWidth, not the client rect: the dock's mark is drawn scaled while its
    // composer is open (app-panels.css), and a rect taken then would size the
    // backing store for 36 px and blur the 42 px mark once it is back at rest
    const w = canvas.offsetWidth;
    if (!w) return;
    const px = Math.round(w * Math.min(window.devicePixelRatio || 1, 2));
    if (canvas.width !== px) canvas.width = canvas.height = px;
  }
  // Frames only while the canvas is on screen: the landing's while the
  // transcript is empty, a composer's once it has something in it and its view
  // shows. Both are display:none otherwise, which the observer reports.
  new IntersectionObserver(([entry]) => {
    running = entry.isIntersecting && !canvas.hidden;
    last = reduced ? stillAt : clock();
    fit();
    kick();
  }).observe(canvas);
  new ResizeObserver(() => { fit(); kick(); }).observe(canvas);
  new MutationObserver(() => { readColors(); kick(); })
    .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  // The mark listens to the field it sits beside: the dock's to the dock's
  // textarea, the landing's to the chat composer it looks down on. A keystroke
  // is a short pulse that dies out before the outer shell.
  const field = canvas.closest(".composer-shell")?.querySelector("textarea") || document.getElementById("input");
  field?.addEventListener("input", () => { if (!reduced) { emit(clock(), 0.45, 10, 5); kick(); } });

  readColors();
  if (reduced) still();

  return {
    state(name) {
      if (!MARK_STATES[name] || name === cur) return;
      cur = name;
      clearTimeout(back);
      if (reduced) still();
      else {
        const t = clock();
        lastPulse = -Infinity;
        if (name === "success") { emit(t, 1.5, 16); emit(t + 0.14, 0.8, 16); }
        else if (name !== "error") emit(t, MARK_STATES[name].amp, MARK_STATES[name].v);
      }
      // success and error are arrivals, not places to stay; error holds longer
      // because grey and still is a quieter reading than the swell
      if (name === "success") back = setTimeout(() => this.state("idle"), 1600);
      if (name === "error") back = setTimeout(() => this.state("idle"), 2600);
      kick();
    },
    // a text delta landed; under reduced motion nothing decays, so nothing lights
    spoke() {
      if (reduced) return;
      speak = Math.min(1.5, speak + 0.5);
      kick();
    },
  };
}

const landingPresence = mountPresence(document.querySelector("#vault-hero .vh-presence"));

// The dock's mark (ADR-0048, second site). The chat's composer carried one too
// until 2026-10-22, when a mark inside the field read as a button and the run
// line took the presence in 2D. One handle still, so work.js and app-chat.js
// report a turn without knowing how many places watch it.
const composerPresence = (() => {
  const all = [...document.querySelectorAll(".composer-shell .cp-presence")].map((c) => mountPresence(c)).filter(Boolean);
  return {
    state(name) { for (const p of all) p.state(name); },
    spoke() { for (const p of all) p.spoke(); },
  };
})();
