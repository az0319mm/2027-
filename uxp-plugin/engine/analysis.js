// 픽셀 단위 탐지기 — Python psd_cleanup/analysis.py 를 그대로 옮긴 것 (결과 일치 테스트로 검증)
// 이미지: { w, h, d: Uint8Array(w*h*4) RGBA }
"use strict";
const N = require("./nd.js");

const NB8 = N.NB8;
const OOB = -1;

function keysOf(img) {
  const { w, h, d } = img;
  const K = new Float64Array(w * h);
  for (let i = 0; i < w * h; i++) {
    const a = d[i * 4 + 3];
    K[i] = a === 0 ? 0 : d[i * 4] * 16777216 + d[i * 4 + 1] * 65536 + d[i * 4 + 2] * 256 + a;
  }
  return K;
}
function keyToRgba(k) {
  if (k <= 0) return [0, 0, 0, 0];
  return [Math.floor(k / 16777216) & 255, Math.floor(k / 65536) & 255, Math.floor(k / 256) & 255, k & 255];
}
const at = (K, w, h, y, x, oob) => (y >= 0 && y < h && x >= 0 && x < w ? K[y * w + x] : oob);

function patchSig(K, w, h, y, x, r, oob) {
  const vals = [];
  for (let yy = y - r; yy <= y + r; yy++) for (let xx = x - r; xx <= x + r; xx++) vals.push(at(K, w, h, yy, xx, oob));
  return vals;
}

function latticeMembers(points, maxN) {
  maxN = maxN || 1500;
  const n = points.length;
  if (n > maxN) return new Array(n).fill(true);
  const idx = new Map();
  points.forEach((p, i) => idx.set(p[0] + "," + p[1], i));
  const mem = new Array(n).fill(false);
  for (let i = 0; i < n; i++) {
    const [yi, xi] = points[i];
    for (let j = i + 1; j < n; j++) {
      const [yj, xj] = points[j];
      const vy = yj - yi, vx = xj - xi;
      for (const t of [[yj + vy, xj + vx], [yi - vy, xi - vx]]) {
        const k = idx.get(t[0] + "," + t[1]);
        if (k !== undefined) { mem[i] = mem[j] = true; mem[k] = true; }
      }
    }
  }
  return mem;
}

function repetitionIntentional(points, sigs, structured, repeatMin, textureMin) {
  textureMin = textureMin || 20;
  const groups = new Map();
  sigs.forEach((s, i) => { if (!groups.has(s)) groups.set(s, []); groups.get(s).push(i); });
  const out = new Array(sigs.length).fill(false), counts = new Array(sigs.length).fill(0), why = new Array(sigs.length).fill(null);
  for (const ids of groups.values()) {
    for (const i of ids) counts[i] = ids.length;
    if (ids.length < repeatMin) continue;
    if (ids.length >= textureMin) { for (const i of ids) { out[i] = true; why[i] = "texture"; } continue; }
    const lat = latticeMembers(ids.map((i) => points[i]));
    ids.forEach((i, k) => {
      out[i] = structured[i] || lat[k];
      why[i] = lat[k] ? "lattice" : (structured[i] ? "structured" : null);
    });
  }
  return { flags: out, counts, why };
}

function repeatedMask(K, w, h, ys, xs, r, minCount, oob) {
  const sigs = [], structured = [];
  for (let i = 0; i < ys.length; i++) {
    const p = patchSig(K, w, h, ys[i], xs[i], r, oob);
    sigs.push(p.join(","));
    const q = p.slice();
    q[q.length >> 1] = q[0];
    structured.push(new Set(q).size >= 2);
  }
  const res = repetitionIntentional(ys.map((y, i) => [y, xs[i]]), sigs, structured, minCount);
  return { flags: res.flags, counts: res.counts };
}

