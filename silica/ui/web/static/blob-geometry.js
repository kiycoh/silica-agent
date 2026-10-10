// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 Alessandro Carosia

// Shared by the graph frame (ADR-0074) and the drawer's Place figure
// (ADR-0075), so a blob is one drawing in both. A plain script with no
// imports: graph_view.py inlines it ahead of graph-frame.js in the frame's
// document, index.html loads it ahead of work.js, and the frame tests read it
// ahead of the frame's pure block.

// --- An area's blob (ADR-0074) ----------------------------------------------
// Each area is a field over its own notes, f = sum of (1 - d^2/R^2)^2, and its
// blob is where f - max(tau, the strongest other area's f) >= 0: the level tau
// against empty space, the line f_a = f_b against a neighbour. So two blobs
// share a border at most, and a note pulled into another area shows as an
// island of its own colour instead of a bulge of its neighbour.
//
// Marching squares one cell at a time, without stitching rings: each cell
// hands back the piece of the blob inside it and the piece of its outline.
// The pieces tile, and one fill() over all of them composites once, the
// reason drawZones keeps one path per zone. A cell wholly inside joins the
// run of full cells on its row, so the path holds a run per row and not a
// rectangle per cell. Saddles are split by the centre value.
//
// points: [{x, y, k}], k the area's index. Returns, per k, {fill: [flat
// polygons], line: [[x1, y1, x2, y2]], n, x, y}, x and y its notes' centroid.
// `cell` is the grid step, R/3 unless given: the frame's R is a few screen
// pixels at its fit, while the drawer's is ~30px, where a step of R/3 shows as
// facets on the outline.
const BLOB_MAX_CELLS = 250000;   // a frame mid-settle spans several times a settled one
const MARCH_X = [0, 1, 1, 0], MARCH_Y = [0, 0, 1, 1];
function blobGeometry(points, R, tau, cell) {
  let K = 0;
  for (const p of points) K = Math.max(K, p.k + 1);
  const out = Array.from({ length: K }, () => ({ fill: [], line: [], n: 0, x: 0, y: 0 }));
  if (!K) return out;
  const ext = out.map(() => [Infinity, Infinity, -Infinity, -Infinity]);
  for (const p of points) {
    const o = out[p.k], e = ext[p.k];
    o.n++; o.x += p.x; o.y += p.y;
    e[0] = Math.min(e[0], p.x); e[1] = Math.min(e[1], p.y);
    e[2] = Math.max(e[2], p.x); e[3] = Math.max(e[3], p.y);
  }
  out.forEach(o => { if (o.n) { o.x /= o.n; o.y /= o.n; } });
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const e of ext) if (e[0] <= e[2]) {
    x0 = Math.min(x0, e[0]); y0 = Math.min(y0, e[1]); x1 = Math.max(x1, e[2]); y1 = Math.max(y1, e[3]);
  }
  x0 -= R; y0 -= R; x1 += R; y1 += R;
  let c = cell || R / 3;
  const cells = (Math.ceil((x1 - x0) / c) + 1) * (Math.ceil((y1 - y0) / c) + 1);
  if (cells > BLOB_MAX_CELLS) c *= Math.sqrt(cells / BLOB_MAX_CELLS);
  const gw = Math.ceil((x1 - x0) / c) + 1, gh = Math.ceil((y1 - y0) / c) + 1;
  const top1 = new Float32Array(gw * gh), top2 = new Float32Array(gw * gh);
  const lab = new Int16Array(gw * gh).fill(-1);
  const box = ext.map(e => e[0] > e[2] ? null : [
    Math.max(0, Math.floor((e[0] - R - x0) / c)), Math.max(0, Math.floor((e[1] - R - y0) / c)),
    Math.min(gw - 1, Math.ceil((e[2] + R - x0) / c)), Math.min(gh - 1, Math.ceil((e[3] + R - y0) / c))]);
  const F = box.map(b => b && new Float32Array((b[2] - b[0] + 1) * (b[3] - b[1] + 1)));
  const R2 = R * R, rc = Math.ceil(R / c);
  for (const p of points) {
    const b = box[p.k], f = F[p.k], bw = b[2] - b[0] + 1;
    const gx = (p.x - x0) / c, gy = (p.y - y0) / c;
    const ix0 = Math.max(b[0], Math.floor(gx) - rc), ix1 = Math.min(b[2], Math.ceil(gx) + rc);
    const iy0 = Math.max(b[1], Math.floor(gy) - rc), iy1 = Math.min(b[3], Math.ceil(gy) + rc);
    for (let iy = iy0; iy <= iy1; iy++) {
      const dy = y0 + iy * c - p.y, row = (iy - b[1]) * bw - b[0];
      for (let ix = ix0; ix <= ix1; ix++) {
        const dx = x0 + ix * c - p.x, d2 = dx * dx + dy * dy;
        if (d2 < R2) { const q = 1 - d2 / R2; f[row + ix] += q * q; }
      }
    }
  }
  box.forEach((b, k) => {
    if (!b) return;
    const f = F[k], bw = b[2] - b[0] + 1;
    for (let iy = b[1]; iy <= b[3]; iy++) for (let ix = b[0]; ix <= b[2]; ix++) {
      const v = f[(iy - b[1]) * bw + ix - b[0]], i = iy * gw + ix;
      if (v > top1[i]) { top2[i] = top1[i]; top1[i] = v; lab[i] = k; } else if (v > top2[i]) top2[i] = v;
    }
  });
  const X = gx => x0 + gx * c, Y = gy => y0 + gy * c;
  box.forEach((b, k) => {
    if (!b) return;
    const f = F[k], bw = b[2] - b[0] + 1, { fill, line } = out[k];
    const v = (ix, iy) => {
      if (ix < b[0] || iy < b[1] || ix > b[2] || iy > b[3]) return -1;
      const i = iy * gw + ix;
      return f[(iy - b[1]) * bw + ix - b[0]] - Math.max(tau, lab[i] === k ? top2[i] : top1[i]);
    };
    for (let iy = b[1] - 1; iy <= b[3]; iy++) {
      // The first and last cell of a row each have a corner outside the box,
      // so a run of full cells always ends inside the loop.
      let run = -1;
      for (let ix = b[0] - 1; ix <= b[2]; ix++) {
        const vs = [v(ix, iy), v(ix + 1, iy), v(ix + 1, iy + 1), v(ix, iy + 1)];
        const mask = (vs[0] >= 0) | (vs[1] >= 0) << 1 | (vs[2] >= 0) << 2 | (vs[3] >= 0) << 3;
        if (mask === 15) { if (run < 0) run = ix; continue; }
        if (run >= 0) {
          fill.push([X(run), Y(iy), X(ix), Y(iy), X(ix), Y(iy + 1), X(run), Y(iy + 1)]);
          run = -1;
        }
        if (!mask) continue;
        // Round the cell from its top-left corner: every inside corner, and
        // the crossing on every edge whose two ends disagree.
        const poly = [], cross = [];
        for (let i = 0; i < 4; i++) {
          const j = (i + 1) & 3, a = vs[i], z = vs[j];
          if (a >= 0) poly.push(X(ix + MARCH_X[i]), Y(iy + MARCH_Y[i]));
          if ((a >= 0) !== (z >= 0)) {
            const t = a / (a - z);
            const px = X(ix + MARCH_X[i] + (MARCH_X[j] - MARCH_X[i]) * t);
            const py = Y(iy + MARCH_Y[i] + (MARCH_Y[j] - MARCH_Y[i]) * t);
            poly.push(px, py); cross.push(px, py);
          }
        }
        if (cross.length === 4) { fill.push(poly); line.push(cross); continue; }
        // A saddle (5: top-left and bottom-right in; 10: the other two) has a
        // crossing on every edge, so edge m's is cross m.
        const joined = vs[0] + vs[1] + vs[2] + vs[3] >= 0;
        const e = m => [cross[2 * m], cross[2 * m + 1]];
        if ((mask === 5) === joined) line.push([...e(0), ...e(1)], [...e(2), ...e(3)]);
        else line.push([...e(1), ...e(2)], [...e(3), ...e(0)]);
        if (joined) fill.push(poly);
        else for (const i of mask === 5 ? [0, 2] : [1, 3])
          fill.push([...e((i + 3) & 3), X(ix + MARCH_X[i]), Y(iy + MARCH_Y[i]), ...e(i)]);
      }
    }
  });
  return out;
}
