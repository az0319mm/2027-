// 수치·형태학 도우미 (Python 엔진의 numpy/scipy.ndimage 동작을 그대로 재현)
// 모든 마스크는 Uint8Array(w*h) (0/1), 행 우선(y*w + x).
"use strict";

const NB8 = [[-1, -1], [-1, 0], [-1, 1], [0, -1], [0, 1], [1, -1], [1, 0], [1, 1]];
const NB4 = [[-1, 0], [0, -1], [0, 1], [1, 0]];

// scipy.ndimage.label 과 같은 번호 (래스터 순서로 처음 만난 순서)
function label(mask, w, h, conn8) {
  const lab = new Int32Array(w * h);
  const boxes = [];          // [y0, y1, x0, x1] (끝 미포함)
  const areas = [0];
  const nb = conn8 ? NB8 : NB4;
  const stack = new Int32Array(w * h);
  let n = 0;
  for (let i = 0; i < w * h; i++) {
    if (!mask[i] || lab[i]) continue;
    n++;
    let sp = 0;
    stack[sp++] = i;
    lab[i] = n;
    let y0 = h, y1 = -1, x0 = w, x1 = -1, area = 0;
    while (sp) {
      const p = stack[--sp];
      const y = (p / w) | 0, x = p - y * w;
      area++;
      if (y < y0) y0 = y; if (y > y1) y1 = y; if (x < x0) x0 = x; if (x > x1) x1 = x;
      for (const [dy, dx] of nb) {
        const yy = y + dy, xx = x + dx;
        if (yy < 0 || yy >= h || xx < 0 || xx >= w) continue;
        const q = yy * w + xx;
        if (mask[q] && !lab[q]) { lab[q] = n; stack[sp++] = q; }
      }
    }
    boxes.push([y0, y1 + 1, x0, x1 + 1]);
    areas.push(area);
  }
  return { lab, n, boxes, areas };
}

// 성분 L 의 픽셀 좌표 (래스터 순서, np.nonzero 와 같음)
function pixelsOf(lab, w, box, L) {
  const ys = [], xs = [];
  for (let y = box[0]; y < box[1]; y++) for (let x = box[2]; x < box[3]; x++) {
    if (lab[y * w + x] === L) { ys.push(y); xs.push(x); }
  }
  return { ys, xs };
}

// distance_transform_cdt(metric='chessboard'): mask 픽셀에서 가장 가까운 0 픽셀까지 체스보드 거리
function chessboardDT(mask, w, h) {
  const INF = 1e9;
  const d = new Float64Array(w * h);
  for (let i = 0; i < w * h; i++) d[i] = mask[i] ? INF : 0;
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const i = y * w + x;
    if (!d[i]) continue;
    let v = d[i];
    if (x > 0) v = Math.min(v, d[i - 1] + 1);
    if (y > 0) {
      v = Math.min(v, d[i - w] + 1);
      if (x > 0) v = Math.min(v, d[i - w - 1] + 1);
      if (x < w - 1) v = Math.min(v, d[i - w + 1] + 1);
    }
    d[i] = v;
  }
  for (let y = h - 1; y >= 0; y--) for (let x = w - 1; x >= 0; x--) {
    const i = y * w + x;
    if (!d[i]) continue;
    let v = d[i];
    if (x < w - 1) v = Math.min(v, d[i + 1] + 1);
    if (y < h - 1) {
      v = Math.min(v, d[i + w] + 1);
      if (x < w - 1) v = Math.min(v, d[i + w + 1] + 1);
      if (x > 0) v = Math.min(v, d[i + w - 1] + 1);
    }
    d[i] = v;
  }
  return d;
}

// 1차원 거리 변환 (Felzenszwalb–Huttenlocher). f: 제곱 거리, '무한대'는 BIG 로 표현
const BIG = 1e20;
function dt1d(f, n, out, v, z) {
  let k = 0;
  v[0] = 0; z[0] = -Infinity; z[1] = Infinity;
  for (let q = 1; q < n; q++) {
    let s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2 * q - 2 * v[k]);
    while (s <= z[k]) {
      k--;
      s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2 * q - 2 * v[k]);
    }
    k++;
    v[k] = q; z[k] = s; z[k + 1] = Infinity;
  }
  k = 0;
  for (let q = 0; q < n; q++) {
    while (z[k + 1] < q) k++;
    out[q] = (q - v[k]) * (q - v[k]) + f[v[k]];
  }
}

// 정확한 유클리드 거리: mask 의 1 픽셀에서 가장 가까운 0 픽셀까지 (Python paper.edt_to_zero 와 같은 규칙)
// borderIsZero=true 이면 이미지 바깥을 0 으로 본다. 0 이 하나도 없으면 Infinity.
function edtToZero(mask, w, h, borderIsZero) {
  const W = w + 2, H = h + 2;
  const f = new Float64Array(W * H);
  let any0 = false;
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    let m;
    if (y === 0 || x === 0 || y === H - 1 || x === W - 1) m = borderIsZero ? 0 : 1;
    else m = mask[(y - 1) * w + (x - 1)] ? 1 : 0;
    f[y * W + x] = m ? BIG : 0;
    if (!m) any0 = true;
  }
  const out = new Float64Array(w * h);
  if (!any0) { out.fill(Infinity); return out; }
  const n = Math.max(W, H);
  const col = new Float64Array(n), res = new Float64Array(n), v = new Int32Array(n), z = new Float64Array(n + 1);
  for (let x = 0; x < W; x++) {
    for (let y = 0; y < H; y++) col[y] = f[y * W + x];
    dt1d(col, H, res, v, z);
    for (let y = 0; y < H; y++) f[y * W + x] = res[y];
  }
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) col[x] = f[y * W + x];
    dt1d(col, W, res, v, z);
    for (let x = 0; x < W; x++) f[y * W + x] = res[x];
  }
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const v2 = f[(y + 1) * W + x + 1];
    out[y * w + x] = v2 >= BIG / 2 ? Infinity : Math.sqrt(v2);
  }
  return out;
}