// ------------------------------------------------------------ 1. 고립 점
function smallComponents(img, maxArea, gapCap, alphaMax) {
  gapCap = gapCap === undefined ? 8 : gapCap;
  const { w, h, d } = img;
  const mask = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) mask[i] = d[i * 4 + 3] > 0 ? 1 : 0;
  const L = N.label(mask, w, h, true);
  if (!L.n) return [];
  const out = [];
  for (let l = 1; l <= L.n; l++) {
    if (L.areas[l] > maxArea) continue;
    const box = L.boxes[l - 1];
    const { ys, xs } = N.pixelsOf(L.lab, w, box, l);
    let maxA = 0, sr = 0, sg = 0, sb = 0;
    for (let k = 0; k < ys.length; k++) {
      const p = (ys[k] * w + xs[k]) * 4;
      maxA = Math.max(maxA, d[p + 3]); sr += d[p]; sg += d[p + 1]; sb += d[p + 2];
    }
    if (alphaMax !== undefined && alphaMax !== null && maxA > alphaMax) continue;
    const [y0, y1, x0, x1] = box;
    const wy0 = Math.max(0, y0 - gapCap - 1), wy1 = Math.min(h, y1 + gapCap + 1);
    const wx0 = Math.max(0, x0 - gapCap - 1), wx1 = Math.min(w, x1 + gapCap + 1);
    let best = Infinity;
    for (let yy = wy0; yy < wy1; yy++) for (let xx = wx0; xx < wx1; xx++) {
      const v = L.lab[yy * w + xx];
      if (v > 0 && v !== l) {
        for (let k = 0; k < ys.length; k++) {
          const dd = Math.max(Math.abs(yy - ys[k]), Math.abs(xx - xs[k]));
          if (dd < best) best = dd;
        }
      }
    }
    const gap = best === Infinity ? gapCap : best - 1;
    const n = ys.length;
    out.push({ label: l, ys, xs, bbox: [x0, y0, x1, y1], area: n, maxAlpha: maxA,
               meanRgb: [Math.floor(sr / n), Math.floor(sg / n), Math.floor(sb / n)], gap: Math.min(gap, gapCap),
               similarCount: 1, exactRepeat: false, repeatReason: null });
  }
  const sig = (c) => [c.area, c.bbox[2] - c.bbox[0], c.bbox[3] - c.bbox[1], c.meanRgb.map((v) => Math.floor(v / 32)).join("/"),
                      Math.floor(c.maxAlpha / 64)].join("|");
  const cnt = new Map();
  out.forEach((c) => cnt.set(sig(c), (cnt.get(sig(c)) || 0) + 1));
  out.forEach((c) => { c.similarCount = cnt.get(sig(c)); });
  const exact = (c) => {
    const parts = [c.bbox[2] - c.bbox[0], c.bbox[3] - c.bbox[1]];
    for (let y = c.bbox[1]; y < c.bbox[3]; y++) for (let x = c.bbox[0]; x < c.bbox[2]; x++) {
      const p = (y * w + x) * 4;
      if (d[p + 3] === 0) parts.push("0,0,0,0"); else parts.push(d[p] + "," + d[p + 1] + "," + d[p + 2] + "," + d[p + 3]);
    }
    return parts.join(";");
  };
  const res = repetitionIntentional(out.map((c) => [c.bbox[1], c.bbox[0]]), out.map(exact), out.map(() => false), 3);
  out.forEach((c, i) => { c.exactRepeat = res.flags[i]; c.repeatReason = res.why[i]; });
  return out;
}

// --------------------------------------------- 2. 1픽셀 잘못 칠함 / 1픽셀 구멍
function singlePixelAnomalies(img, repeatMin, hasAlpha, patchR) {
  patchR = patchR || 3;
  const { w, h } = img;
  if (h < 3 || w < 3) return [];
  const K = keysOf(img);
  const oob = hasAlpha ? 0 : OOB;
  const ys = [], xs = [], n0s = [];
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const n0 = at(K, w, h, y - 1, x - 1, oob);
    if (!(n0 > 0) || (n0 % 256) !== 255) continue;
    let same = true;
    for (let k = 1; k < 8; k++) if (at(K, w, h, y + NB8[k][0], x + NB8[k][1], oob) !== n0) { same = false; break; }
    if (!same || K[y * w + x] === n0) continue;
    ys.push(y); xs.push(x); n0s.push(n0);
  }
  if (!ys.length) return [];
  const rep = repeatedMask(K, w, h, ys, xs, patchR, repeatMin, oob);
  return ys.map((y, i) => {
    const k = K[y * w + xs[i]];
    return { y, x: xs[i], kind: k === 0 ? "pinhole" : "odd_pixel", old: keyToRgba(k), new: keyToRgba(n0s[i]),
             repeatCount: rep.counts[i], intentional: rep.flags[i] };
  });
}

// ------------------------------------------------ 3. 곧은 경계 1픽셀 돌출/패임
const DIRS = [["up", -1, 0], ["down", 1, 0], ["left", 0, -1], ["right", 0, 1]];

function plateau(K, w, h, y, x, dy, dx, ey, ex, I, O, oob, maxlen) {
  maxlen = maxlen || 64;
  const res = [];
  for (const sg of [1, -1]) {
    let k = 1;
    while (k <= maxlen) {
      const py = y + sg * k * ey, px = x + sg * k * ex;
      if (at(K, w, h, py + dy, px + dx, oob) === I && at(K, w, h, py, px, oob) === O) { k++; continue; }
      break;
    }
    const py = y + sg * k * ey, px = x + sg * k * ex;
    const shoulder = at(K, w, h, py + dy, px + dx, oob) === O && at(K, w, h, py + 2 * dy, px + 2 * dx, oob) === I;
    res.push([k - 1, shoulder && k <= maxlen]);
  }
  return res;
}

