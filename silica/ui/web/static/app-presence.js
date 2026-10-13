// --- the landing presence ---------------------------------------------------------
// A quartz point in a glass body, above the wordmark while the chat is empty.
// ADR-0048 says why a shader and why quartz. It stands for the assistant, so it
// leaves idle only for work happening on this screen: it thinks while the worker
// model writes the vault sentence, breathes out when the sentence lands, and
// turns a little quicker while you type. The crystal is seeded by the vault's
// path, so each folder keeps its own stone.
//
// The glass body, palette, surface and slab-method solid are ported from Murmur's
// MurmurGlass.metal (mh_body, mh_surface, mh_present, mh_geode), and the state
// seeds, entries and signal envelope from MurmurState.swift and
// MurmurSignals.swift. The prototype, with the two interiors that lost, is
// app-prototype-presence.js, reachable with ?presence=A|B|C.
//
// Murmur is under this licence:
//
//   MIT License
//
//   Copyright (c) 2026 Kris Puckett
//
//   Permission is hereby granted, free of charge, to any person obtaining a copy
//   of this software and associated documentation files (the "Software"), to deal
//   in the Software without restriction, including without limitation the rights
//   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
//   copies of the Software, and to permit persons to whom the Software is
//   furnished to do so, subject to the following conditions:
//
//   The above copyright notice and this permission notice shall be included in
//   all copies or substantial portions of the Software.
//
//   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
//   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
//   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
//   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
//   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
//   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
//   SOFTWARE.