function diskOffsets(r) {
  const o = [];
  for (let dy = -r; dy <= r; dy++) for (let dx = -r; dx <= r; dx++) if (dy * dy + dx * dx <= r * r) o.push([dy, dx]);
  return o;
}

// 이진 팽창/침식 (scipy: 대칭 구조, 바깥 = borderValue)
function dilate(mask, w, h, offs) {
  const out = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    if (!mask[y * w + x]) continue;
    for (const [dy, dx] of offs) {
      const yy = y + dy, xx = x + dx;
      if (yy >= 0 && yy < h && xx >= 0 && xx < w) out[yy * w + xx] = 1;
    }
  }
  return out;
}
function erode(mask, w, h, offs, borderValue) {
  const out = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    let ok = 1;
    for (const [dy, dx] of offs) {
      const yy = y + dy, xx = x + dx;
      const v = (yy >= 0 && yy < h && xx >= 0 && xx < w) ? mask[yy * w + xx] : borderValue;
      if (!v) { ok = 0; break; }
    }
    out[y * w + x] = ok;
  }
  return out;
}
function closing(mask, w, h, offs) { return erode(dilate(mask, w, h, offs), w, h, offs, 0); }

// binary_fill_holes (기본 4연결): 가장자리와 4연결로 이어지지 않은 0 영역을 1 로
function fillHoles(mask, w, h) {
  const out = new Uint8Array(w * h).fill(1);
  const st = new Int32Array(w * h);
  let sp = 0;
  const push = (i) => { if (!mask[i] && out[i]) { out[i] = 0; st[sp++] = i; } };
  for (let x = 0; x < w; x++) { push(x); push((h - 1) * w + x); }
  for (let y = 0; y < h; y++) { push(y * w); push(y * w + w - 1); }
  while (sp) {
    const p = st[--sp];
    const y = (p / w) | 0, x = p - y * w;
    if (y > 0) push(p - w); if (y < h - 1) push(p + w); if (x > 0) push(p - 1); if (x < w - 1) push(p + 1);
  }
  return out;
}

// binary_propagation(seed, mask) (기본 4연결)
function propagate(seed, maskAllowed, w, h) {
  const out = new Uint8Array(w * h);
  const st = new Int32Array(w * h);
  let sp = 0;
  for (let i = 0; i < w * h; i++) if (seed[i] && maskAllowed[i]) { out[i] = 1; st[sp++] = i; }
  while (sp) {
    const p = st[--sp];
    const y = (p / w) | 0, x = p - y * w;
    const nb = [y > 0 ? p - w : -1, y < h - 1 ? p + w : -1, x > 0 ? p - 1 : -1, x < w - 1 ? p + 1 : -1];
    for (const q of nb) if (q >= 0 && maskAllowed[q] && !out[q]) { out[q] = 1; st[sp++] = q; }
  }
  return out;
}

// uniform_filter (mode='reflect' 또는 'constant'), 크기 size 의 정사각형 평균
function uniformFilter(arr, w, h, size, mode) {
  const r = size >> 1;
  const idx = (i, n) => {
    if (mode === "constant") return i < 0 || i >= n ? -1 : i;
    while (i < 0 || i >= n) i = i < 0 ? -i - 1 : 2 * n - i - 1;     // scipy 'reflect' (d c b a | a b c d)
    return i;
  };
  const tmp = new Float64Array(w * h), out = new Float64Array(w * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    let s = 0;
    for (let k = -r; k <= size - 1 - r; k++) { const xx = idx(x + k, w); if (xx >= 0) s += arr[y * w + xx]; }
    tmp[y * w + x] = s / size;
  }
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    let s = 0;
    for (let k = -r; k <= size - 1 - r; k++) { const yy = idx(y + k, h); if (yy >= 0) s += tmp[yy * w + x]; }
    out[y * w + x] = s / size;
  }
  return out;
}

function median(a) {
  const s = Array.from(a).sort((x, y) => x - y);
  const n = s.length;
  if (!n) return NaN;
  return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2;
}
function percentile(a, p) {            // numpy 'linear'
  const s = Array.from(a).sort((x, y) => x - y);
  const n = s.length;
  if (!n) return NaN;
  const r = (p / 100) * (n - 1), lo = Math.floor(r), hi = Math.ceil(r);
  return s[lo] + (s[hi] - s[lo]) * (r - lo);
}
function polyfitSlope(xs, ys) {
  const n = xs.length;
  let mx = 0, my = 0;
  for (let i = 0; i < n; i++) { mx += xs[i]; my += ys[i]; }
  mx /= n; my /= n;
  let sxy = 0, sxx = 0;
  for (let i = 0; i < n; i++) { sxy += (xs[i] - mx) * (ys[i] - my); sxx += (xs[i] - mx) * (xs[i] - mx); }
  return sxx === 0 ? 0 : sxy / sxx;
}

function amin(a) { let m = Infinity; for (let i = 0; i < a.length; i++) if (a[i] < m) m = a[i]; return m; }
function amax(a) { let m = -Infinity; for (let i = 0; i < a.length; i++) if (a[i] > m) m = a[i]; return m; }

module.exports = { amin, amax, NB8, NB4, label, pixelsOf, chessboardDT, edtToZero, diskOffsets, dilate, erode, closing,
                   fillHoles, propagate, uniformFilter, median, percentile, polyfitSlope };