function edgeBumps(img, repeatMin, hasAlpha, patchR, straightHalf) {
  patchR = patchR || 3; straightHalf = straightHalf || 2;
  const { w, h } = img;
  if (h < 3 || w < 3) return [];
  const K = keysOf(img);
  const oob = hasAlpha ? 0 : OOB;
  const S = (y, x) => at(K, w, h, y, x, oob);
  const out = [];
  for (const [name, dy, dx] of DIRS) {
    const ey = dx, ex = dy;
    const ys = [], xs = [];
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const k = K[y * w + x];
      const I = S(y + dy, x + dx), O = S(y - dy, x - dx);
      if (!(k === I && I !== O && I !== OOB && O !== OOB)) continue;
      let ok = true;
      for (let s = 1; s <= straightHalf && ok; s++) for (const sg of [1, -1]) {
        if (S(y + sg * s * ey, x + sg * s * ex) !== O || S(y + dy + sg * s * ey, x + dx + sg * s * ex) !== I) { ok = false; break; }
      }
      if (!ok) continue;
      for (const sg of [1, -1]) {
        if (S(y - dy + sg * ey, x - dx + sg * ex) !== O || S(y + 2 * dy + sg * ey, x + 2 * dx + sg * ex) !== I) { ok = false; break; }
      }
      if (!ok || S(y + 2 * dy, x + 2 * dx) !== I) continue;
      ys.push(y); xs.push(x);
    }
    if (!ys.length) continue;
    const rep = repeatedMask(K, w, h, ys, xs, patchR, repeatMin, oob);
    ys.forEach((y, i) => {
      const x = xs[i];
      const ik = S(y + dy, x + dx), ok_ = S(y - dy, x - dx);
      const [[a, sa], [b, sb]] = plateau(K, w, h, y, x, dy, dx, ey, ex, ik, ok_, oob);
      out.push({ y, x, kind: ik === 0 ? "edge_notch" : "edge_spur", direction: name, old: keyToRgba(ik), new: keyToRgba(ok_),
                 repeatCount: rep.counts[i], intentional: rep.flags[i], apex: sa && sb && Math.abs(a - b) <= 1, plateau: [a, b] });
    });
  }
  return out;
}

// ------------------------------------------------------ 4. 색 / 허용 색
function colorStats(img, top, opaqueOnly) {
  top = top || 64;
  const { w, h, d } = img;
  const cnt = new Map();
  let total = 0;
  for (let i = 0; i < w * h; i++) {
    const a = d[i * 4 + 3];
    if (opaqueOnly === false ? a === 0 : a !== 255) continue;
    const k = d[i * 4] * 65536 + d[i * 4 + 1] * 256 + d[i * 4 + 2];
    cnt.set(k, (cnt.get(k) || 0) + 1);
    total++;
  }
  if (!total) return { n: 0, tops: [], total: 0 };
  // numpy: np.unique (오름차순) → argsort(-cnt) (안정 정렬 아님: quicksort). 같은 개수일 때 순서는 보고용이라 키 오름차순으로 통일
  const entries = Array.from(cnt.entries()).sort((a, b) => a[0] - b[0]);
  const order = entries.slice().sort((a, b) => b[1] - a[1]);
  const tops = order.slice(0, top).map(([k, c]) => [(k >> 16) & 255, (k >> 8) & 255, k & 255, c]);
  return { n: entries.length, tops, total };
}

function offPaletteGroups(img, palette, tol) {
  tol = tol || 0;
  const { w, h, d } = img;
  const P = palette;
  const palIdx = new Int32Array(w * h).fill(-1), opaque = new Uint8Array(w * h), off = new Uint8Array(w * h);
  let any = false;
  for (let i = 0; i < w * h; i++) {
    opaque[i] = d[i * 4 + 3] === 255 ? 1 : 0;
    let best = -1, bd = Infinity, inPal = false;
    for (let j = 0; j < P.length; j++) {
      const dd = Math.max(Math.abs(d[i * 4] - P[j][0]), Math.abs(d[i * 4 + 1] - P[j][1]), Math.abs(d[i * 4 + 2] - P[j][2]));
      if (dd <= tol) inPal = true;
      if (dd < bd) { bd = dd; best = j; }
    }
    if (inPal) palIdx[i] = best;
    if (opaque[i] && !inPal) { off[i] = 1; any = true; }
  }
  if (!any) return [];
  const L = N.label(off, w, h, true);
  const out = [];
  for (let l = 1; l <= L.n; l++) {
    const { ys, xs } = N.pixelsOf(L.lab, w, L.boxes[l - 1], l);
    let certain = true;
    const news = [];
    for (let j = 0; j < ys.length && certain; j++) {
      const y = ys[j], x = xs[j];
      const uniq = new Set();
      for (const [dy, dx] of NB8) {
        const yy = y + dy, xx = x + dx;
        if (yy < 0 || yy >= h || xx < 0 || xx >= w) continue;
        const q = yy * w + xx;
        if (palIdx[q] >= 0 && opaque[q]) uniq.add(palIdx[q]);
      }
      if (uniq.size < 2) { certain = false; break; }
      const cols = Array.from(uniq).sort((a, b) => a - b).map((u) => P[u]);
      const c = [d[(y * w + x) * 4], d[(y * w + x) * 4 + 1], d[(y * w + x) * 4 + 2]];
      for (let ch = 0; ch < 3; ch++) {
        const lo = Math.min(...cols.map((q) => q[ch])) - 2, hi = Math.max(...cols.map((q) => q[ch])) + 2;
        if (c[ch] < lo || c[ch] > hi) { certain = false; break; }
      }
      if (!certain) break;
      let bi = 0, bs = Infinity;
      cols.forEach((q, k) => { const s = Math.abs(q[0] - c[0]) + Math.abs(q[1] - c[1]) + Math.abs(q[2] - c[2]); if (s < bs) { bs = s; bi = k; } });
      news.push(cols[bi]);
    }
    const colors = Array.from(new Set(ys.slice(0, 200).map((y, j) => {
      const p = (y * w + xs[j]) * 4; return [d[p], d[p + 1], d[p + 2]].join(",");
    }))).map((s) => s.split(",").map(Number)).sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2]).slice(0, 20);
    out.push({ ys, xs, newRgb: certain ? news : null, certain, colors,
               bbox: [N.amin(xs), N.amin(ys), N.amax(xs) + 1, N.amax(ys) + 1] });
  }
  return out;
}

