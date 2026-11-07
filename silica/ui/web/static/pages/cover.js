// The press a wiki's cover is printed on (ADR-0146 point 4), the app's and Pages' one text: each
// loads it when it first prints, because a published page's <head> is its bytes and the app's
// script list is index.html's. A classic script, as mermaid is: a page built on a laptop opens
// from file://, which refuses a module. `N` is the layout's rows, x and y in 0..1 and links at [3].
function coverPress(cv, N, w, h, pad) {
  const X = (i) => Math.round(pad + N[i][0] * (w - 2 * pad)), Y = (i) => Math.round(pad + N[i][1] * (h - 2 * pad));
  const rad = (i) => (N[i][3] >= 12 ? 2 : 1);
  const x = cv.getContext("2d"), img = x.createImageData(w, h), px = new Uint32Array(img.data.buffer);
  const put = (a, b, c) => { if (a >= 0 && b >= 0 && a < w && b < h) px[b * w + a] = c; };
  const seg = (x0, y0, x1, y1, c) => {
    const n = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0), 1);
    for (let k = 0; k <= n; k++) put(Math.round(x0 + (x1 - x0) * k / n), Math.round(y0 + (y1 - y0) * k / n), c);
  };
  const disc = (cx, cy, rr, c) => { for (let a = -rr; a <= rr; a++) for (let b = -rr; b <= rr; b++) if (a * a + b * b <= rr * rr + rr) put(cx + a, cy + b, c); };
  return { X, Y, rad, seg, disc, px, done: () => x.putImageData(img, 0, 0) };
}