const PRESENCE_FRAG = `#version 300 es
precision highp float;
precision highp int;

uniform vec2 uRes;
uniform float uSizePt;
uniform float uTime;
uniform float uYaw;
uniform vec3 uInk;
uniform vec3 uTone;
uniform float uDepth;
uniform float uGlow;
uniform float uStateIndex;
uniform float uStateTau;
uniform float uThink;
uniform float uRespond; // text is arriving: the run line's own signal, decays in JS
uniform float uCrystalOnly; // 1 = the crystal without the glass body: the composers
uniform float uSway;   // the orb's float, from JS: a roll added on the view axis (rad)
uniform float uBob;    // and a drop inside the glass, in body radii (+ = down)
uniform float uSpark;  // the catchlight's strength this frame, 0..1
uniform vec4 uSeedA;   // roll, pitch, yaw offset, z-face inset
uniform vec4 uSeedB;   // upper and lower prism half-lengths, phantom scale and shift
uniform float uJit[6]; // how far each prism face sits off the regular hexagon

out vec4 fragColor;

const float PI = 3.14159265;
const float MH_R = 0.300;
const float MH_ETA = 1.0 / 1.20;
const float MH_EXT = 0.55;
const float MH_SPREAD = 0.50;
// geode's character dials, at the defaults the prototype was judged on
const float FACET = 0.5, GLIMMER = 0.4, SPREAD = 0.5;
// quartz r {10-11}: the face normal sits 51.8 deg off the c axis
const float SIN_RHO = 0.7859;
const float COS_RHO = 0.6184;
const float TAN_RHO = 1.2708;

// ---- the kit ----------------------------------------------------------------------
uint mh_hash(uvec3 v) {
  uint h = (v.x * 1597334673u) ^ (v.y * 3812015801u) ^ (v.z * 2798796415u);
  h ^= h >> 15; h *= 2246822519u;
  h ^= h >> 13; h *= 3266489917u;
  h ^= h >> 16;
  return h;
}

vec3 mh_grad3(ivec3 c) {
  uint h = mh_hash(uvec3(c + 4096));
  float z = float(h & 0xFFFFu) * (2.0 / 65535.0) - 1.0;
  float a = float((h >> 16) & 0xFFFFu) * (6.28318530718 / 65536.0);
  float r = sqrt(max(0.0, 1.0 - z * z));
  return vec3(r * cos(a), r * sin(a), z);
}

float mh_noise3(vec3 p) {
  vec3 i = floor(p);
  vec3 f = p - i;
  vec3 u = f * f * f * (f * (f * 6.0 - 15.0) + 10.0);
  ivec3 c = ivec3(i);
  float va = dot(mh_grad3(c + ivec3(0, 0, 0)), f - vec3(0.0, 0.0, 0.0));
  float vb = dot(mh_grad3(c + ivec3(1, 0, 0)), f - vec3(1.0, 0.0, 0.0));
  float vc = dot(mh_grad3(c + ivec3(0, 1, 0)), f - vec3(0.0, 1.0, 0.0));
  float vd = dot(mh_grad3(c + ivec3(1, 1, 0)), f - vec3(1.0, 1.0, 0.0));
  float ve = dot(mh_grad3(c + ivec3(0, 0, 1)), f - vec3(0.0, 0.0, 1.0));
  float vf = dot(mh_grad3(c + ivec3(1, 0, 1)), f - vec3(1.0, 0.0, 1.0));
  float vg = dot(mh_grad3(c + ivec3(0, 1, 1)), f - vec3(0.0, 1.0, 1.0));
  float vh = dot(mh_grad3(c + ivec3(1, 1, 1)), f - vec3(1.0, 1.0, 1.0));
  return mix(mix(mix(va, vb, u.x), mix(vc, vd, u.x), u.y),
             mix(mix(ve, vf, u.x), mix(vg, vh, u.x), u.y), u.z);
}

vec3 s2l(vec3 c) { c = max(c, 0.0); return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(0.04045, c)); }
vec3 l2s(vec3 c) { c = max(c, 0.0); return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c)); }

vec3 toLab(vec3 c) {
  float l = 0.4122214708 * c.r + 0.5363325363 * c.g + 0.0514459929 * c.b;
  float m = 0.2119034982 * c.r + 0.6806995451 * c.g + 0.1073969566 * c.b;
  float s = 0.0883024619 * c.r + 0.2817188376 * c.g + 0.6299787005 * c.b;
  float l_ = pow(max(l, 0.0), 1.0 / 3.0);
  float m_ = pow(max(m, 0.0), 1.0 / 3.0);
  float s_ = pow(max(s, 0.0), 1.0 / 3.0);
  return vec3(0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
              1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
              0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_);
}

vec3 fromLab(vec3 lab) {
  float l_ = lab.x + 0.3963377774 * lab.y + 0.2158037573 * lab.z;
  float m_ = lab.x - 0.1055613458 * lab.y - 0.0638541728 * lab.z;
  float s_ = lab.x - 0.0894841775 * lab.y - 1.2914855480 * lab.z;
  float l = l_ * l_ * l_, m = m_ * m_ * m_, s = s_ * s_ * s_;
  return vec3( 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
              -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
              -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s);
}

vec3 lch(float L, float C, float h) { return vec3(L, C * cos(h), C * sin(h)); }

struct Pal { vec3 s0; vec3 s1; vec3 s2; vec3 s3; float paper; };

// Murmur walks the shadow -0.35 rad, "warm, toward ember", which is right for its
// amber and turns this teal green. Here it walks toward blue, and the paper rail's
// smaller offsets are mirrored with it.
const float SHADOW_WALK = 0.35;

Pal mh_palette(vec3 inkC, vec3 toneC, float depth) {
  vec3 ink = toLab(s2l(inkC));
  vec3 tone = toLab(s2l(toneC));
  float L = tone.x;
  float C = length(tone.yz);
  float h = atan(tone.z, tone.y);
  float d = clamp(depth, 0.30, 2.00);
  Pal p;
  p.paper = smoothstep(0.50, 0.72, ink.x);
  vec3 d1 = lch(mix(ink.x, L, 0.30 / d), C * (0.52 + 0.10 * d), h + SHADOW_WALK);
  vec3 d2 = lch(L, C, h);
  vec3 d3 = lch(min(L * (1.20 + 0.12 * d), 0.93), C * 0.55, h - 0.10);
  vec3 l1 = lch(mix(ink.x, L, 0.42 / d), C * (0.34 + 0.10 * d), h - 0.05);
  vec3 l2 = lch(L * (0.82 - 0.06 * d), C * (1.20 + 0.14 * d), h);
  vec3 l3 = lch(max(L * (0.52 - 0.05 * d), 0.18), C * (1.05 + 0.10 * d), h + 0.08);
  p.s0 = ink;
  p.s1 = mix(d1, l1, p.paper);
  p.s2 = mix(d2, l2, p.paper);
  p.s3 = mix(d3, l3, p.paper);
  return p;
}

vec3 mh_shade(Pal p, float t, float hue) {
  t = clamp(t, 0.0, 1.0);
  vec3 lab;
  if (t < 0.40) lab = mix(p.s0, p.s1, smoothstep(0.0, 1.0, t * 2.5));
  else if (t < 0.78) lab = mix(p.s1, p.s2, smoothstep(0.0, 1.0, (t - 0.40) * (1.0 / 0.38)));
  else lab = mix(p.s2, p.s3, smoothstep(0.0, 1.0, (t - 0.78) * (1.0 / 0.22)));
  float ch = cos(hue), sh = sin(hue);
  lab.yz = vec2(lab.y * ch - lab.z * sh, lab.y * sh + lab.z * ch);
  return fromLab(lab);
}

float mh_knee(float x, float k) { return x < k ? x : k + (1.0 - k) * (1.0 - exp(-(x - k) / max(1.0 - k, 1e-3))); }

float mh_tier(float e) {
  float x = clamp(e, 0.0, 1.0);
  float body = (x / 0.78) * 0.72;
  float peak = 0.72 + ((x - 0.78) / 0.22) * 0.28;
  return mix(body, peak, smoothstep(0.68, 0.88, x));
}

vec3 mh_lit(Pal pal, float e, float glow, float emis, float hue) {
  float G = max(glow, 0.0);
  float en = clamp(mh_knee(max(e, 0.0) * (0.35 + 0.65 * G), 0.92), 0.0, 1.0);
  float tRail = clamp(mh_tier(en), 0.0, 1.0);
  vec3 col = mh_shade(pal, tRail, hue);
  return col * (1.0 + emis * G * (1.0 - pal.paper) * smoothstep(0.72, 1.0, tRail));
}

float mh_containment(vec2 uv, float reach) { float r = length(uv) * 2.0; return 1.0 - smoothstep(reach, reach + 0.26, r); }

struct St { float complete; float sweep; float settled; };
St g_st;
float g_cov; // the crystal's silhouette: the alpha when there is no body

// Only success has an expression of its own on the landing: the ignition, the
// sweep it sends up the stone, and what is left after it.
St mh_state(float si, float tau) {
  St o; o.complete = 0.0; o.sweep = 0.0; o.settled = 0.0;
  tau = max(tau, 0.0);
  if (si > 3.5 && si < 4.5) {
    float a = clamp(tau / 1.20, 0.0, 1.0);
    o.complete = smoothstep(0.0, 0.30, a) * (1.0 - smoothstep(0.36, 1.0, a));
    o.settled = smoothstep(0.30, 1.05, a);
    o.sweep = smoothstep(0.0, 1.0, clamp(tau / 0.95, 0.0, 1.0));
  }
  return o;
}

float mh_hash1(float cell, float lane) {
  return float(mh_hash(uvec3(uint(int(cell) + 32768), uint(int(lane) + 32768), 0x9E3779B9u)) >> 8) * (1.0 / 16777216.0);
}

vec4 mh_flourish(float t, float lane, float slotLen) {
  float SLOT = max(slotLen, 1.0);
  float slot = floor(t / SLOT);
  float local = t - slot * SLOT;
  float start = 0.9 + (SLOT * 0.28) * mh_hash1(slot, lane);
  float dur = SLOT * (0.24 + 0.16 * mh_hash1(slot + 811.0, lane));
  float u = (local - start) / dur;
  float sn = sin(PI * clamp(u, 0.0, 1.0));
  float env = (u <= 0.0 || u >= 1.0) ? 0.0 : sn * sn;
  return vec4(env, clamp(u, 0.0, 1.0), mh_hash1(slot + 1607.0, lane), dur);
}

float mh_breath(float t, float lane) {
  float a = sin(t * 0.668 + lane);
  float b = sin(t * 0.427 + lane * 2.3 + 1.1);
  return 0.5 + 0.5 * (0.62 * a + 0.38 * b);
}

// ---- the glass body -----------------------------------------------------------------
struct Def { float d; vec3 g; };

Def mh_deform(vec3 n, float t) {
  float a1 = t * 0.083, a2 = t * 0.061 + 2.10, a3 = t * 0.047 + 4.37;
  vec3 ax1 = normalize(vec3(cos(a1), 0.62, sin(a1)));
  vec3 ax2 = normalize(vec3(0.55, cos(a2), sin(a2)));
  vec3 ax3 = normalize(vec3(sin(a3), -0.44, cos(a3)));
  float u1 = dot(n, ax1), u2 = dot(n, ax2), u3 = dot(n, ax3);
  const float NORM = 1.0 / 1.03;
  Def o;
  o.d = (0.55 * sin(1.70 * u1) + 0.30 * sin(2.60 * u2 + 1.9) + 0.18 * sin(3.40 * u3 + 4.1)) * NORM;
  o.g = (0.55 * 1.70 * cos(1.70 * u1) * ax1
       + 0.30 * 2.60 * cos(2.60 * u2 + 1.9) * ax2
       + 0.18 * 3.40 * cos(3.40 * u3 + 4.1) * ax3) * NORM;
  return o;
}

struct Body { float m; vec3 P; vec3 N; float Rd; float rho; float fres; };

Body mh_body(vec2 uv, float t, float px, float amp, float gain) {
  Body o;
  amp = clamp(amp, 0.0, 0.085);
  vec2 s = uv / MH_R;
  o.rho = length(s);
  float z0 = sqrt(max(1.0 - min(o.rho * o.rho, 1.0), 0.0));
  vec3 n0 = normalize(vec3(s, z0) + vec3(0.0, 0.0, 1e-6));
  float R0 = 1.0 + mh_deform(n0, t).d * amp;
  float z1 = sqrt(max(R0 * R0 - o.rho * o.rho, 0.0));
  vec3 n1 = normalize(vec3(s, z1) + vec3(0.0, 0.0, 1e-6));
  Def d1 = mh_deform(n1, t);
  o.Rd = 1.0 + d1.d * amp;
  float z2 = sqrt(max(o.Rd * o.Rd - o.rho * o.rho, 0.0));
  o.P = vec3(s, z2);
  vec3 gt = d1.g * amp;
  gt = gt - dot(gt, n1) * n1;
  o.N = normalize(n1 - (gt * gain) / max(o.Rd, 1e-3));
  float feather = max(0.018, 1.3 * px);
  o.m = 1.0 - smoothstep(o.Rd - feather, o.Rd + feather, o.rho);
  o.fres = 1.0 - clamp(o.N.z, 0.0, 1.0);
  return o;
}

vec3 mh_refract(vec3 V, vec3 N, float eta) {
  float ci = clamp(-dot(V, N), 0.0, 1.0);
  float k = 1.0 - eta * eta * (1.0 - ci * ci);
  if (k <= 0.0) return V;
  return normalize(eta * V + (eta * ci - sqrt(k)) * N);
}

float mh_exit(vec3 P, vec3 rd) {
  float b = dot(P, rd);
  float c = dot(P, P) - 1.0;
  float disc = b * b - c;
  if (disc <= 0.0) return 0.0;
  return clamp(-b + sqrt(disc), 0.0, 2.2);
}

float mh_haze(vec3 p, float t, float scale) {
  vec3 q = p * scale + vec3(t * 0.051, -t * 0.033, t * 0.089);
  return clamp(0.5 + 0.85 * mh_noise3(q), 0.0, 1.0);
}
float mh_medium(vec3 p, float t, float scale) {
  float fog = 1.0 - smoothstep(0.05, 0.98, length(p));
  return fog * (0.55 + 0.45 * mh_haze(p, t, scale));
}
float mh_inside(vec3 p) { return 1.0 - smoothstep(0.76, 0.99, length(p)); }
float mh_transmit(float fres) { return 1.0 - 0.88 * pow(clamp(fres, 0.0, 1.0), 2.2); }
vec3 mh_key(float t) {
  float dr = t * 0.21;
  return normalize(vec3(-0.52 + 0.055 * sin(dr), -0.60 + 0.045 * cos(dr * 0.83), 0.61));
}

struct Surf { float rim; float spec; float glow; };

Surf mh_surface(Body b, float t, float small, float paper) {
  Surf o;
  float rimK = 0.74 * mix(1.0, 1.32, paper);
  vec3 H = normalize(mh_key(t) + vec3(0.0, 0.0, 1.0));
  float nh = clamp(dot(b.N, H), 0.0, 1.0);
  // Murmur tunes the tight lobe for a 36pt body. At the landing's 66px body the
  // same exponent spreads the catchlight into a soft ball beside the crystal,
  // and the broad lobe is cut for the same reason. At the run line's 19px body
  // Murmur's own 16 was the brightest thing in the bead and sat on the spine's
  // upper end, so the small lobe is tighter and dimmer than Murmur's too.
  float tight = mix(320.0, 90.0, small);
  o.spec = (pow(nh, tight) + 0.04 * pow(nh, 4.0)) * b.m * 0.62 * mix(1.0, 0.55, small)
         * mix(0.92, 1.08, 0.5 - 0.5 * clamp(b.N.y, -1.0, 1.0));
  float wrap = 0.55 + 0.45 * clamp(dot(normalize(b.N.xy + vec2(1e-4)), normalize(vec2(0.42, 0.50))), 0.0, 1.0);
  wrap = mix(wrap, 0.88, paper);
  float sky = 0.5 - 0.5 * clamp(b.N.y, -1.0, 1.0);
  float envRim = mix(mix(0.86, 1.14, sky), mix(1.14, 0.86, sky), paper);
  o.rim = pow(max(b.fres, 1e-6), mix(3.9, 5.4, paper)) * b.m * wrap * rimK * envRim;
  float outr = (b.rho - b.Rd) / 0.13;
  float pool = 0.55 + 0.55 * smoothstep(-0.25, 0.85, b.P.y);
  o.glow = exp(-outr * outr) * (1.0 - b.m) * pool * 0.14;
  return o;
}

// Premultiplied, with the containment as alpha: the view's own wash shows round
// the body, where Murmur composites onto a flat ink that would read as a disc.
vec4 mh_present(float body, float spec, float contact, float hue, vec2 uv, Pal pal, float glow) {
  float paper = pal.paper;
  float railE = body + (spec + contact) * (1.0 - paper);
  vec3 rgb = mh_lit(pal, railE, glow, 0.34, hue);
  if (paper > 0.002) {
    vec3 lit = fromLab(lch(min(pal.s0.x * 1.06 + 0.05, 1.02), 0.012, 0.9));
    rgb = mix(rgb, lit, smoothstep(0.34, 0.92, spec) * paper);
    float below = smoothstep(-0.10, 0.66, uv.y / MH_R);
    rgb = mix(rgb, s2l(uInk) * 0.55, clamp(contact * 2.60, 0.0, 1.0) * (0.06 + 1.05 * below) * paper);
  }
  float knee = mix(0.90, 0.96, paper);
  rgb = vec3(mh_knee(rgb.r, knee), mh_knee(rgb.g, knee), mh_knee(rgb.b, knee));
  vec3 c = l2s(rgb);
  float n = fract(52.9829189 * fract(dot(gl_FragCoord.xy, vec2(0.06711056, 0.00583715))));
  float tri = n < 0.5 ? (sqrt(2.0 * n) - 1.0) : (1.0 - sqrt(max(0.0, 2.0 - 2.0 * n)));
  c = clamp(c + tri / 255.0, 0.0, 1.0);
  // without the body the crystal is its own alpha: the lit faces opaque, the
  // ones in shadow smoked glass over whatever the composer floats on
  float cont = uCrystalOnly > 0.5 ? g_cov * clamp(0.42 + 1.25 * body, 0.0, 1.0) : mh_containment(uv, 0.72);
  return vec4(c * cont, cont);
}

// ---- the crystal ------------------------------------------------------------------
vec3 rotX(vec3 p, float a) { float c = cos(a), s = sin(a); return vec3(p.x, c * p.y - s * p.z, s * p.y + c * p.z); }
vec3 rotY(vec3 p, float a) { float c = cos(a), s = sin(a); return vec3(c * p.x + s * p.z, p.y, -s * p.x + c * p.z); }
vec3 rotZ(vec3 p, float a) { float c = cos(a), s = sin(a); return vec3(c * p.x - s * p.y, s * p.x + c * p.y, p.z); }

// Into the crystal's frame, c axis = +z. Yaw is applied last, so it spins the
// stone about its own c axis whatever the lean.
vec3 toC(vec3 p, float roll, float pitch, float yaw) {
  vec3 q = rotY(rotX(rotZ(p, roll), pitch), yaw);
  return vec3(q.x, q.z, -q.y);
}

struct Hit { float tIn; float tOut; float tIn2; vec3 n; vec3 n2; };

// One half-space n.p <= o against the ray: geode's slab test generalised, since
// a quartz point has faces that do not come in parallel pairs.
void cut(inout Hit H, vec3 n, float o, vec3 P, vec3 R) {
  float nr = dot(n, R);
  float np = dot(n, P);
  if (abs(nr) < 1e-6) { if (np > o) { H.tIn = 1e9; H.tOut = -1e9; } return; }
  float s = (o - np) / nr;
  if (nr < 0.0) {
    if (s > H.tIn) { H.tIn2 = H.tIn; H.n2 = H.n; H.tIn = s; H.n = n; }
    else if (s > H.tIn2) { H.tIn2 = s; H.n2 = n; }
  } else {
    H.tOut = min(H.tOut, s);
  }
}

struct Q { float a; float hT; float hB; float rz; };

// Plane i of a quartz point: 0-5 the prism m, 6-11 the upper rhombohedra, 12-17
// the lower. r and z alternate at the same angle and z takes the larger offset,
// so it cuts less and comes out the smaller face, as on a real crystal. The lower
// large faces sit 60 deg round from the upper ones.
void qPlane(Q q, int i, out vec3 n, out float o) {
  int f = i - 6 * (i / 6);
  float th = float(f) * (PI / 3.0);
  vec2 d = vec2(cos(th), sin(th));
  float odd = float(f - 2 * (f / 2));
  if (i < 6) { n = vec3(d, 0.0); o = q.a * (1.0 + 0.10 * uJit[f]); }
  else if (i < 12) { n = vec3(d * SIN_RHO, COS_RHO); o = q.a * SIN_RHO + q.hT * COS_RHO + q.rz * odd; }
  else { n = vec3(d * SIN_RHO, -COS_RHO); o = q.a * SIN_RHO + q.hB * COS_RHO + q.rz * (1.0 - odd); }
}

Hit qHit(Q q, vec3 P, vec3 R) {
  Hit H; H.tIn = -1e9; H.tOut = 1e9; H.tIn2 = -1e9; H.n = vec3(0.0, 0.0, 1.0); H.n2 = H.n;
  for (int i = 0; i < 18; i++) {
    vec3 n; float o;
    qPlane(q, i, n, o);
    cut(H, n, o, P, R);
  }
  return H;
}

float qSD(Q q, vec3 p) {
  float m = -1e9;
  for (int i = 0; i < 18; i++) {
    vec3 n; float o;
    qPlane(q, i, n, o);
    m = max(m, dot(n, p) - o);
  }
  return m;
}

// ---- the lattice: what thinking looks like ---------------------------------------------
// Nearest site of a triangular lattice stacked straight along c. Rounding in the
// skew frame is not always the true nearest; the taps are dense enough that a
// neighbouring tap picks it up.
vec3 siteNear(vec3 p, float D, out ivec3 id) {
  float j = round(p.y / (0.8660254 * D));
  float i = round(p.x / D - 0.5 * j);
  float l = round(p.z / D);
  id = ivec3(int(i), int(j), int(l));
  return vec3((i + 0.5 * j) * D, j * 0.8660254 * D, l * D);
}

float rayDist2(vec3 P, vec3 R, vec3 X, out float along) {
  vec3 q = X - P;
  along = dot(q, R);
  vec3 perp = q - along * R;
  return dot(perp, perp);
}

float segRay2(vec3 P, vec3 R, vec3 A, vec3 B, out float along) {
  vec3 u = B - A, w0 = A - P;
  float a = dot(u, u), bb = dot(u, R), dd = dot(u, w0), e = dot(R, w0);
  float den = a - bb * bb;
  float tau = den > 1e-7 ? clamp((bb * e - dd) / den, 0.0, 1.0) : 0.0;
  along = e + tau * bb;
  vec3 x = w0 + tau * u - along * R;
  return dot(x, x);
}

// While thinking every site holds a floor and nucleates in travelling waves on
// its own phase; the success ignition lights them all at once.
float siteLight(vec3 X, ivec3 id, float t) {
  float h = float(mh_hash(uvec3(id + 1024)) >> 8) * (1.0 / 16777216.0);
  float wave = sin(t * 2.4 - dot(X, vec3(5.0, 3.5, 7.0)) + h * 6.2831853);
  float think = smoothstep(0.35, 1.0, wave) * (0.55 + 0.45 * h);
  return uThink * (0.42 + think) + g_st.complete * 0.9;
}

float lattice(Q q, vec3 P, vec3 R, float s0, float s1, float t, float px) {
  const float D = 0.115;
  float sig = max(0.024, 1.6 * px);
  float sb = sig * 0.55;
  float node = 0.0, bond = 0.0;
  vec3 dirs[6] = vec3[6](vec3(D, 0.0, 0.0), vec3(-D, 0.0, 0.0),
                         vec3(0.5 * D, 0.8660254 * D, 0.0), vec3(-0.5 * D, -0.8660254 * D, 0.0),
                         vec3(0.0, 0.0, D), vec3(0.0, 0.0, -D));
  for (int i = 0; i < 12; i++) {
    float s = mix(s0, s1, (float(i) + 0.5) / 12.0);
    ivec3 id;
    vec3 X = siteNear(P + R * s, D, id);
    if (qSD(q, X) > -0.015) continue;
    float al;
    float r2 = rayDist2(P, R, X, al);
    float a = siteLight(X, id, t);
    node = max(node, exp(-r2 / (sig * sig)) * a * exp(-MH_EXT * max(al, 0.0)));
    // bonds belong to thinking alone: the success ignition lights the sites only
    if (uThink < 0.001) continue;
    for (int k = 0; k < 6; k++) {
      vec3 Y = X + dirs[k];
      if (qSD(q, Y) > -0.015) continue;
      float bl;
      float d2 = segRay2(P, R, X, Y, bl);
      bond = max(bond, exp(-d2 / (sb * sb)) * (0.20 + 0.30 * a) * uThink * exp(-MH_EXT * max(bl, 0.0)));
    }
  }
  return node + bond * 0.8;
}

// ---- the spine: the crystal at the run line's size -----------------------------------
// Under ~40 px the body is 20 device px across, and ADR-0048 measured that neither
// the faces nor the lattice read there. What a 20 px bead can still say is a line
// with two ends: the habit's skeleton, the c axis between its two terminations.
// It is the lattice reduced to its two farthest sites and the one bond between
// them, so thinking is the same travelling wave with one line to run along, and
// text arriving is a front climbing it, the prototype's responding on one axis.
// Weighted by small at the call site, so the landing never draws it.
float spine(Q q, vec3 P, vec3 R, float t, float px) {
  // the ends sit short of the terminations: the tips are where mh_inside fades
  float zHi = 0.80 * (q.hT + q.a * TAN_RHO), zLo = -0.80 * (q.hB + q.a * TAN_RHO);
  vec3 A = vec3(0.0, 0.0, zLo), B = vec3(0.0, 0.0, zHi);
  // two device pixels wide whatever the ratio: the lattice's width is under a
  // pixel at 1 dpr, which is the whole reason the lattice does not read here
  float sig = max(0.08, 1.9 * px);
  float al;
  float d2 = segRay2(P, R, A, B, al);
  vec3 X = P + R * al;
  float u = clamp((X.z - zLo) / (zHi - zLo), 0.0, 1.0);
  float wave = smoothstep(0.30, 1.0, sin(t * 2.4 - u * 7.5));
  // what has been said stays lit and the front is brightest
  float front = exp(-pow((u - fract(t * 0.55)) / 0.12, 2.0));
  float bondLit = 0.22 + 0.85 * uThink * wave + uRespond * (0.45 + 0.90 * front);
  float bond = exp(-d2 / (sig * sig)) * bondLit;
  float ta, tb;
  float ra = rayDist2(P, R, A, ta), rb = rayDist2(P, R, B, tb);
  float sigT = sig * 1.35;
  float tipLit = 0.55 + 0.45 * uThink * (0.5 + 0.5 * sin(t * 2.4)) + 0.5 * uRespond;
  float tips = (exp(-ra / (sigT * sigT)) + exp(-rb / (sigT * sigT))) * tipLit;
  return (bond + tips) * mh_inside(X) * exp(-MH_EXT * max(al, 0.0));
}

void main() {
  float minR = min(uRes.x, uRes.y);
  // Murmur's uv: centred, y down, unit = the short side
  vec2 uv = vec2(gl_FragCoord.x - 0.5 * uRes.x, 0.5 * uRes.y - gl_FragCoord.y) / minR;
  float t = uTime;
  float small = 1.0 - smoothstep(16.0, 88.0, uSizePt);
  float big = 1.0 - small;
  // The composers draw the crystal alone (ADR-0048, amendment 2026-10-06). With
  // no body to frame it, it is framed by its own length: every vault's seed puts
  // its farthest tip at 0.40 of the box, so no seed grows into the corners and
  // none shrinks to a speck. k is the same expression as below.
  bool bare = uCrystalOnly > 0.5;
  float kB = 1.40 * mix(1.0, 1.35, 1.0 - smoothstep(16.0, 88.0, uSizePt));
  float bareR = 0.40 / ((max(uSeedB.x, uSeedB.y) + 0.17 * TAN_RHO) * kB);
  float px = 1.0 / (minR * (bare ? bareR : MH_R));
  g_st = mh_state(uStateIndex, uStateTau);
  g_cov = 0.0;

  float k = 1.40 * mix(1.0, 1.35, small);
  float sc = 0.4 * k;

  Body b;
  vec3 rd;
  float L;
  if (bare) {
    // nothing to refract through: straight rays from a plane in front of it
    vec2 s = uv / bareR;
    b.m = 1.0; b.rho = length(s); b.P = vec3(s, 1.0); b.N = vec3(0.0, 0.0, 1.0); b.Rd = 1.0; b.fres = 0.0;
    rd = vec3(0.0, 0.0, -1.0);
    L = 2.0;
  } else {
    b = mh_body(uv, t, px, 0.021 + 0.007 * mh_breath(t, 11.7), 1.22);
    rd = mh_refract(vec3(0.0, 0.0, -1.0), b.N, MH_ETA);
    L = mh_exit(b.P, rd);
  }
  vec4 fl = mh_flourish(t, 19.0, 10.2);

  float roll = uSeedA.x + 0.05 * sin(t * 0.033) + uSway;
  float pitch = uSeedA.y + 0.08 * sin(t * 0.041);
  float yaw = uSeedA.z + uYaw + 0.19 * sin(0.2196 * t + 3.42);
  // the crystal moves inside a glass that stays put, so only the ray's origin
  // shifts: the body, its rim and its catchlight are not translated with it
  vec3 Pc = toC(b.P - vec3(0.0, uBob, 0.0), roll, pitch, yaw);
  vec3 Rc = toC(rd, roll, pitch, yaw);
  vec3 keyC = toC(mh_key(t), roll, pitch, yaw);

  Q q; q.a = 0.17 * k; q.hT = uSeedB.x * k; q.hB = uSeedB.y * k; q.rz = uSeedA.w * k;
  Hit H = qHit(q, Pc, Rc);

  float E = 0.0, Hn = 0.0;
  float sEnter = max(H.tIn, 0.0);
  float sExit = min(H.tOut, L);
  float chord = sExit - sEnter;
  if (H.tIn < H.tOut && chord > 0.0 && sEnter < L) {
    float soft = (0.10 - 0.055 * FACET) * sc;
    float eMix = exp(-max(H.tIn - H.tIn2, 0.0) / max(soft, 1e-4));
    vec3 nrm = normalize(mix(H.n, H.n2, 0.5 * eMix) + 1e-5);
    float face = pow(clamp(dot(nrm, keyC), 0.0, 1.0), 1.4 + 2.6 * FACET);
    // alone, the faces turned from the key still have to read as crystal: at
    // 0.08 they fell to the ink and left the lit tip floating by itself
    float lit = (bare ? 0.30 : 0.08) + face;
    // real quartz prism faces are striated across c: a faint rhythm on m faces only,
    // faded out before it is finer than a pixel
    vec3 Xe = Pc + Rc * sEnter;
    float prismFace = 1.0 - smoothstep(0.02, 0.10, abs(H.n.z));
    float stri = sin(Xe.z * 150.0 + 2.0 * mh_noise3(Xe * 12.0));
    lit *= 1.0 + 0.07 * stri * prismFace * (1.0 - eMix) * smoothstep(0.012, 0.004, px);
    if (fl.x > 0.002) lit += fl.x * 1.70 * pow(clamp(dot(nrm, normalize(vec3(0.5, 0.2, 0.84))), 0.0, 1.0), 3.0);
    lit += g_st.complete * 0.70;
    float body = smoothstep(0.0, 0.34 * sc, chord);
    float vis = (bare ? 1.0 : mh_inside(b.P + rd * (sEnter + chord * 0.4))) * exp(-MH_EXT * sEnter);
    g_cov = smoothstep(0.0, 2.5 * px, chord);
    float edge = GLIMMER * big * 0.85 * eMix * (1.0 - eMix * 0.4);
    // thinking: the faces give way to what is inside them, the lattice at the
    // landing and the spine at the run line; at the run line they also sit back
    // at rest, or a 20 px bead is one bright blob with a line lost in it
    float lat = uThink;
    float cE = (lit * body * (1.0 - 0.75 * lat) + edge * body * (1.0 - 0.65 * lat)) * vis
             * mix(1.0, 0.45, small);
    E += cE;
    Hn += cE * clamp(nrm.x * 0.7 + nrm.y * 0.5, -1.0, 1.0) * SPREAD * MH_SPREAD;

    // the phantom, an earlier surface of the same crystal left inside it
    float ghost = big * (1.0 - uThink);
    if (ghost > 0.001) {
      float ps = uSeedB.z;
      Hit Hp = qHit(q, (Pc - vec3(0.0, 0.0, uSeedB.w * k)) / ps, Rc / ps);
      if (Hp.tIn < Hp.tOut && Hp.tIn > sEnter && Hp.tIn < sExit) {
        float em = exp(-max(Hp.tIn - Hp.tIn2, 0.0) / max(soft, 1e-4));
        float fp = pow(clamp(dot(Hp.n, keyC), 0.0, 1.0), 2.0);
        E += (0.06 + 0.20 * fp + 0.50 * em * (1.0 - em * 0.4)) * exp(-MH_EXT * Hp.tIn) * 0.6 * ghost;
      }
    }

    // at composer size a site is under a pixel, so the lattice is skipped there
    if (big > 0.01 && uThink + g_st.complete > 0.001) {
      E += lattice(q, Pc, Rc, sEnter, sExit, t, px) * big;
    }

    // success: one ring of light climbs the faces, tip to tip
    if (g_st.sweep > 0.0 && g_st.sweep < 1.0) {
      float zLo = -(q.hB + q.a * TAN_RHO), zHi = q.hT + q.a * TAN_RHO;
      float dzs = (Xe.z - mix(zLo, zHi, g_st.sweep)) / 0.03;
      E += 0.9 * sin(PI * g_st.sweep) * exp(-dzs * dzs) * body * vis;
    }
  }

  if (small > 0.01) E += spine(q, Pc, Rc, t, px) * small;

  float medAmt = mix(0.048, 0.028, small);
  float acc = 0.0;
  float trans = 1.0;
  float ds = L / 5.0;
  // the haze fills the body, so without one there is nothing to march
  for (int i = 0; i < (bare ? 0 : 5); i++) {
    vec3 p = b.P + rd * ((float(i) + 0.5) * ds);
    if (mh_inside(p) <= 0.001) continue;
    float e = mh_medium(p, t, 2.0) * medAmt;
    acc += e * trans * ds;
    trans *= exp(-(2.00 * e + MH_EXT) * ds);
  }

  float interior = (acc * 3.20 + E * 0.92) * b.m * mh_transmit(b.fres) * (1.0 + 0.22 * g_st.settled);
  Pal pal = mh_palette(uInk, uTone, uDepth);
  Surf sf = mh_surface(b, t, small, pal.paper);
  // rim, catchlight and halo are all the glass's
  if (bare) { sf.rim = 0.0; sf.spec = 0.0; sf.glow = 0.0; }
  sf.spec *= uSpark;
  float e = interior + sf.rim + sf.spec + sf.glow;
  fragColor = mh_present(interior + sf.rim, sf.spec, sf.glow, Hn * 0.92 / max(e, 1e-4), uv, pal, uGlow);
}`;