// ---------------------------------------------------------- 5. 구멍
function enclosedHoles(img, minArea, maxArea) {
  const { w, h, d } = img;
  const hole = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) hole[i] = d[i * 4 + 3] === 0 ? 1 : 0;
  const L = N.label(hole, w, h, false);
  if (!L.n) return [];
  const border = new Set();
  for (let x = 0; x < w; x++) { border.add(L.lab[x]); border.add(L.lab[(h - 1) * w + x]); }
  for (let y = 0; y < h; y++) { border.add(L.lab[y * w]); border.add(L.lab[y * w + w - 1]); }
  const out = [];
  for (let l = 1; l <= L.n; l++) {
    if (border.has(l) || !(minArea <= L.areas[l] && L.areas[l] <= maxArea)) continue;
    out.push(N.pixelsOf(L.lab, w, L.boxes[l - 1], l));
  }
  return out;
}

// ---------------------------------------------------- 6. 종이색 외곽 잔여 (보고용)
function lumAt(d, i) { return 0.299 * d[i * 4] + 0.587 * d[i * 4 + 1] + 0.114 * d[i * 4 + 2]; }

function paperFringe(img, paperRgb, ring, minArea) {
  ring = ring || 2; minArea = minArea || 6;
  const { w, h, d } = img;
  const mask = new Uint8Array(w * h);
  let anyOn = false, allOn = true;
  for (let i = 0; i < w * h; i++) { mask[i] = d[i * 4 + 3] > 0 ? 1 : 0; if (mask[i]) anyOn = true; else allOn = false; }
  if (!anyOn || allOn) return [];
  const dist = N.chessboardDT(mask, w, h);
  const lum = new Float64Array(w * h), edge = new Uint8Array(w * h);
  let anyEdge = false;
  for (let i = 0; i < w * h; i++) {
    lum[i] = lumAt(d, i);
    const r = d[i * 4], g = d[i * 4 + 1], b = d[i * 4 + 2];
    let like;
    if (paperRgb) like = Math.max(Math.abs(r - paperRgb[0]), Math.abs(g - paperRgb[1]), Math.abs(b - paperRgb[2])) <= 24;
    else like = lum[i] >= 215 && Math.max(r, g, b) - Math.min(r, g, b) <= 25;
    if (mask[i] && dist[i] <= ring && like && d[i * 4 + 3] >= 128) { edge[i] = 1; anyEdge = true; }
  }
  if (!anyEdge) return [];
  const innerL = new Float64Array(w * h), inner = new Float64Array(w * h);
  for (let i = 0; i < w * h; i++) { const on = dist[i] >= ring + 2 && dist[i] <= ring + 6 ? 1 : 0; inner[i] = on; innerL[i] = on ? lum[i] : 0; }
  const s = N.uniformFilter(innerL, w, h, 13, "reflect"), c = N.uniformFilter(inner, w, h, 13, "reflect");
  const fr = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) {
    if (!edge[i] || !(c[i] > 0.02)) continue;
    const im = s[i] / Math.max(c[i], 1e-9);
    if (im <= lum[i] - 35) fr[i] = 1;
  }
  const L = N.label(fr, w, h, true);
  const out = [];
  for (let l = 1; l <= L.n; l++) if (L.areas[l] >= minArea) out.push(N.pixelsOf(L.lab, w, L.boxes[l - 1], l));
  return out;
}

// ---------------------------------------------------- 7. 리핏 연결부
function premul(d, p) { const a = d[p + 3] / 255; return [d[p] * a, d[p + 1] * a, d[p + 2] * a, d[p + 3]]; }

