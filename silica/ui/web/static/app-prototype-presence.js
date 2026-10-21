// PROTOTYPE, kept in main as the record of ADR-0048's choice and not maintained.
// A won, with C's thinking, and shipped as the orb until 2026-10-21, when the
// lockup's own mark took its place (app-presence.js); B and C and the
// responding and error states live only here.
//
// Question: does a Murmur-style glass presence with a crystalline interior make
// the empty chat recognisably Silica? Three interiors on the real landing,
// switched with ?presence=A|B|C (or the arrows in the black bar, or the arrow
// keys). Nothing happens without the param.
//
//   A  Quartz point  one doubly terminated crystal, phantom inside; while it
//                    thinks the faces give way to C's lattice (the chosen one)
//   B  Druse         four single points growing from one base, tips nucleate
//   C  Lattice       the same habit drawn by its sites and bonds alone
//
// The glass body, palette, surface and the slab-method solid are ported from
// krispuckett/murmur (MIT), Sources/Murmur/Shaders/MurmurGlass.metal: the kit,
// mh_body, mh_surface, mh_present and mh_geode. The state seeds, entries and the
// activity envelope are MurmurState.swift and MurmurSignals.swift.
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
(function () {
  const want = new URLSearchParams(location.search).get("presence");
  if (!want) return;

  const VARIANTS = [["A", "Quartz point"], ["B", "Druse"], ["C", "Lattice"]];
  let vi = Math.max(0, VARIANTS.findIndex((v) => v[0] === want.toUpperCase()));

  const VERT = `#version 300 es
in vec2 p;
void main() { gl_Position = vec4(p, 0.0, 1.0); }`;

  const FRAG = `#version 300 es
precision highp float;
precision highp int;

uniform vec2 uRes;
uniform float uSizePt;
uniform float uTime;
uniform float uYaw;
uniform float uFront;
uniform vec3 uInk;
uniform vec3 uTone;
uniform vec3 uTone2;
uniform float uHueShift;
uniform float uDepth;
uniform float uGlow;
uniform vec4 uChar;
uniform float uStateIndex;
uniform float uStateTau;
uniform float uThink;
uniform float uResp;
uniform float uErr;
uniform int uVariant;
uniform vec4 uSeedA;
uniform vec4 uSeedB;
uniform vec4 uSeedC;
uniform float uJit[6];
uniform vec4 uDruse[4];

out vec4 fragColor;

const float PI = 3.14159265;
const float MH_R = 0.300;
const float MH_ETA = 1.0 / 1.20;
const float MH_EXT = 0.55;
const float MH_SPREAD = 0.50;
// quartz r {10-11}: the face normal sits 51.8 deg off the c axis
const float SIN_RHO = 0.7859;
const float COS_RHO = 0.6184;
const float TAN_RHO = 1.2708;

// ---- the kit ------------------------------------------------------------------
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

struct Pal { vec3 s0; vec3 s1; vec3 s2; vec3 s3; float paper; float duo; float dHue; float dC; float dL; };

// Murmur walks the shadow -0.35 rad, "warm, toward ember", which is right for its
// amber and turns a teal toward green. Here it walks the other way, toward blue.
const float SHADOW_WALK = 0.35;

Pal mh_palette(vec3 inkC, vec3 toneC, vec3 tone2C, float hueShift, float depth) {
  vec3 ink = toLab(s2l(inkC));
  vec3 tone = toLab(s2l(toneC));
  float L = tone.x;
  float C = length(tone.yz);
  float h = atan(tone.z, tone.y) + hueShift;
  float d = clamp(depth, 0.30, 2.00);
  Pal p;
  p.paper = smoothstep(0.50, 0.72, ink.x);
  vec3 tone2 = toLab(s2l(tone2C));
  float C2 = length(tone2.yz);
  float dh = atan(tone2.z, tone2.y) - atan(tone.z, tone.y);
  p.dHue = dh - 6.2831853 * floor(dh / 6.2831853 + 0.5);
  p.dC = C2 / max(C, 1e-4);
  p.dL = tone2.x / max(tone.x, 1e-4);
  p.duo = smoothstep(0.004, 0.035, length(tone2 - tone));
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
  float a = hue * (1.0 / MH_SPREAD);
  float pos = max(a, 0.0), neg = max(-a, 0.0);
  float rot = -neg * MH_SPREAD + pos * mix(MH_SPREAD, p.dHue, p.duo);
  float w = min(pos, 1.0) * p.duo;
  float cS = 1.0 + w * (p.dC - 1.0);
  float lS = 1.0 + w * (p.dL - 1.0);
  float ch = cos(rot), sh = sin(rot);
  lab.yz = vec2(lab.y * ch - lab.z * sh, lab.y * sh + lab.z * ch) * cS;
  lab.x *= lS;
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

struct St { float complete; float sweep; float settled; float drive; };
St g_st;

St mh_state(float si, float tau) {
  St o; o.complete = 0.0; o.sweep = 0.0; o.settled = 0.0; o.drive = 0.0;
  tau = max(tau, 0.0);
  if (si > 3.5 && si < 4.5) {
    float a = clamp(tau / 1.20, 0.0, 1.0);
    o.complete = smoothstep(0.0, 0.30, a) * (1.0 - smoothstep(0.36, 1.0, a));
    o.settled = smoothstep(0.30, 1.05, a);
    o.sweep = smoothstep(0.0, 1.0, clamp(tau / 0.95, 0.0, 1.0));
  } else if (si > 2.5 && si < 3.5) {
    o.drive = smoothstep(0.0, 0.55, tau);
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

// ---- the glass body ------------------------------------------------------------
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

Surf mh_surface(Body b, float t, float small, float paper, float rimK, float specK, float glowK) {
  Surf o;
  rimK *= mix(1.0, 1.32, paper);
  vec3 H = normalize(mh_key(t) + vec3(0.0, 0.0, 1.0));
  float nh = clamp(dot(b.N, H), 0.0, 1.0);
  float tight = mix(320.0, 16.0, small);
  o.spec = (pow(nh, tight) + 0.04 * pow(nh, 4.0)) * b.m * specK
         * mix(0.92, 1.08, 0.5 - 0.5 * clamp(b.N.y, -1.0, 1.0));
  float wrap = 0.55 + 0.45 * clamp(dot(normalize(b.N.xy + vec2(1e-4)), normalize(vec2(0.42, 0.50))), 0.0, 1.0);
  wrap = mix(wrap, 0.88, paper);
  float sky = 0.5 - 0.5 * clamp(b.N.y, -1.0, 1.0);
  float envRim = mix(mix(0.86, 1.14, sky), mix(1.14, 0.86, sky), paper);
  o.rim = pow(max(b.fres, 1e-6), mix(3.9, 5.4, paper)) * b.m * wrap * rimK * envRim;
  float outr = (b.rho - b.Rd) / 0.13;
  float pool = 0.55 + 0.55 * smoothstep(-0.25, 0.85, b.P.y);
  o.glow = exp(-outr * outr) * (1.0 - b.m) * pool * glowK;
  return o;
}

// Premultiplied, with the containment as alpha, so the page's own wash shows
// round the body instead of a disc of ink that only matches it on a flat fill.
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
  float cont = mh_containment(uv, 0.72);
  return vec4(c * cont, cont);
}

// ---- the crystal ----------------------------------------------------------------
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

Hit hitNone() { Hit H; H.tIn = -1e9; H.tOut = 1e9; H.tIn2 = -1e9; H.n = vec3(0.0, 0.0, 1.0); H.n2 = H.n; return H; }

// One half-space n.p <= o against the ray: geode's slab test generalised, so a
// solid can have faces that do not come in parallel pairs.
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

struct Q { float a; float hT; float hB; float rz; float lane; bool capped; };

float qJit(Q q, int f) { return q.lane < 0.0 ? uJit[f] : mh_hash1(float(f), q.lane) * 2.0 - 1.0; }

// Plane i of a quartz point: 0-5 the prism m, 6-11 the upper rhombohedra, 12-17
// the lower. r and z alternate at the same angle and z takes the larger offset,
// so it cuts less and comes out the smaller face, as on a real crystal. The lower
// large faces sit 60 deg round from the upper ones.
void qPlane(Q q, int i, out vec3 n, out float o) {
  int f = i - 6 * (i / 6);
  float th = float(f) * (PI / 3.0);
  vec2 d = vec2(cos(th), sin(th));
  float odd = float(f - 2 * (f / 2));
  if (i < 6) { n = vec3(d, 0.0); o = q.a * (1.0 + 0.10 * qJit(q, f)); }
  else if (i < 12) { n = vec3(d * SIN_RHO, COS_RHO); o = q.a * SIN_RHO + q.hT * COS_RHO + q.rz * odd; }
  else { n = vec3(d * SIN_RHO, -COS_RHO); o = q.a * SIN_RHO + q.hB * COS_RHO + q.rz * (1.0 - odd); }
}

Hit qHit(Q q, vec3 P, vec3 R) {
  Hit H = hitNone();
  int count = q.capped ? 12 : 18;
  for (int i = 0; i < 18; i++) {
    if (i >= count) break;
    vec3 n; float o;
    qPlane(q, i, n, o);
    cut(H, n, o, P, R);
  }
  if (q.capped) cut(H, vec3(0.0, 0.0, -1.0), 0.0, P, R);
  return H;
}

float qSD(Q q, vec3 p) {
  float m = q.capped ? -p.z : -1e9;
  int count = q.capped ? 12 : 18;
  for (int i = 0; i < 18; i++) {
    if (i >= count) break;
    vec3 n; float o;
    qPlane(q, i, n, o);
    m = max(m, dot(n, p) - o);
  }
  return m;
}

Q quartzA(float k) {
  Q q; q.a = 0.17 * k; q.hT = uSeedB.x * k; q.hB = uSeedB.y * k; q.rz = uSeedA.w * k; q.lane = -1.0; q.capped = false;
  return q;
}

Q druseQ(int j, float k) {
  vec4 D = uDruse[j];
  Q q; q.a = D.w * k; q.hT = D.z * k - q.a * TAN_RHO; q.hB = 0.0; q.rz = uSeedA.w * k * 0.6;
  q.lane = float(j) + 3.0; q.capped = true;
  return q;
}

mat3 druseBasis(int j) {
  vec4 D = uDruse[j];
  vec3 w = normalize(vec3(D.x, D.y, sqrt(max(1.0 - D.x * D.x - D.y * D.y, 0.05))));
  vec3 u = normalize(cross(w, abs(w.x) < 0.9 ? vec3(1.0, 0.0, 0.0) : vec3(0.0, 1.0, 0.0)));
  vec3 v = cross(w, u);
  float ph = float(j) * 1.3;
  vec3 u2 = cos(ph) * u + sin(ph) * v;
  return mat3(u2, cross(w, u2), w);
}

vec3 druseBase(int j, float k) {
  float a = float(j) * 2.4;
  float off = j > 0 ? 0.07 : 0.0;
  return vec3(off * cos(a), off * sin(a), -0.50) * k;
}

// ---- sites --------------------------------------------------------------------
// Nearest site of a triangular lattice stacked straight along c. Rounding in the
// skew frame is not always the true nearest; the taps are dense enough that a
// neighbouring tap picks it up.
vec3 siteNear(vec3 p, float D, float DC, out ivec3 id) {
  float j = round(p.y / (0.8660254 * D));
  float i = round(p.x / D - 0.5 * j);
  float l = round(p.z / DC);
  id = ivec3(int(i), int(j), int(l));
  return vec3((i + 0.5 * j) * D, j * 0.8660254 * D, l * DC);
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

float siteLight(vec3 X, ivec3 id, float t, float zLo, float zHi, vec3 nF, float oF) {
  float h = float(mh_hash(uvec3(id + 1024)) >> 8) * (1.0 / 16777216.0);
  // thinking: sites nucleate in travelling waves, each on its own phase
  float wave = sin(t * 2.4 - dot(X, vec3(5.0, 3.5, 7.0)) + h * 6.2831853);
  float think = smoothstep(0.35, 1.0, wave) * (0.55 + 0.45 * h);
  // responding: what has grown stays lit and the front itself is brightest
  float zf = mix(zLo, zHi, uFront);
  float dz = (X.z - zf) / 0.05;
  float grown = (1.0 - smoothstep(-0.02, 0.02, X.z - zf)) * 0.35 + exp(-dz * dz) * 0.9;
  float base = 0.42 * (uVariant == 2 ? 1.0 : uThink);
  float nearF = exp(-pow((dot(nF, X) - oF) / 0.06, 2.0));
  float flick = 0.5 + 0.5 * sin(t * 23.0 + h * 40.0);
  // In A the growth front is the striation ring alone: at this lattice density the
  // grown sites under it read as a ragged fringe, not as a front.
  float growK = uVariant == 2 ? 1.0 : 0.0;
  return base + uThink * think + uResp * grown * growK + g_st.complete * 0.9 + uErr * nearF * flick;
}

float lattice(Q q, vec3 P, vec3 R, float s0, float s1, float t, float px, float zLo, float zHi, vec3 nF, float oF) {
  float D = 0.115, DC = D;
  float sig = max(0.024, 1.6 * px);
  float sb = sig * 0.55;
  float node = 0.0, bond = 0.0;
  float latVis = uVariant == 2 ? 1.0 : uThink;
  bool bonds = latVis > 0.001;
  vec3 dirs[6] = vec3[6](vec3(D, 0.0, 0.0), vec3(-D, 0.0, 0.0),
                         vec3(0.5 * D, 0.8660254 * D, 0.0), vec3(-0.5 * D, -0.8660254 * D, 0.0),
                         vec3(0.0, 0.0, DC), vec3(0.0, 0.0, -DC));
  for (int i = 0; i < 12; i++) {
    float s = mix(s0, s1, (float(i) + 0.5) / 12.0);
    ivec3 id;
    vec3 X = siteNear(P + R * s, D, DC, id);
    if (qSD(q, X) > -0.015) continue;
    float al;
    float r2 = rayDist2(P, R, X, al);
    float a = siteLight(X, id, t, zLo, zHi, nF, oF);
    node = max(node, exp(-r2 / (sig * sig)) * a * exp(-MH_EXT * max(al, 0.0)));
    if (!bonds) continue;
    for (int k = 0; k < 6; k++) {
      vec3 Y = X + dirs[k];
      if (qSD(q, Y) > -0.015) continue;
      float broken = step((dot(nF, X) - oF) * (dot(nF, Y) - oF), 0.0);
      float bl;
      float d2 = segRay2(P, R, X, Y, bl);
      bond = max(bond, exp(-d2 / (sb * sb)) * (0.20 + 0.30 * a) * latVis * (1.0 - uErr * broken) * exp(-MH_EXT * max(bl, 0.0)));
    }
  }
  return node + bond * 0.8;
}

// A lit plane across the solid at height zf: the growth front, and the sweep of
// success. Face-on it is a faint slice, edge-on a bright line.
float sheet(vec3 P, vec3 R, float s0, float s1, float zf) {
  if (abs(R.z) < 1e-3) return 0.0;
  float sf = (zf - P.z) / R.z;
  if (sf <= s0 || sf >= s1) return 0.0;
  return min(1.0 / abs(R.z), 3.0) * exp(-MH_EXT * sf);
}

void main() {
  float minR = min(uRes.x, uRes.y);
  // Murmur's uv: centred, y down, unit = the short side
  vec2 uv = vec2(gl_FragCoord.x - 0.5 * uRes.x, 0.5 * uRes.y - gl_FragCoord.y) / minR;
  float t = uTime;
  float small = 1.0 - smoothstep(16.0, 88.0, uSizePt);
  float big = 1.0 - small;
  float px = 1.0 / (minR * MH_R);
  g_st = mh_state(uStateIndex, uStateTau);

  float facetK = uChar.x, glimK = uChar.y, spreadK = uChar.w;
  float k = (1.25 + 0.3 * uChar.z) * mix(1.0, 1.35, small);
  float kd = (0.9 + 0.3 * uChar.z) * mix(1.0, 1.3, small);
  float sc = 0.4 * k;

  Body b = mh_body(uv, t, px, 0.021 + 0.007 * mh_breath(t, 11.7), 1.22);
  vec3 rd = mh_refract(vec3(0.0, 0.0, -1.0), b.N, MH_ETA);
  float L = mh_exit(b.P, rd);
  vec4 fl = mh_flourish(t, 19.0, 10.2);

  float roll = uSeedA.x + 0.05 * sin(t * 0.033);
  float pitch = uSeedA.y + 0.08 * sin(t * 0.041);
  float yaw = uSeedA.z + uYaw + (1.0 - g_st.drive) * 0.19 * sin(0.2196 * t + 3.42);
  vec3 Pc = toC(b.P, roll, pitch, yaw);
  vec3 Rc = toC(rd, roll, pitch, yaw);
  vec3 keyC = toC(mh_key(t), roll, pitch, yaw);

  float E = 0.0, Hn = 0.0;

  Q q = quartzA(k);
  Hit H = hitNone();
  vec3 Pl = Pc, Rl = Rc, keyL = keyC;
  float zLo = -(q.hB + q.a * TAN_RHO), zHi = q.hT + q.a * TAN_RHO;
  if (uVariant == 1) {
    H.tIn = 1e9; H.tOut = -1e9;
    float best = 1e9;
    for (int j = 0; j < 4; j++) {
      Q qj = druseQ(j, kd);
      mat3 Bj = druseBasis(j);
      vec3 pl = (Pc - druseBase(j, kd)) * Bj;
      vec3 rl = Rc * Bj;
      Hit hj = qHit(qj, pl, rl);
      float se = max(hj.tIn, 0.0);
      if (hj.tIn < hj.tOut && hj.tOut > 0.0 && se < L && se < best) {
        best = se; H = hj; q = qj; Pl = pl; Rl = rl; keyL = keyC * Bj;
      }
    }
    zLo = 0.0; zHi = q.hT + q.a * TAN_RHO;
  } else {
    H = qHit(q, Pc, Rc);
  }

  float fa = uSeedC.x, fe = uSeedC.y;
  vec3 nF = vec3(sin(fe) * cos(fa), sin(fe) * sin(fa), cos(fe));
  float oF = 0.03 * k;

  float sEnter = max(H.tIn, 0.0);
  float sExit = min(H.tOut, L);
  float chord = sExit - sEnter;
  if (H.tIn < H.tOut && chord > 0.0 && sEnter < L) {
    float soft = (0.10 - 0.055 * facetK) * sc;
    float eMix = exp(-max(H.tIn - H.tIn2, 0.0) / max(soft, 1e-4));
    vec3 nrm = normalize(mix(H.n, H.n2, 0.5 * eMix) + 1e-5);
    float face = pow(clamp(dot(nrm, keyL), 0.0, 1.0), 1.4 + 2.6 * facetK);
    float lit = 0.08 + 1.0 * face;
    vec3 Xe = Pl + Rl * sEnter;
    float prismFace = 1.0 - smoothstep(0.02, 0.10, abs(H.n.z));
    float stri = sin(Xe.z * 150.0 + 2.0 * mh_noise3(Xe * 12.0));
    lit *= 1.0 + 0.07 * stri * prismFace * (1.0 - eMix) * smoothstep(0.012, 0.004, px);
    if (fl.x > 0.002) lit += fl.x * 1.70 * pow(clamp(dot(nrm, normalize(vec3(0.5, 0.2, 0.84))), 0.0, 1.0), 3.0);
    lit += g_st.complete * 0.70;
    float body = smoothstep(0.0, 0.34 * sc, chord);
    float vis = mh_inside(b.P + rd * (sEnter + chord * 0.4)) * exp(-MH_EXT * sEnter);
    float edge = glimK * big * 0.85 * eMix * (1.0 - eMix * 0.4);
    // the lattice variant keeps only a trace of the edges: its sites draw the habit
    float lat = uVariant == 2 ? 1.0 : (uVariant == 0 ? uThink * big : 0.0);
    float faceK = uVariant == 2 ? small : 1.0 - 0.75 * lat;
    float edgeK = 1.0 - 0.65 * lat;
    float cE = (lit * body * faceK + edge * body * edgeK) * vis;
    E += cE;
    Hn += cE * clamp(nrm.x * 0.7 + nrm.y * 0.5, -1.0, 1.0) * spreadK * MH_SPREAD;

    // A: the phantom, an earlier surface of the same crystal left inside it
    if (uVariant == 0 && big > 0.01) {
      float ps = uSeedB.z;
      vec3 off = vec3(0.0, 0.0, uSeedB.w * k);
      Hit Hp = qHit(q, (Pl - off) / ps, Rl / ps);
      if (Hp.tIn < Hp.tOut && Hp.tIn > sEnter && Hp.tIn < sExit) {
        float em = exp(-max(Hp.tIn - Hp.tIn2, 0.0) / max(soft, 1e-4));
        float fp = pow(clamp(dot(Hp.n, keyL), 0.0, 1.0), 2.0);
        E += (0.06 + 0.20 * fp + 0.50 * em * (1.0 - em * 0.4)) * exp(-MH_EXT * Hp.tIn) * big * 0.6 * (1.0 - uThink);
      }
    }

    if (uVariant != 1 && big > 0.01) {
      E += lattice(q, Pl, Rl, sEnter, sExit, t, px, zLo, zHi, nF, oF) * big;
    }

    if (uVariant != 2) {
      float edgeFade = smoothstep(0.0, 0.08, uFront) * (1.0 - smoothstep(0.85, 1.0, uFront));
      float zf = mix(zLo, zHi, uFront);
      float dzf = (Xe.z - zf) / max(0.016, 1.2 * px);
      E += uResp * edgeFade * (0.05 * sheet(Pl, Rl, sEnter, sExit, zf) + 0.9 * exp(-dzf * dzf) * body * vis);
    }
    if (g_st.sweep > 0.0 && g_st.sweep < 1.0) {
      float dzs = (Xe.z - mix(zLo, zHi, g_st.sweep)) / 0.03;
      E += 0.9 * sin(PI * g_st.sweep) * exp(-dzs * dzs) * body * vis;
    }

    // error: a fracture across the solid, veined, in the second tone
    if (uErr > 0.001) {
      float nr = dot(nF, Rl);
      if (abs(nr) > 1e-3) {
        float sf = (oF - dot(nF, Pl)) / nr;
        if (sf > sEnter && sf < sExit) {
          vec3 x = Pl + Rl * sf;
          float vein = smoothstep(0.12, 0.0, abs(mh_noise3(x * 8.0 + 3.1)));
          float crE = uErr * (0.25 + 0.9 * vein) * (0.55 + 0.45 * exp(-uStateTau * 1.2))
                    * min(1.0 / abs(nr), 3.0) * 0.35 * exp(-MH_EXT * sf);
          E += crE;
          Hn += crE * MH_SPREAD;
        }
      }
    }
  }

  // B: the tips nucleate in turn while it thinks, seen through the glass
  if (uVariant == 1 && uThink > 0.001) {
    for (int j = 0; j < 4; j++) {
      Q qj = druseQ(j, kd);
      mat3 Bj = druseBasis(j);
      vec3 tip = druseBase(j, kd) + Bj[2] * (qj.hT + qj.a * TAN_RHO);
      float al;
      float r2 = rayDist2(Pc, Rc, tip, al);
      if (al < 0.0 || al > L) continue;
      float pulse = pow(max(0.0, sin(t * 1.6 - float(j) * 1.7)), 6.0);
      float sg = mix(0.045, 0.09, small);
      E += uThink * pulse * 1.1 * exp(-r2 / (sg * sg)) * exp(-MH_EXT * al);
    }
  }

  float medAmt = mix(0.048, 0.028, small);
  float acc = 0.0;
  float trans = 1.0;
  float ds = L / 5.0;
  for (int i = 0; i < 5; i++) {
    vec3 p = b.P + rd * ((float(i) + 0.5) * ds);
    if (mh_inside(p) <= 0.001) continue;
    float e = mh_medium(p, t, 2.0) * medAmt;
    acc += e * trans * ds;
    trans *= exp(-(2.00 * e + MH_EXT) * ds);
  }

  float interior = (acc * 3.20 + E * 0.92) * b.m * mh_transmit(b.fres) * (1.0 + 0.22 * g_st.settled);
  Pal pal = mh_palette(uInk, uTone, uTone2, uHueShift, uDepth);
  Surf sf = mh_surface(b, t, small, pal.paper, 0.74, 0.62, 0.14);
  float e = interior + sf.rim + sf.spec + sf.glow;
  float hueMix = Hn * 0.92 / max(e, 1e-4);
  fragColor = mh_present(interior + sf.rim, sf.spec, sf.glow, hueMix, uv, pal, uGlow);
}`;

  // ---- states: MurmurState.swift seeds, entries and the clock ----------------------
  const STATES = {
    idle:       { i: 0, speed: 0.30, glow: 0.65, depth: 0.75, hue: 0, entry: "none" },
    thinking:   { i: 2, speed: 1.15, glow: 1.20, depth: 1.25, hue: 0, entry: "wake" },
    responding: { i: 3, speed: 1.45, glow: 1.30, depth: 1.25, hue: 0, entry: "none" },
    success:    { i: 4, speed: 0.55, glow: 1.05, depth: 1.00, hue: 0, entry: "swell" },
    error:      { i: 5, speed: 0.65, glow: 0.80, depth: 1.20, hue: 0, entry: "stutter" },
  };
  const NAMES = Object.keys(STATES);
  const ease = (x) => { x = Math.min(Math.max(x, 0), 1); return x * x * (3 - 2 * x); };
  const speedBoost = (en, tau) => (en === "wake" && tau > 0 && tau < 2.5 ? 1 + 0.6 * Math.exp(-tau / 0.4) : 1);
  function glowBoost(en, tau) {
    if (en !== "swell" || tau <= 0 || tau >= 1.5) return 1;
    const x = tau / 0.4;
    return 1 + 0.35 * x * x * Math.exp(2 * (1 - x)) * (1 - ease((tau - 1.1) / 0.4));
  }
  const arch = (tau, s, w) => { const u = (tau - s) / w; return u > 0 && u < 1 ? Math.sin(Math.PI * u) : 0; };
  const phaseOffset = (en, tau) =>
    en === "stutter" && tau > 0 && tau < 0.5 ? -(0.030 * arch(tau, 0.05, 0.20) + 0.018 * arch(tau, 0.28, 0.16)) : 0;

  // ---- seed: one crystal per vault ------------------------------------------------
  function prng(str) {
    let h = 2166136261;
    for (const ch of str) { h ^= ch.charCodeAt(0); h = Math.imul(h, 16777619); }
    let a = h >>> 0;
    return () => {
      a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  let seed = {};
  function applySeed(str) {
    const r = prng(str || "silica");
    seed.A = [(r() - 0.5) * 0.6, 0.22 + r() * 0.22, r() * 6.283, 0.015 + r() * 0.05]; // roll, pitch, yaw0, z-face inset
    seed.B = [0.20 + r() * 0.08, 0.14 + r() * 0.08, 0.50 + r() * 0.18, (r() - 0.5) * 0.10]; // tips, phantom scale, shift
    seed.C = [r() * 6.283, 0.35 + r() * 0.6, 0, 0]; // fracture azimuth and lean
    seed.jit = Array.from({ length: 6 }, () => (r() - 0.5) * 2);
    seed.druse = [];
    for (let j = 0; j < 4; j++) {
      const az = r() * 6.283, lean = j === 0 ? r() * 0.15 : 0.3 + r() * 0.45;
      seed.druse.push(Math.sin(lean) * Math.cos(az), Math.sin(lean) * Math.sin(az),
        j === 0 ? 0.92 : 0.55 + r() * 0.30, 0.10 + r() * 0.06);
    }
  }

  // ---- GL -------------------------------------------------------------------------
  const UNIFORMS = ["uRes", "uSizePt", "uTime", "uYaw", "uFront", "uInk", "uTone", "uTone2", "uHueShift",
    "uDepth", "uGlow", "uChar", "uStateIndex", "uStateTau", "uThink", "uResp", "uErr", "uVariant",
    "uSeedA", "uSeedB", "uSeedC", "uJit", "uDruse"];
  function presence(canvas, sizePt) {
    const gl = canvas.getContext("webgl2", { premultipliedAlpha: true, antialias: false });
    if (!gl) { console.warn("[presence] no webgl2"); return null; }
    const sh = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) console.error("[presence]", gl.getShaderInfoLog(s));
      return s;
    };
    const prog = gl.createProgram();
    gl.attachShader(prog, sh(gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) console.error("[presence]", gl.getProgramInfoLog(prog));
    gl.useProgram(prog);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(prog, "p");
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    const u = {};
    for (const n of UNIFORMS) u[n] = gl.getUniformLocation(prog, n);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(sizePt * dpr);
    canvas.height = Math.round(sizePt * dpr);
    return { gl, u, canvas, sizePt };
  }

  function draw(p, f) {
    const { gl, u } = p;
    gl.viewport(0, 0, p.canvas.width, p.canvas.height);
    gl.uniform2f(u.uRes, p.canvas.width, p.canvas.height);
    gl.uniform1f(u.uSizePt, p.sizePt);
    gl.uniform1f(u.uTime, f.time);
    gl.uniform1f(u.uYaw, f.yaw);
    gl.uniform1f(u.uFront, f.front);
    gl.uniform3fv(u.uInk, colors.ink);
    gl.uniform3fv(u.uTone, f.tone);
    gl.uniform3fv(u.uTone2, f.tone);
    gl.uniform1f(u.uHueShift, f.hue);
    gl.uniform1f(u.uDepth, f.depth);
    gl.uniform1f(u.uGlow, f.glow);
    gl.uniform4f(u.uChar, 0.5, 0.4, 0.5, 0.5); // geode's character defaults
    gl.uniform1f(u.uStateIndex, f.index);
    gl.uniform1f(u.uStateTau, f.tau);
    gl.uniform1f(u.uThink, f.think);
    gl.uniform1f(u.uResp, f.resp);
    gl.uniform1f(u.uErr, f.err);
    gl.uniform1i(u.uVariant, vi);
    gl.uniform4fv(u.uSeedA, seed.A);
    gl.uniform4fv(u.uSeedB, seed.B);
    gl.uniform4fv(u.uSeedC, seed.C);
    gl.uniform1fv(u.uJit, seed.jit);
    gl.uniform4fv(u.uDruse, seed.druse);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  // ---- colours from the live tokens ---------------------------------------------
  const probe = document.createElement("span");
  probe.style.display = "none";
  document.body.appendChild(probe);
  const rgb = (css) => { const m = css.match(/[\d.]+/g) || [0, 0, 0]; return [m[0] / 255, m[1] / 255, m[2] / 255]; };
  const token = (name) => { probe.style.color = `var(${name})`; return rgb(getComputedStyle(probe).color); };
  function ground(el) {
    for (let n = el; n; n = n.parentElement) {
      const c = getComputedStyle(n).backgroundColor;
      const m = c.match(/[\d.]+/g);
      if (m && (m.length < 4 || +m[3] > 0.5)) return rgb(c);
    }
    return token("--slate");
  }
  const colors = {};
  // Error walks the tone toward its own grey. A hue walk toward --warn crosses
  // green on the way from 189deg, and the stone read as an emerald, not a fault.
  const drain = (c, k) => { const y = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]; return c.map((v) => v + (y - v) * k); };
  function readColors() {
    colors.tone = token("--accent");
    colors.ink = ground(heroCanvas);
  }

  // ---- DOM ------------------------------------------------------------------------
  const hero = document.getElementById("vault-hero");
  if (!hero) return;
  // one presence per screen: while the prototype is on, it replaces the shipped one
  hero.querySelector(".vh-presence")?.remove();
  const heroCanvas = document.createElement("canvas");
  // The sphere is 60% of its canvas (MH_R 0.3); the rest is containment and the
  // contact glow, so the negative margins pull the wordmark back under the body.
  heroCanvas.style.cssText = "display:block;width:220px;height:220px;margin:-30px auto -18px;pointer-events:none";
  hero.insertBefore(heroCanvas, hero.firstChild);

  const bar = document.createElement("div");
  bar.id = "pp-bar";
  bar.innerHTML = `
    <button data-cyc="-1" title="previous variant (←)">‹</button>
    <b id="pp-var"></b>
    <button data-cyc="1" title="next variant (→)">›</button>
    <span class="pp-sep"></span>
    ${NAMES.map((n, i) => `<button data-state="${n}" title="${i + 1}">${n}</button>`).join("")}
    <button id="pp-demo" title="d">demo</button>
    <span class="pp-sep"></span>
    <label>activity <input id="pp-act" type="range" min="0" max="1" step="0.01" value="0"></label>
    <label>seed <input id="pp-seed" size="10" value="silica"></label>
    <button id="pp-rand">random</button>
    <canvas id="pp-mini" style="width:24px;height:24px" title="24px, the composer size"></canvas>
    <code id="pp-read"></code>`;
  const style = document.createElement("style");
  style.textContent = `
    #pp-bar { position: fixed; left: 50%; bottom: 10px; transform: translateX(-50%); z-index: 99999;
      display: flex; flex-wrap: wrap; gap: 6px; align-items: center; justify-content: center;
      max-width: calc(100vw - 24px); padding: 6px 12px; border-radius: 14px;
      background: #000; color: #fff; font: 12px/1.3 ui-monospace, monospace;
      box-shadow: 0 4px 18px rgb(0 0 0 / .45); }
    #pp-bar button { background: none; color: inherit; border: 1px solid #555; border-radius: 4px;
      padding: 2px 6px; font: inherit; cursor: pointer; }
    #pp-bar button.on { background: #fff; color: #000; }
    #pp-bar input { font: inherit; background: #111; color: #fff; border: 1px solid #555; }
    #pp-bar input[type=range] { width: 80px; vertical-align: middle; }
    #pp-bar .pp-sep { width: 1px; height: 16px; background: #444; }
    #pp-read { color: #9f9; min-width: 34ch; }`;
  document.head.appendChild(style);
  document.body.appendChild(bar);
  const $ = (s) => bar.querySelector(s);

  const big = presence(heroCanvas, 220);
  const mini = presence($("#pp-mini"), 24);
  if (!big) return;

  // ---- the clock ------------------------------------------------------------------
  let cur = "idle", prev = "idle", changedAt = performance.now() / 1000;
  let phase = 0, yaw = 0, front = 0, act = 0, last = changedAt;
  let demo = false, demoAt = 0, frameNo = 0, dirty = true;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  function setState(name) {
    if (name === cur) return;
    prev = cur; cur = name; changedAt = performance.now() / 1000; dirty = true;
    bar.querySelectorAll("[data-state]").forEach((b) => b.classList.toggle("on", b.dataset.state === cur));
  }
  function setVariant(i) {
    vi = (i + VARIANTS.length) % VARIANTS.length;
    const url = new URL(location.href);
    url.searchParams.set("presence", VARIANTS[vi][0]);
    history.replaceState(history.state, "", url);
    $("#pp-var").textContent = `${VARIANTS[vi][0]} · ${VARIANTS[vi][1]}`;
    dirty = true;
  }

  const DEMO = [["idle", 2.5], ["thinking", 3.5], ["responding", 5], ["success", 2.5], ["idle", 2], ["thinking", 2], ["error", 2.5]];
  const DEMO_LEN = DEMO.reduce((s, d) => s + d[1], 0);
  function demoStep(now) {
    let x = (now - demoAt) % DEMO_LEN;
    for (const [name, len] of DEMO) { if (x < len) { setState(name); break; } x -= len; }
    // a token stream: bursty, never quite still while it answers
    return cur === "responding" ? 0.45 + 0.35 * Math.sin(now * 2.3) * Math.sin(now * 0.7 + 1) : 0;
  }

  function frame(ms) {
    requestAnimationFrame(frame);
    const now = ms / 1000;
    const dt = Math.min(Math.max(now - last, 0), 0.25);
    last = now;
    const target = demo ? demoStep(now) : +$("#pp-act").value;
    const tau = now - changedAt;
    const e = ease(tau / 0.6);
    const from = STATES[prev], to = STATES[cur];
    const lerp = (key) => from[key] + (to[key] - from[key]) * e;
    const w = (name) => (prev === name ? 1 - e : 0) + (cur === name ? e : 0);
    act += (target - act) * (1 - Math.exp(-dt / (target > act ? 0.05 : 0.25)));
    const tempo = lerp("speed") * speedBoost(to.entry, tau);
    const quick = tempo * (1 + 0.25 * act);
    const working = cur === "thinking" || cur === "responding" ? 1 : 0.6;
    const pace = Math.pow(act, 0.85) * working;
    // Integrated here rather than as rate * t in the shader: geode multiplies its
    // spin rate onto the whole elapsed phase, so any change of pace re-scales
    // every past second and the stone lurches.
    if (!reduced) {
      phase += dt * quick;
      yaw += dt * tempo * (0.088 + (0.30 - 0.088) * 0.7 * w("responding")) * (1 + 0.8 * pace);
      if (cur === "responding") front = (front + dt * (0.22 + 0.45 * act)) % 1;
    }
    frameNo++;
    if (frameNo % 30 === 1) readColors();
    if (reduced && !dirty) return;
    dirty = false;
    const errW = w("error");
    const f = {
      time: Math.max(phase + phaseOffset(to.entry, tau) * to.speed, 0),
      yaw, front,
      glow: lerp("glow") * glowBoost(to.entry, tau),
      depth: lerp("depth"),
      hue: lerp("hue"),
      index: to.i,
      tau: Math.max(tau, 0),
      think: w("thinking"),
      resp: w("responding"),
      err: errW,
      tone: drain(colors.tone, 0.8 * errW),
    };
    if (heroCanvas.offsetParent !== null) draw(big, f);
    if (mini) draw(mini, f);
    if (frameNo % 6 === 0) {
      $("#pp-read").textContent =
        `${cur} τ${f.tau.toFixed(1)} t${f.time.toFixed(1)} glow${f.glow.toFixed(2)} act${act.toFixed(2)}` +
        ` think${f.think.toFixed(2)} resp${f.resp.toFixed(2)} err${f.err.toFixed(2)}`;
    }
  }

  bar.addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    if (b.dataset.cyc) setVariant(vi + +b.dataset.cyc);
    else if (b.dataset.state) { demo = false; $("#pp-demo").classList.remove("on"); setState(b.dataset.state); }
    else if (b.id === "pp-demo") { demo = !demo; demoAt = performance.now() / 1000; b.classList.toggle("on", demo); }
    else if (b.id === "pp-rand") { $("#pp-seed").value = Math.random().toString(36).slice(2, 8); applySeed($("#pp-seed").value); dirty = true; }
  });
  $("#pp-seed").addEventListener("input", (ev) => { applySeed(ev.target.value); dirty = true; });
  addEventListener("keydown", (ev) => {
    if (ev.target.closest && ev.target.closest("input, textarea, [contenteditable]")) return;
    if (ev.key === "ArrowLeft") setVariant(vi - 1);
    else if (ev.key === "ArrowRight") setVariant(vi + 1);
    else if (ev.key >= "1" && ev.key <= "5") { demo = false; setState(NAMES[+ev.key - 1]); }
    else if (ev.key === "d") $("#pp-demo").click();
    else return;
    ev.preventDefault();
  });

  applySeed("silica");
  readColors();
  setVariant(vi);
  bar.querySelector('[data-state="idle"]').classList.add("on");
  requestAnimationFrame(frame);
})();