// One stone, mounted on any canvas: the landing's at 150-220 px and the
// composers' at 36-42 px are the same shader, which picks the interior by size.
// The composers pass crystalOnly: the landing keeps its glass body.
function mountPresence(canvas, { crystalOnly = false } = {}) {
  if (!canvas) return null;
  // Without WebGL2 (a GPU blocklist, a remote desktop) the canvas goes and the
  // page is what it was before the presence existed: the landing is the wordmark
  // and the sentence, the composer is its controls.
  const gl = canvas.getContext("webgl2", { premultipliedAlpha: true, antialias: false });
  if (!gl) { canvas.remove(); return null; }
  const compile = (type, src) => {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) console.warn("[presence]", gl.getShaderInfoLog(s));
    return s;
  };
  const prog = gl.createProgram();
  gl.attachShader(prog, compile(gl.VERTEX_SHADER, "#version 300 es\nin vec2 p;\nvoid main() { gl_Position = vec4(p, 0.0, 1.0); }"));
  gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, PRESENCE_FRAG));
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    console.warn("[presence]", gl.getProgramInfoLog(prog));
    canvas.remove();
    return null;
  }
  gl.useProgram(prog);
  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const attr = gl.getAttribLocation(prog, "p");
  gl.enableVertexAttribArray(attr);
  gl.vertexAttribPointer(attr, 2, gl.FLOAT, false, 0, 0);
  const u = {};
  for (const n of ["uRes", "uSizePt", "uTime", "uYaw", "uInk", "uTone", "uDepth", "uGlow",
    "uStateIndex", "uStateTau", "uThink", "uRespond", "uCrystalOnly", "uSeedA", "uSeedB", "uJit",
    "uSway", "uBob", "uSpark"]) u[n] = gl.getUniformLocation(prog, n);
  // A lost context is not worth a rebuild for a landing ornament: hide the canvas
  // and the landing reads as it did without one.
  canvas.addEventListener("webglcontextlost", () => { running = false; canvas.hidden = true; });

  // The seeds are Murmur's; the index is the shader's contract, success = 4.
  // `drain` is how far the tone walks to grey: the prototype's error walked the
  // hue and turned the stone into an emerald on the way. `hold` is a floor under
  // the composer's speaking signal, so a state can keep the spine lit and the
  // front climbing without a text delta.
  const STATES = {
    idle:     { index: 0, speed: 0.30, glow: 0.65, depth: 0.75, drain: 0, hold: 0, entry: "" },
    thinking: { index: 2, speed: 1.15, glow: 1.20, depth: 1.25, drain: 0, hold: 0, entry: "wake" },
    // a tool is running: the model has decided, so the wave gives way to the
    // front, the same reading a streaming answer gets, at half its light
    acting:   { index: 2, speed: 0.80, glow: 1.05, depth: 1.10, drain: 0, hold: 0.5, entry: "" },
    success:  { index: 4, speed: 0.55, glow: 1.05, depth: 1.00, drain: 0, hold: 0, entry: "swell" },
    // nothing has arrived for STALL_S: slow and drained, never fractured, because
    // the run line's own sentence beside it says "slow, not gone"
    stalled:  { index: 0, speed: 0.12, glow: 0.50, depth: 0.90, drain: 0.7, hold: 0, entry: "" },
    // the turn failed: the stall taken to its end, grey and still. The
    // prototype's veined fracture is not drawn because at the composer's 36 px
    // the crystal is about 11 px across and the veins are sub-pixel, and the
    // failure card under the question already says what broke; like success it
    // is an arrival, and idle follows.
    error:    { index: 0, speed: 0.04, glow: 0.42, depth: 0.85, drain: 1.0, hold: 0, entry: "" },
  };
  // prefers-reduced-motion turns the motion off rather than shortening it: the
  // clock stands still, a state is a cut, and a frame is drawn only on a change.
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const ease = (x) => { x = Math.min(Math.max(x, 0), 1); return x * x * (3 - 2 * x); };
  const cross = (tau) => (reduced ? 1 : ease(tau / 0.6));
  const speedBoost = (en, tau) => (en === "wake" && tau > 0 && tau < 2.5 ? 1 + 0.6 * Math.exp(-tau / 0.4) : 1);
  const glowBoost = (en, tau) => {
    if (en !== "swell" || tau <= 0 || tau >= 1.5) return 1;
    const x = tau / 0.4;
    return 1 + 0.35 * x * x * Math.exp(2 * (1 - x)) * (1 - ease((tau - 1.1) / 0.4));
  };

  const seed = {};
  let seeded = false;
  function applySeed(str) {
    let h = 2166136261;
    for (const ch of str || "silica") { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); }
    let a = h >>> 0;
    const r = () => { // mulberry32
      a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
    seed.A = [(r() - 0.5) * 0.6, 0.22 + r() * 0.22, r() * 6.283, 0.015 + r() * 0.05];
    seed.B = [0.20 + r() * 0.08, 0.14 + r() * 0.08, 0.50 + r() * 0.18, (r() - 0.5) * 0.10];
    seed.jit = Array.from({ length: 6 }, () => (r() - 0.5) * 2);
  }

  const probe = document.createElement("span");
  probe.hidden = true;
  document.body.appendChild(probe);
  const rgb = (css) => { const m = css.match(/[\d.]+/g) || [0, 0, 0]; return [m[0] / 255, m[1] / 255, m[2] / 255]; };
  const colors = {};
  // The tone is the live accent; the ink is whatever opaque surface the landing
  // sits on, read rather than named because the view's plane is a theme decision.
  function readColors() {
    probe.style.color = "var(--accent)";
    colors.tone = rgb(getComputedStyle(probe).color);
    colors.ink = [0, 0, 0];
    for (let n = canvas.parentElement; n; n = n.parentElement) {
      const c = getComputedStyle(n).backgroundColor;
      const m = c.match(/[\d.]+/g);
      if (m && (m.length < 4 || +m[3] > 0.5)) { colors.ink = rgb(c); break; }
    }
  }

  // The orb floats (owner, 2026-10-13): 10 degrees of roll either side over 16 s
  // and a drop of 0.05 body radii over 11 s. Both run on `drift`, wall-clock
  // seconds, and not on `phase`, which idles at 0.30 and would stretch the sway
  // to 53 s and change its pace at every state. 16 and 11 share no multiple
  // under 176 s, so the two never fall into one visible beat. The composers'
  // stone does not float: it is framed by its own length at 0.40 of a 36 px
  // box, and a drop would walk its tips into the box's edge.
  const SWAY = (10 * Math.PI) / 180, SWAY_S = 16, BOB = 0.05, BOB_S = 11;
  // The catchlight breathes between 0.24 and 1.0 of its strength on two
  // periods with no common beat, so it glints rather than blinks like a beacon.
  const sparkAt = (s) => 0.62 + 0.38 * (0.65 * Math.sin((2 * Math.PI * s) / 6.1)
    + 0.35 * Math.sin((2 * Math.PI * s) / 3.7 + 1.7));
  let cur = "idle", prev = "idle", changedAt = performance.now() / 1000, back = 0;
  let phase = 0, yaw = 0, act = 0, typing = 0, speak = 0, last = changedAt, drift = 0;
  let running = false, raf = 0, dirty = true, sizePt = 0;

  function frame(ms) {
    raf = 0;
    if (!running) return;
    const now = ms / 1000;
    const dt = Math.min(Math.max(now - last, 0), 0.25);
    last = now;
    const tau = now - changedAt;
    const e = cross(tau);
    const from = STATES[prev], to = STATES[cur];
    const lerp = (key) => from[key] + (to[key] - from[key]) * e;
    // typing cadence, Murmur's `activity`: each keystroke adds, the sum decays
    typing *= Math.exp(-dt / 0.6);
    act += (typing - act) * (1 - Math.exp(-dt / (typing > act ? 0.05 : 0.25)));
    // text arriving, the same shape: each delta adds, the sum decays, so a
    // stream reads as one held light and a pause lets it fall back
    speak *= Math.exp(-dt / 0.8);
    // the drain walks the accent toward a grey of its own lightness
    const dr = lerp("drain");
    const lum = 0.2126 * colors.tone[0] + 0.7152 * colors.tone[1] + 0.0722 * colors.tone[2];
    const tone = colors.tone.map((c) => c + (lum - c) * dr);
    const tempo = lerp("speed") * speedBoost(to.entry, tau);
    // Integrated here, not rate * t in the shader: geode multiplies its spin rate
    // onto the whole elapsed phase, so a change of pace re-scales every past
    // second and the stone lurches.
    if (!reduced) {
      phase += dt * tempo * (1 + 0.25 * act);
      yaw += dt * tempo * 0.088 * (1 + 0.8 * Math.pow(act, 0.85) * 0.6);
      drift += dt;
    }
    const floats = crystalOnly ? 0 : 1;
    if (seeded && sizePt && (dirty || !reduced)) {
      dirty = false;
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.uniform2f(u.uRes, canvas.width, canvas.height);
      gl.uniform1f(u.uSizePt, sizePt);
      gl.uniform1f(u.uTime, phase);
      gl.uniform1f(u.uYaw, yaw);
      gl.uniform3fv(u.uInk, colors.ink);
      gl.uniform3fv(u.uTone, tone);
      gl.uniform1f(u.uDepth, lerp("depth"));
      gl.uniform1f(u.uGlow, lerp("glow") * glowBoost(to.entry, tau));
      gl.uniform1f(u.uStateIndex, to.index);
      gl.uniform1f(u.uStateTau, reduced ? 4 : Math.max(tau, 0));
      gl.uniform1f(u.uThink, (prev === "thinking" ? 1 - e : 0) + (cur === "thinking" ? e : 0));
      gl.uniform1f(u.uCrystalOnly, crystalOnly ? 1 : 0);
      gl.uniform1f(u.uRespond, Math.min(Math.max(speak, lerp("hold")), 1));
      gl.uniform4fv(u.uSeedA, seed.A);
      gl.uniform4fv(u.uSeedB, seed.B);
      gl.uniform1fv(u.uJit, seed.jit);
      gl.uniform1f(u.uSway, floats * SWAY * Math.sin((2 * Math.PI * drift) / SWAY_S));
      gl.uniform1f(u.uBob, floats * BOB * Math.sin((2 * Math.PI * drift) / BOB_S + 1.0));
      gl.uniform1f(u.uSpark, sparkAt(drift));
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }
    if (!reduced) raf = requestAnimationFrame(frame);
  }
  const kick = () => { dirty = true; if (running && !raf) raf = requestAnimationFrame(frame); };

  // The backing store follows the CSS box. Measured from both observers, not the
  // resize one alone: the composer's stone is display:none while the landing
  // shows, and in Chrome that first display:none -> shown transition reached the
  // intersection observer and not the resize one, so the stone drew 300x150
  // into a 32px box.
  function fit() {
    // offsetWidth, not the client rect: the dock's stone is drawn scaled while
    // its composer is open (app-panels.css), and a rect taken then would size
    // the backing store for 36 px and blur the 42 px stone once it is back at rest
    const w = canvas.offsetWidth;
    if (!w) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const px = Math.round(w * dpr);
    if (canvas.width !== px) canvas.width = canvas.height = px;
    sizePt = w;
  }
  // Frames only while the canvas is on screen: the hero while the transcript is
  // empty, the composer's stone once it has something in it and its view shows.
  // Both are display:none otherwise, which the observer reports.
  new IntersectionObserver(([entry]) => {
    running = entry.isIntersecting && !canvas.hidden;
    last = performance.now() / 1000;
    fit();
    kick();
  }).observe(canvas);
  new ResizeObserver(() => { fit(); kick(); }).observe(canvas);
  new MutationObserver(() => { readColors(); kick(); })
    .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  // The stone listens to the field it sits beside: the dock's to the dock's
  // textarea, the landing's to the chat composer it looks down on.
  const field = canvas.closest(".composer-shell")?.querySelector("textarea") || document.getElementById("input");
  field?.addEventListener("input", () => { typing = Math.min(1, typing + 0.18); kick(); });

  readColors();
  applySeed("");
  // The path arrives with /vault_info; drawing before it would show a stone that
  // changes shape a moment later. A failed /vault_info still gets the default one.
  setTimeout(() => { if (!seeded) { seeded = true; kick(); } }, 1500);

  return {
    state(name) {
      if (!STATES[name] || name === cur) return;
      prev = cur; cur = name;
      changedAt = performance.now() / 1000;
      clearTimeout(back);
      // success and error are arrivals, not places to stay; error holds longer
      // because grey and still is a quieter reading than the swell
      if (name === "success") back = setTimeout(() => this.state("idle"), 1600);
      if (name === "error") back = setTimeout(() => this.state("idle"), 2600);
      kick();
    },
    seed(path) {
      applySeed(path);
      seeded = true;
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

// The composer's stone (ADR-0048, second site), in both composers: the chat's
// and the dock's. One handle fans out, so work.js and app-chat.js report a turn
// without knowing how many places watch it.
const composerPresence = (() => {
  const all = [...document.querySelectorAll(".composer-shell .cp-presence")]
    .map((c) => mountPresence(c, { crystalOnly: true })).filter(Boolean);
  return {
    state(name) { for (const p of all) p.state(name); },
    seed(path) { for (const p of all) p.seed(path); },
    spoke() { for (const p of all) p.spoke(); },
  };
})();
