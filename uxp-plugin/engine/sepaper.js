// 분판(컬러 세퍼레이션) 감지 + 스캔 종이 제거 — Python separation.py / paper.py 와 같은 규칙
"use strict";
const N = require("./nd.js");

// ------------------------------------------------------------------ 분판
function coverage(doc, idxs, W, H) {
  const cov = new Int16Array(W * H);
  for (const idx of idxs) {
    const l = doc.layer(idx), img = doc.images.get(idx);
    const [x0, y0, x1, y1] = l.bbox;
    for (let y = Math.max(0, y0); y < Math.min(H, y1); y++) for (let x = Math.max(0, x0); x < Math.min(W, x1); x++) {
      if (img.d[((y - y0) * img.w + (x - x0)) * 4 + 3] > 0) cov[y * W + x]++;
    }
  }
  return cov;
}

function detectSeparation(doc, W, H) {
  const slots = [], names = [], colors = [];
  for (const l of doc.layers) {
    if (l.kind === "group" || !l.effective_visible) continue;
    if (l.kind !== "pixel" || !doc.images.has(l.index)) return [null, `보이는 비픽셀/읽기 불가 레이어: ${l.path}`];
    const img = doc.images.get(l.index), d = img.d;
    if (!l.has_alpha_channel) return [null, `투명 채널 없는 레이어: ${l.path}`];
    for (let i = 3; i < d.length; i += 4) if (d[i] !== 0 && d[i] !== 255) return [null, `반투명 픽셀이 있는 레이어: ${l.path}`];
    if (l.opacity !== 255 || !(l.fill_opacity === null || l.fill_opacity === 255) || l.blend_mode !== "NORMAL" ||
        l.clipping || l.has_mask || l.has_vector_mask || l.has_effects) return [null, `불투명도/블렌드/마스크/효과가 있는 레이어: ${l.path}`];
    if (!l.editable) return [null, `수정 불가 레이어: ${l.path}`];
    let first = null, multi = false;
    for (let i = 0; i < d.length; i += 4) {
      if (d[i + 3] !== 255) continue;
      if (!first) first = [d[i], d[i + 1], d[i + 2]];
      else if (d[i] !== first[0] || d[i + 1] !== first[1] || d[i + 2] !== first[2]) { multi = true; break; }
    }
    if (!first) continue;
    if (multi) return [null, `색이 2개 이상인 레이어: ${l.path}`];
    if (colors.some((c) => c[0] === first[0] && c[1] === first[1] && c[2] === first[2])) return [null, `같은 색 레이어가 2개 이상: ${l.path}`];
    slots.push(l.index); names.push(l.path); colors.push(first);
  }
  if (slots.length < 2) return [null, "분판 레이어가 2개 미만"];
  const cov = coverage(doc, slots, W, H);
  let overlaps = 0, holes = 0;
  for (let i = 0; i < W * H; i++) { if (cov[i] > 1) overlaps++; if (cov[i] === 0) holes++; }
  if (overlaps) return [null, `분판 레이어끼리 겹치는 픽셀 ${overlaps}개`];
  const label = new Int16Array(W * H).fill(-1);
  slots.forEach((idx, k) => {
    const l = doc.layer(idx), img = doc.images.get(idx);
    const [x0, y0, x1, y1] = l.bbox;
    for (let y = Math.max(0, y0); y < Math.min(H, y1); y++) for (let x = Math.max(0, x0); x < Math.min(W, x1); x++) {
      if (img.d[((y - y0) * img.w + (x - x0)) * 4 + 3] > 0) label[y * W + x] = k;
    }
  });
  const sep = {
    slots, names, colors, label, holes, overlaps: 0, W, H,
    virtualImg() {
      const d = new Uint8Array(W * H * 4);
      for (let i = 0; i < W * H; i++) {
        const k = label[i];
        if (k >= 0) { d[i * 4] = colors[k][0]; d[i * 4 + 1] = colors[k][1]; d[i * 4 + 2] = colors[k][2]; d[i * 4 + 3] = 255; }
      }
      return { w: W, h: H, d };
    },
    slotOfColor(rgb) { return colors.findIndex((c) => c[0] === rgb[0] && c[1] === rgb[1] && c[2] === rgb[2]); },
  };
  return [sep, "ok"];
}