// 캔버스 좌표의 열/행 4줄을 레이어에서 가져와 [줄 수][4][4] 형태로
function canvasLines(img, left, top, W, H, axis, idxs) {
  const { w, h, d } = img;
  const n = axis === "col" ? H : W;
  const out = [];
  for (let t = 0; t < n; t++) {
    const row = [];
    for (const c of idxs) {
      let px = [0, 0, 0, 0];
      const ly = axis === "col" ? t - top : c - top, lx = axis === "col" ? c - left : t - left;
      if (ly >= 0 && ly < h && lx >= 0 && lx < w) { const p = (ly * w + lx) * 4; px = [d[p], d[p + 1], d[p + 2], d[p + 3]]; }
      row.push(px);
    }
    out.push(row);
  }
  return out;
}

function seamDiscontinuities(lines, minLen, absThr, ratio) {
  minLen = minLen || 2; absThr = absThr || 60; ratio = ratio || 2.5;
  const pm = (px) => { const a = px[3] / 255; return [px[0] * a, px[1] * a, px[2] * a, px[3]]; };
  const N_ = lines.length;
  const dSeam = new Float64Array(N_), flag = new Uint8Array(N_);
  const L1 = (a, b) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]) + Math.abs(a[3] - b[3]);
  let flagged = 0;
  for (let i = 0; i < N_; i++) {
    const p = lines[i].map(pm);
    dSeam[i] = L1(p[2], p[1]);
    const dIn = Math.max(L1(p[1], p[0]), L1(p[3], p[2]));
    flag[i] = dSeam[i] > absThr && dSeam[i] > ratio * dIn + 8 ? 1 : 0;
    flagged += flag[i];
  }
  const segs = [];
  let i = 0;
  while (i < N_) {
    if (flag[i]) {
      let j = i;
      while (j + 1 < N_ && (flag[j + 1] || (j + 2 < N_ && flag[j + 2]))) j++;
      if (j - i + 1 >= minLen) { let mx = 0; for (let k = i; k <= j; k++) mx = Math.max(mx, dSeam[k]); segs.push([i, j + 1, mx]); }
      i = j + 1;
    } else i++;
  }
  return { segs, stats: { flagged_lines: flagged, total_lines: N_ } };
}

// ================================================================ PDF 대조 후 추가 탐지기
function denseMask(points, h, w, radius, minCount) {
  radius = radius || 12; minCount = minCount || 5;
  if (!points.length) return [];
  const cnt = new Float64Array(w * h);
  for (const [y, x] of points) cnt[y * w + x] += 1;
  const size = 2 * radius + 1;
  const s = N.uniformFilter(cnt, w, h, size, "constant");
  return points.map(([y, x]) => s[y * w + x] * size * size >= minCount - 0.5);
}

function diagonalSpikesAndBoundaryOdd(img, repeatMin, hasAlpha, patchR) {
  patchR = patchR || 3;
  const { w, h } = img;
  if (h < 3 || w < 3) return [];
  const K = keysOf(img);
  const oob = hasAlpha ? 0 : OOB;
  const nsame = new Int8Array(w * h);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const k = K[y * w + x];
    let c = 0;
    for (const [dy, dx] of NB8) if (at(K, w, h, y + dy, x + dx, oob) === k) c++;
    nsame[y * w + x] = c;
  }
  const ring = [[-1, -1], [-1, 0], [-1, 1], [0, 1], [1, 1], [1, 0], [1, -1], [0, -1]];
  const ringIdx = ring.map((o) => NB8.findIndex((q) => q[0] === o[0] && q[1] === o[1]));
  const outIdx = [];
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const i = y * w + x, k = K[i];
    if (nsame[i] > 2 || k === OOB) continue;
    const nb = NB8.map(([dy, dx]) => at(K, w, h, y + dy, x + dx, oob));
    const s = nb.map((v) => v === k);
    const ns = nsame[i];
    const others = nb.filter((v, j) => !s[j]);
    if (others.some((v) => v === OOB)) continue;
    if (ns === 0) {
      if (new Set(others).size === 1) continue;
      const cnt = new Map();
      others.forEach((v) => cnt.set(v, (cnt.get(v) || 0) + 1));
      // np.unique 오름차순 + argmax (동률이면 가장 작은 값)
      let m = null, mc = -1;
      Array.from(cnt.keys()).sort((a, b) => a - b).forEach((v) => { if (cnt.get(v) > mc) { mc = cnt.get(v); m = v; } });
      if (mc < 5 || (m % 256) !== 255) continue;
      outIdx.push([y, x, "odd_pixel", m, 0]);
      continue;
    }
    if (new Set(others).size !== 1) continue;
    const o = others[0];
    if (ns === 2) {
      const pos = [];
      ringIdx.forEach((ri, j) => { if (s[ri]) pos.push(j); });
      const dd = ((pos[1] - pos[0]) % 8 + 8) % 8;
      if (dd !== 1 && dd !== 7) continue;
    }
    let thick = false;
    NB8.forEach(([dy, dx], j) => {
      if (s[j]) { const yy = y + dy, xx = x + dx; if (yy >= 0 && yy < h && xx >= 0 && xx < w && nsame[yy * w + xx] >= 5) thick = true; }
    });
    if (!thick) continue;
    let c5 = 0;
    for (let yy = Math.max(0, y - 2); yy < Math.min(h, y + 3); yy++)
      for (let xx = Math.max(0, x - 2); xx < Math.min(w, x + 3); xx++) if (K[yy * w + xx] === k) c5++;
    if (c5 <= 9) continue;
    const kind = k === 0 || (hasAlpha && (k % 256) === 0) ? "edge_notch" : "edge_spur";
    outIdx.push([y, x, kind, o, ns]);
  }
  if (!outIdx.length) return [];
  const rep = repeatedMask(K, w, h, outIdx.map((t) => t[0]), outIdx.map((t) => t[1]), patchR, repeatMin, oob);
  return outIdx.map(([y, x, kind, o, ns], i) => ({ y, x, kind, old: keyToRgba(K[y * w + x]), new: keyToRgba(o), sameNeighbors: ns,
                                                    repeatCount: rep.counts[i], intentional: rep.flags[i] }));
}

function edgeForeignSpecks(img, band, delta, maxArea) {
  band = band || 2; delta = delta || 60; maxArea = maxArea || 16;
  const { w, h, d } = img;
  const mask = new Uint8Array(w * h);
  let cnt = 0;
  for (let i = 0; i < w * h; i++) { mask[i] = d[i * 4 + 3] >= 128 ? 1 : 0; cnt += mask[i]; }
  if (cnt < 50 || cnt === w * h) return [];
  const dist = N.chessboardDT(mask, w, h);
  const lw = new Float64Array(w * h), wt = new Float64Array(w * h), lum = new Float64Array(w * h);
  for (let i = 0; i < w * h; i++) {
    // Python: float32 에서 계산 → 판정 경계 근처 오차는 보고용이라 허용
    lum[i] = 0.299 * d[i * 4] + 0.587 * d[i * 4 + 1] + 0.114 * d[i * 4 + 2];
    wt[i] = mask[i]; lw[i] = lum[i] * mask[i];
  }
  const s = N.uniformFilter(lw, w, h, 15, "reflect"), c = N.uniformFilter(wt, w, h, 15, "reflect");
  const odd = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) {
    if (!mask[i] || dist[i] > band) continue;
    if (Math.abs(lum[i] - s[i] / Math.max(c[i], 1e-6)) >= delta) odd[i] = 1;
  }
  const L = N.label(odd, w, h, true);
  const out = [];
  for (let l = 1; l <= L.n; l++) if (L.areas[l] <= maxArea) out.push(N.pixelsOf(L.lab, w, L.boxes[l - 1], l));
  return out;
}

function hardEdgeSegments(img, softRatio, minPx) {
  softRatio = softRatio || 0.4; minPx = minPx || 30;
  const { w, h, d } = img;
  const m = new Uint8Array(w * h);
  let any = false, all = true;
  for (let i = 0; i < w * h; i++) { m[i] = d[i * 4 + 3] > 0 ? 1 : 0; if (m[i]) any = true; else all = false; }
  if (!any || all) return [];
  const boundary = new Uint8Array(w * h);
  let nb = 0, soft = 0;
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const i = y * w + x;
    if (!m[i]) continue;
    let touch = false;
    for (const [dy, dx] of N.NB4) {
      const yy = y + dy, xx = x + dx;
      if (yy < 0 || yy >= h || xx < 0 || xx >= w || !m[yy * w + xx]) { touch = true; break; }
    }
    if (touch) { boundary[i] = 1; nb++; if (d[i * 4 + 3] < 255) soft++; }
  }
  if (nb < minPx || soft / nb < softRatio) return [];
  const hard = new Uint8Array(w * h);
  for (let i = 0; i < w * h; i++) hard[i] = boundary[i] && d[i * 4 + 3] === 255 ? 1 : 0;
  const cross = [[0, 0], [-1, 0], [1, 0], [0, -1], [0, 1]];
  const dil = N.dilate(N.dilate(hard, w, h, cross), w, h, cross);
  const L = N.label(dil, w, h, true);
  const out = [];
  for (let l = 1; l <= L.n; l++) {
    const { ys, xs } = N.pixelsOf(L.lab, w, L.boxes[l - 1], l);
    const hy = [], hx = [];
    ys.forEach((y, k) => { if (hard[y * w + xs[k]]) { hy.push(y); hx.push(xs[k]); } });
    if (hy.length >= minPx) out.push({ ys: hy, xs: hx });
  }
  return out;
}