// ------------------------------------------------------------------ 종이 제거
function estimatePaper(img) {
  const { w, h, d } = img;
  const rs = [], gs = [], bs = [];
  let present = 0;
  for (let i = 0; i < w * h; i++) {
    const a = d[i * 4 + 3];
    if (a > 0) present++;
    if (a !== 255) continue;
    const r = d[i * 4], g = d[i * 4 + 1], b = d[i * 4 + 2];
    const lum = 0.299 * r + 0.587 * g + 0.114 * b;
    if (lum >= 200 && Math.max(r, g, b) - Math.min(r, g, b) <= 30) { rs.push(r); gs.push(g); bs.push(b); }
  }
  if (rs.length < Math.max(200, 0.02 * present)) return null;
  return [Math.trunc(N.median(rs)), Math.trunc(N.median(gs)), Math.trunc(N.median(bs))];
}

function removePaper(img, paperRgb, opt) {
  const o = Object.assign({ inkThr: 30, softLo: 10, closeR: 3, band: 3, dustMaxArea: 40, keepEnclosedMaxFrac: 0.02, edgeBand: 4, inkFull: 90 }, opt || {});
  const { w, h, d } = img;
  if (!paperRgb) { paperRgb = estimatePaper(img); if (!paperRgb) return null; }
  // Python 은 float32 로 계산: 종이색·색 차이는 정수라 결과 동일
  const p = paperRgb;
  const n = w * h;
  const present = new Uint8Array(n), dist = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    present[i] = d[i * 4 + 3] > 0 ? 1 : 0;
    dist[i] = Math.max(Math.abs(d[i * 4] - p[0]), Math.abs(d[i * 4 + 1] - p[1]), Math.abs(d[i * 4 + 2] - p[2]));
  }
  const dEdge = N.edtToZero(present, w, h, false);
  let ink = new Uint8Array(n);
  const paperVals = [];
  for (let i = 0; i < n; i++) {
    if (present[i] && dist[i] > o.inkThr && dEdge[i] > o.edgeBand) ink[i] = 1;
    if (present[i] && dist[i] <= o.inkThr) paperVals.push(dist[i]);
  }
  let noise = 3.0;
  if (paperVals.length) {
    let m = 0; for (const v of paperVals) m += v; m /= paperVals.length;
    let s = 0; for (const v of paperVals) s += (v - m) * (v - m);
    noise = Math.sqrt(s / paperVals.length);
  }
  const lightThr = Math.max(12.0, 4.0 * noise);
  const allow = new Uint8Array(n);
  for (let i = 0; i < n; i++) allow[i] = (present[i] && dist[i] > lightThr && dEdge[i] > o.edgeBand) || ink[i] ? 1 : 0;
  ink = N.propagate(ink, allow, w, h);
  const closedC = N.closing(ink, w, h, N.diskOffsets(o.closeR));
  const closed = new Uint8Array(n);
  for (let i = 0; i < n; i++) closed[i] = closedC[i] || ink[i] ? 1 : 0;
  const filled = N.fillHoles(closed, w, h);
  const enclosed = new Uint8Array(n);
  let total = 0;
  for (let i = 0; i < n; i++) { enclosed[i] = filled[i] && !closed[i] && present[i] ? 1 : 0; total += present[i]; }
  total = Math.max(1, total);
  const LE = N.label(enclosed, w, h, false);
  const big = new Uint8Array(n);
  const keptRegions = [];
  for (let l = 1; l <= LE.n; l++) {
    const [y0, y1, x0, x1] = LE.boxes[l - 1];
    const bw = x1 - x0, bh = y1 - y0;
    const reg = new Uint8Array(bw * bh);
    let area = 0;
    for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) if (LE.lab[y * w + x] === l) { reg[(y - y0) * bw + (x - x0)] = 1; area++; }
    const dt = N.edtToZero(reg, bw, bh, true);
    let rIn = 0;
    for (let i = 0; i < bw * bh; i++) if (reg[i] && dt[i] > rIn) rIn = dt[i];
    const elongated = rIn < 0.2 * Math.sqrt(area);
    if (area > o.keepEnclosedMaxFrac * total || elongated) {
      for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) if (LE.lab[y * w + x] === l) big[y * w + x] = 1;
    } else keptRegions.push([x0, y0, x1, y1, area]);
  }
  let keep = new Uint8Array(n);
  for (let i = 0; i < n; i++) keep[i] = filled[i] && !big[i] && present[i] ? 1 : 0;
  // 먼지
  const L2 = N.label(keep, w, h, true);
  let dust = 0;
  const isDust = new Uint8Array(L2.n + 1);
  for (let l = 1; l <= L2.n; l++) if (L2.areas[l] <= o.dustMaxArea) { isDust[l] = 1; dust++; }
  if (dust) for (let i = 0; i < n; i++) if (isDust[L2.lab[i]]) keep[i] = 0;
  // 떨어진 가는 선
  const L3 = N.label(keep, w, h, true);
  let thinLines = 0;
  const thinKept = [];
  if (L3.n > 1) {
    let main = 0;
    for (let l = 1; l <= L3.n; l++) main = Math.max(main, L3.areas[l]);
    const dt = N.edtToZero(keep, w, h, false);
    const rmax = new Float64Array(L3.n + 1), nearCnt = new Float64Array(L3.n + 1);
    for (let i = 0; i < n; i++) {
      const l = L3.lab[i];
      if (!l) continue;
      if (dt[i] > rmax[l]) rmax[l] = dt[i];
      if (dEdge[i] <= 4 * o.edgeBand) nearCnt[l]++;
    }
    for (let l = 1; l <= L3.n; l++) {
      const area = L3.areas[l], r_ = rmax[l];
      const thin = area < main && r_ < 2.5 && area >= 12 * Math.pow(Math.max(r_, 0.5), 2) && area < 0.02 * total;
      if (!thin) continue;
      if (nearCnt[l] / area >= 0.5) {
        for (let i = 0; i < n; i++) if (L3.lab[i] === l) keep[i] = 0;
        thinLines++;
      } else {
        const [y0, y1, x0, x1] = L3.boxes[l - 1];
        thinKept.push([x0, y0, x1, y1, area]);
      }
    }
  }
  const outside = new Uint8Array(n), notOutside = new Uint8Array(n), notKeep = new Uint8Array(n);
  for (let i = 0; i < n; i++) { outside[i] = present[i] && !keep[i] ? 1 : 0; notOutside[i] = outside[i] ? 0 : 1; notKeep[i] = keep[i] ? 0 : 1; }
  const dOut = N.edtToZero(notOutside, w, h, false), dIn = N.edtToZero(notKeep, w, h, false);
  const out = new Uint8Array(d);
  let removed = 0, softened = 0;
  for (let i = 0; i < n; i++) {
    let soft = (keep[i] && dOut[i] <= o.band) || (outside[i] && dIn[i] <= o.band);
    // Python 과 같은 정밀도: strength 는 float32 로 계산, ramp 는 float64
    let s32 = Math.fround(Math.fround(dist[i] - o.softLo) / Math.fround(o.inkFull - o.softLo));
    s32 = Math.min(1, Math.max(0, s32));
    const ramp = keep[i] && ink[i] ? Math.min(1, Math.max(0, dOut[i] / (o.band + 1))) : 0;
    const strength = Math.max(s32, ramp);
    if (outside[i] && dEdge[i] <= o.edgeBand) soft = false;
    let alpha32 = keep[i] ? 1 : 0;
    if (soft) alpha32 = Math.fround(strength);
    alpha32 = Math.fround(alpha32 * Math.fround(d[i * 4 + 3] / 255));
    let aNew = roundHalfEven(Math.fround(alpha32 * 255));
    aNew = Math.min(255, Math.max(0, aNew));
    if (present[i]) out[i * 4 + 3] = aNew;
    if (soft && aNew > 0) {
      const af = Math.min(1, Math.max(1e-3, strength));
      for (let c = 0; c < 3; c++) {
        const v = p[c] - (p[c] - d[i * 4 + c]) / af;
        out[i * 4 + c] = Math.min(255, Math.max(0, roundHalfEven(v)));
      }
    }
    if (present[i] && aNew === 0) removed++;
    else if (soft && aNew > 0) softened++;
  }
  return { img: { w, h, d: out }, paperRgb: p, removedPx: removed, softenedPx: softened, dustComponents: dust,
           thinLineComponents: thinLines, thinLinesKept: thinKept, enclosedPaperRegions: keptRegions };
}

function roundHalfEven(v) {
  const f = Math.floor(v), r = v - f;
  if (r > 0.5) return f + 1;
  if (r < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
}

module.exports = { detectSeparation, coverage, estimatePaper, removePaper, roundHalfEven };