function straightLineArtifacts(img, minLen) {
  minLen = minLen || 24;
  const W0 = img.w + 2, H0 = img.h + 2;
  const f = new Float64Array(W0 * H0 * 4);
  for (let y = 0; y < img.h; y++) for (let x = 0; x < img.w; x++) {
    const p = (y * img.w + x) * 4, q = ((y + 1) * W0 + x + 1) * 4, a = img.d[p + 3] / 255;
    f[q] = img.d[p] * a; f[q + 1] = img.d[p + 1] * a; f[q + 2] = img.d[p + 2] * a; f[q + 3] = img.d[p + 3];
  }
  const out = [];
  for (const axis of ["v", "h"]) {
    const N_ = axis === "v" ? H0 : W0, M = axis === "v" ? W0 : H0;     // g: (N, M) 축
    if (M < 3) continue;
    const g = (n, m) => (axis === "v" ? (n * W0 + m) * 4 : (m * W0 + n) * 4);
    const L1 = (p, q) => Math.abs(f[p] - f[q]) + Math.abs(f[p + 1] - f[q + 1]) + Math.abs(f[p + 2] - f[q + 2]) + Math.abs(f[p + 3] - f[q + 3]);
    for (let j = 0; j < M - 2; j++) {
      const col = new Uint8Array(N_);
      let sum = 0;
      for (let n = 0; n < N_; n++) {
        const c = g(n, j + 1), l = g(n, j), r = g(n, j + 2);
        const dl = L1(c, l), dr = L1(c, r), dlr = L1(l, r);
        if (dl > 30 && dr > 30 && dlr < 0.35 * Math.min(dl, dr)) { col[n] = 1; sum++; }
      }
      if (sum < minLen) continue;
      let n = 0;
      while (n < N_) {
        if (!col[n]) { n++; continue; }
        let e = n;
        while (e < N_ && col[e]) e++;
        if (e - n >= minLen) out.push([axis === "v" ? "vertical" : "horizontal", j, n - 1, e - 1]);
        n = e;
      }
    }
  }
  return out;
}

function internalSeams(img, runLen, minRuns, factor, thr) {
  runLen = runLen || 3; minRuns = minRuns || 3; factor = factor || 2.5; thr = thr === undefined ? 30 : thr;
  const { w, h, d } = img;
  const out = [];
  for (const axis of ["v", "h"]) {
    const N_ = axis === "v" ? h : w, M = axis === "v" ? w : h;
    if (M < 16 || N_ < 16) continue;
    const pix = (n, m) => (axis === "v" ? (n * w + m) * 4 : (m * w + n) * 4);
    const pmL1 = (p, q) => {
      const ap = d[p + 3] / 255, aq = d[q + 3] / 255;
      return Math.abs(d[q] * aq - d[p] * ap) + Math.abs(d[q + 1] * aq - d[p + 1] * ap) +
             Math.abs(d[q + 2] * aq - d[p + 2] * ap) + Math.abs(d[q + 3] - d[p + 3]);
    };
    const nruns = new Int32Array(M - 1), medlen = new Float64Array(M - 1), spans = new Map();
    for (let j = 0; j < M - 1; j++) {
      const lens = [];
      let starts = [], ends = [];
      let inRun = false, s0 = 0;
      for (let n = 0; n < N_; n++) {
        const st = pmL1(pix(n, j), pix(n, j + 1)) > thr;
        if (st && !inRun) { inRun = true; s0 = n; }
        if (!st && inRun) { inRun = false; if (n - s0 >= runLen) { lens.push(n - s0); starts.push(s0); ends.push(n); } }
      }
      if (inRun && N_ - s0 >= runLen) { lens.push(N_ - s0); starts.push(s0); ends.push(N_); }
      nruns[j] = lens.length;
      if (lens.length) { medlen[j] = N.median(lens); spans.set(j, [N.amin(starts), N.amax(ends)]); }
    }
    let mx = 0;
    for (let j = 0; j < M - 1; j++) mx = Math.max(mx, nruns[j]);
    if (mx < minRuns) continue;
    const base = Math.max(1, N.percentile(nruns, 99));
    for (let j = 0; j < M - 1; j++) {
      if (nruns[j] < minRuns || nruns[j] < factor * base) continue;
      let lm = -1;
      for (let k = Math.max(0, j - 4); k < Math.min(M - 1, j + 5); k++) if (k !== j) lm = Math.max(lm, nruns[k]);
      if (lm >= 0 && lm * 2 > nruns[j]) continue;
      if (medlen[j] >= 16) continue;
      const [r0, r1] = spans.get(j);
      out.push([axis === "v" ? "vertical" : "horizontal", j + 1, r0, r1, nruns[j]]);
    }
  }
  return out;
}

function chains(K, w, h, maxJump) {
  maxJump = maxJump || 2;
  const done = [];
  let prev = [];
  for (let x = 0; x < w; x++) {
    const cur = [], used = new Set();
    for (let y = 0; y < h - 1; y++) {
      const a = K[y * w + x], b = K[(y + 1) * w + x];
      if (a === b) continue;
      let best = -1;
      for (let ci = 0; ci < prev.length; ci++) {
        const [pa, pb, ch] = prev[ci];
        if (used.has(ci) || pa !== a || pb !== b) continue;
        const last = ch[ch.length - 1];
        if (Math.abs(last[1] - y) <= maxJump && last[0] === x - 1) {
          if (best < 0 || Math.abs(last[1] - y) < Math.abs(prev[best][2][prev[best][2].length - 1][1] - y)) best = ci;
        }
      }
      if (best >= 0) { used.add(best); const ch = prev[best][2]; ch.push([x, y]); cur.push([a, b, ch]); }
      else cur.push([a, b, [[x, y]]]);
    }
    prev.forEach((item, ci) => { if (!used.has(ci)) done.push(item); });
    prev = cur;
  }
  for (const it of prev) done.push(it);
  return done.filter((c) => c[2].length >= 9);
}

function spread(xs, hs) {
  if (xs.length < 2) return 0;
  const s0 = N.polyfitSlope(xs, hs);
  let best = 9e9;
  const step = (0.12 - -0.12) / 24;                 // numpy.linspace(-0.12, 0.12, 25) 와 같은 계산
  for (let k = 0; k < 25; k++) {
    const s = s0 + (k * step + -0.12);
    let mn = Infinity, mx = -Infinity;
    for (let i = 0; i < xs.length; i++) { const r = hs[i] - s * xs[i]; if (r < mn) mn = r; if (r > mx) mx = r; }
    best = Math.min(best, mx - mn);
  }
  return best;
}

function lineProfileBumps(img, hasAlpha, wHalf) {
  wHalf = wHalf || 7;
  const K0 = keysOf(img);
  const out = [];
  for (const transpose of [false, true]) {
    const w = transpose ? img.h : img.w, h = transpose ? img.w : img.h;
    let K = K0;
    if (transpose) { K = new Float64Array(w * h); for (let y = 0; y < img.h; y++) for (let x = 0; x < img.w; x++) K[x * w + y] = K0[y * img.w + x]; }
    for (const [a, b, ch] of chains(K, w, h)) {
      if (ch.length < 2 * wHalf + 1) continue;
      const xs = ch.map((p) => p[0]), hs = ch.map((p) => p[1]);
      for (let i = wHalf; i < ch.length - wHalf; i++) {
        if (xs[i + wHalf] - xs[i - wHalf] !== 2 * wHalf) continue;
        const wx = xs.slice(i - wHalf, i + wHalf + 1), wh = hs.slice(i - wHalf, i + wHalf + 1);
        if (spread(wx, wh) < 0.999) continue;
        const sx = wx.filter((_, k) => k !== wHalf), sh = wh.filter((_, k) => k !== wHalf);
        if (spread(sx, sh) >= 0.999) continue;
        if (Math.abs(N.polyfitSlope(sx, sh)) > 1.0) continue;
        const steps = new Set();
        for (let k = 1; k < wHalf; k++) steps.add(wh[k] - wh[k - 1]);
        for (let k = wHalf + 2; k < wh.length; k++) steps.add(wh[k] - wh[k - 1]);
        const d1 = wh[wHalf] - wh[wHalf - 1], d2 = wh[wHalf + 1] - wh[wHalf];
        if (steps.has(d1) && steps.has(d2)) continue;
        const ok = [];
        for (const dd of [-1, 1]) { const t = wh.slice(); t[wHalf] += dd; if (spread(wx, t) < 0.999) ok.push(dd); }
        if (ok.length !== 1) continue;
        const x = xs[i], y = hs[i];
        let py, nw, old;
        if (ok[0] === 1) { py = y + 1; nw = a; old = b; } else { py = y; nw = b; old = a; }
        if (!(py >= 0 && py < h) || K[py * w + x] !== old) continue;
        let fine = true;
        for (let yy = Math.max(0, py - 1); yy < Math.min(h, py + 2) && fine; yy++)
          for (let xx = Math.max(0, x - 1); xx < Math.min(w, x + 2); xx++) { const v = K[yy * w + xx]; if (v !== a && v !== b) { fine = false; break; } }
        if (!fine) continue;
        out.push(transpose ? [x, py, nw, old, old === 0 ? "edge_notch" : "edge_spur"] : [py, x, nw, old, old === 0 ? "edge_notch" : "edge_spur"]);
      }
    }
  }
  const seen = new Set(), uniq = [];
  for (const t of out) { const k = t[0] + "," + t[1]; if (!seen.has(k)) { seen.add(k); uniq.push(t); } }
  return uniq;
}

module.exports = { OOB, keysOf, keyToRgba, latticeMembers, repetitionIntentional, repeatedMask, smallComponents,
  singlePixelAnomalies, edgeBumps, colorStats, offPaletteGroups, enclosedHoles, paperFringe, canvasLines, seamDiscontinuities,
  denseMask, diagonalSpikesAndBoundaryOdd, edgeForeignSpecks, hardEdgeSegments, straightLineArtifacts, internalSeams,
  lineProfileBumps, premul };
