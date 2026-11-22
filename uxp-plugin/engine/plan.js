// 클린업 실행 흐름 — Python pipeline.plan_cleanup + exchange.process_job 과 같은 규칙
// 입력: 플러그인이 Photoshop 에서 읽은 문서(요청 객체) → 출력: 레이어별 패치(바뀌는 영역 픽셀) + 검출 목록
"use strict";
const A = require("./analysis.js");
const E = require("./engines.js");
const S = require("./sepaper.js");
const { crc32 } = require("../lib/layers.js");

const { CERTAIN, UNCERTAIN, INTENTIONAL, UNSUPPORTED, INFO } = E;
const BLEND = { normal: "NORMAL", passThrough: "PASS_THROUGH", dissolve: "DISSOLVE", darken: "DARKEN", multiply: "MULTIPLY",
  colorBurn: "COLOR_BURN", linearBurn: "LINEAR_BURN", darkerColor: "DARKER_COLOR", lighten: "LIGHTEN", screen: "SCREEN",
  colorDodge: "COLOR_DODGE", linearDodge: "LINEAR_DODGE", lighterColor: "LIGHTER_COLOR", overlay: "OVERLAY", softLight: "SOFT_LIGHT",
  hardLight: "HARD_LIGHT", vividLight: "VIVID_LIGHT", linearLight: "LINEAR_LIGHT", pinLight: "PIN_LIGHT", hardMix: "HARD_MIX",
  difference: "DIFFERENCE", exclusion: "EXCLUSION", subtract: "SUBTRACT", divide: "DIVIDE", hue: "HUE", saturation: "SATURATION",
  color: "COLOR", luminosity: "LUMINOSITY" };

// ------------------------------------------------------------------ 문서 (Python MemoryDocument 와 동일)
// req: { document:{width,height,mode,bitsPerChannel,...}, layers:[{order,id,name,path,kind,...,bounds,components}], composite? }
// data: { layers: Map(order → Uint8Array raw), composite: Uint8Array|null }
function buildDocument(req, data) {
  const dd = req.document;
  const W = Number(dd.width), H = Number(dd.height);
  const mode = String(dd.mode || ""), bits = String(dd.bitsPerChannel || "");
  const reasons = [];
  if (!/RGB/i.test(mode)) reasons.push(`색상 모드 ${mode} (자동 수정은 RGB 만 지원)`);
  if (bits && !bits.includes("8")) reasons.push(`${bits} 문서 (자동 수정은 8비트만 지원 — 16/32비트 정밀도 손실 방지)`);
  const n = req.layers.length;
  const layers = [], images = new Map(), raw = new Map();
  for (const L of req.layers) {
    const order = Number(L.order), idx = n - 1 - order;
    const kind = String(L.kind || "pixel");
    const b = L.bounds || [0, 0, 0, 0];
    const comps = Number(L.components || 0);
    const locks = { transparency: !!L.transparentPixelsLocked, composite: !!L.pixelsLocked, position: !!L.positionLocked, all: !!L.allLocked };
    const info = {
      index: idx, order, layer_id: L.id === undefined ? null : L.id, name: L.name || "", path: L.path || "", kind,
      visible: L.visible !== false, effective_visible: L.effectiveVisible === undefined ? L.visible !== false : !!L.effectiveVisible,
      opacity: Math.round(Number(L.opacity === undefined ? 100 : L.opacity) * 2.55),
      fill_opacity: Math.round(Number(L.fillOpacity === undefined ? 100 : L.fillOpacity) * 2.55),
      blend_mode: BLEND[String(L.blendMode || "normal")] || String(L.blendMode || "").toUpperCase(),
      bbox: [b[0], b[1], b[2], b[3]], clipping: !!L.isClippingMask, has_mask: !!L.hasMask, has_vector_mask: false, has_effects: false,
      locks, has_alpha_channel: comps === 4, components: comps, unsupported_reasons: [],
    };
    const r = info.unsupported_reasons;
    r.push(...reasons);
    if (kind !== "pixel") r.push(`비픽셀 레이어(${kind})`);
    if (!L.file && !(data.layers && data.layers.has(order))) r.push("픽셀 데이터 없음(빈 레이어 또는 읽기 실패)");
    if (locks.all || locks.composite || locks.transparency) r.push("잠긴 레이어");
    if (info.has_mask) r.push("레이어 마스크 있음 (마스크 결합 결과 수정 미지원)");
    info.editable = r.length === 0;
    layers.push(info);
    if (data.layers && data.layers.has(order)) raw.set(idx, { L, buf: data.layers.get(order) });
  }
  layers.sort((a, b) => a.index - b.index);
  const byIndex = new Map(layers.map((l) => [l.index, l]));
  const doc = {
    W, H, layers, images, pixelEditSupported: reasons.length === 0, unsupportedReasons: reasons, req,
    layer(i) { return byIndex.get(i); },
    readImg(i) {
      const R = raw.get(i);
      if (!R || !doc.pixelEditSupported) return null;
      const b = R.L.bounds, w = b[2] - b[0], h = b[3] - b[1], c = Number(R.L.components);
      if (R.buf.length !== w * h * c) throw new Error("레이어 데이터 크기 불일치: " + R.L.path);
      if (c === 4) return { w, h, d: new Uint8Array(R.buf) };
      const d = new Uint8Array(w * h * 4);
      for (let k = 0; k < w * h; k++) { d[k * 4] = R.buf[k * c]; d[k * 4 + 1] = R.buf[k * c + 1]; d[k * 4 + 2] = R.buf[k * c + 2]; d[k * 4 + 3] = 255; }
      return { w, h, d };
    },
    // Python MemoryDocument.simple_composite: 보이는 픽셀 레이어 Normal 근사 합성 (float32 계산 재현)
    simpleComposite() {
      const acc = new Float32Array(W * H * 3).fill(255);
      for (const l of layers) {
        if (l.kind !== "pixel" || !l.effective_visible || !raw.has(l.index)) continue;
        const im = doc.readImg(l.index);
        if (!im) continue;
        const [x0, y0, x1, y1] = l.bbox;
        const op = Math.fround(l.opacity / 255);
        for (let y = Math.max(0, y0); y < Math.min(H, y1); y++) for (let x = Math.max(0, x0); x < Math.min(W, x1); x++) {
          const s = ((y - y0) * im.w + (x - x0)) * 4, t = (y * W + x) * 3;
          const al = Math.fround(Math.fround(im.d[s + 3] / 255) * op);
          const inv = Math.fround(1 - al);
          for (let k = 0; k < 3; k++) acc[t + k] = Math.fround(Math.fround(im.d[s + k] * al) + Math.fround(acc[t + k] * inv));
        }
      }
      const d = new Uint8Array(W * H * 4);
      for (let i = 0; i < W * H; i++) {
        for (let k = 0; k < 3; k++) d[i * 4 + k] = Math.min(255, Math.max(0, S.roundHalfEven(acc[i * 3 + k])));
        d[i * 4 + 3] = 255;
      }
      return { w: W, h: H, d };
    },
    compositeImg() {
      const C = req.composite;
      if (!C || !data.composite) return doc.simpleComposite();
      const c = Number(C.components), b = C.bounds || [0, 0, W, H], w = b[2] - b[0], h = b[3] - b[1];
      if (data.composite.length !== w * h * c) return doc.simpleComposite();
      const d = new Uint8Array(W * H * 4).fill(255);
      for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
        const s = (y * w + x) * c, t = ((y + b[1]) * W + x + b[0]) * 4;
        if (c === 4) {
          const al = Math.fround(data.composite[s + 3] / 255);
          for (let k = 0; k < 3; k++) {
            const v = Math.fround(Math.fround(data.composite[s + k] * al) + Math.fround(255 * Math.fround(1 - al)));
            d[t + k] = Math.min(255, Math.max(0, S.roundHalfEven(v)));
          }
        } else { d[t] = data.composite[s]; d[t + 1] = data.composite[s + 1]; d[t + 2] = data.composite[s + 2]; }
      }
      return { w: W, h: H, d };
    },
  };
  return doc;
}

// ------------------------------------------------------------------ 리핏 (Python repeat.seam_findings)
function seamFindings(img, left, top, W, H, mode, engineName, layer) {
  const out = [];
  if (W < 4 || H < 4) return out;
  const dirs = { none: [], horizontal: ["horizontal"], vertical: ["vertical"], both: ["horizontal", "vertical"] }[mode] || [];
  for (const d of dirs) {
    const lines = d === "horizontal" ? A.canvasLines(img, left, top, W, H, "col", [W - 2, W - 1, 0, 1])
                                     : A.canvasLines(img, left, top, W, H, "row", [H - 2, H - 1, 0, 1]);
    const { segs, stats } = A.seamDiscontinuities(lines);
    for (const [s0, s1, mx] of segs) {
      if (patternSeenElsewhere(img, left, top, W, H, d, s0, s1)) continue;
      const bounds = d === "horizontal" ? [W - 2, s0, W, s1] : [s0, H - 2, s1, H];
      const where = d === "horizontal" ? `오른쪽 끝 ↔ 왼쪽 시작, y=${s0}~${s1 - 1}` : `아래 끝 ↔ 위 시작, x=${s0}~${s1 - 1}`;
      const f = E.finding({ type: "repeat_discontinuity", engine: engineName, scope: layer ? "layer" : "composite", confidence: UNCERTAIN,
        proposed_action: "보고만 (리핏 자동 수정은 지원하지 않음)",
        evidence: Object.assign({ direction: d, where, length_px: s1 - s0, max_seam_diff: Math.round(mx * 10) / 10 }, stats),
        bounds, pdf_ref: "8쪽 모티프 리핏 / 10쪽 지무늬 리핏" });
      if (layer) { f.layer_index = layer.index; f.layer_name = layer.path; f.layer_id = layer.layer_id; }
      out.push(f);
    }
  }
  return out;
}

function patternSeenElsewhere(img, left, top, W, H, direction, s0, s1, k) {
  k = k || 8;
  const L = direction === "horizontal" ? W : H;
  if (L < 4 * k) return false;
  const key = (t, u) => {          // t: 줄(구간 안), u: 줄 위치
    const y = direction === "horizontal" ? t : u, x = direction === "horizontal" ? u : t;
    const ly = y - top, lx = x - left;
    if (ly < 0 || ly >= img.h || lx < 0 || lx >= img.w) return 0;
    const p = (ly * img.w + lx) * 4, a = img.d[p + 3];
    return a === 0 ? 0 : img.d[p] * 16777216 + img.d[p + 1] * 65536 + img.d[p + 2] * 256 + a;
  };
  let matched = 0;
  const nLines = s1 - s0;
  for (let t = s0; t < s1; t++) {
    const row = new Float64Array(L);
    for (let u = 0; u < L; u++) row[u] = key(t, u);
    const block = [];
    for (let u = L - k; u < L; u++) block.push(row[u]);
    for (let u = 0; u < k; u++) block.push(row[u]);
    let found = false;
    for (let s = 0; s + 2 * k <= L && !found; s++) {
      let ok = true;
      for (let q = 0; q < 2 * k; q++) if (row[s + q] !== block[q]) { ok = false; break; }
      if (ok) found = true;
    }
    if (found) matched++;
  }
  return nLines > 0 && matched / nLines >= 0.8;
}

const near = (fb, b) => fb[0] < b[2] + 2 && b[0] - 2 < fb[2] && fb[1] < b[3] + 2 && b[1] - 2 < fb[3];
const intersects = (b, r) => b[0] < r.x + r.w && r.x < b[2] && b[1] < r.y + r.h && r.y < b[3];

// ------------------------------------------------------------------ 메인
function planCleanup(doc, cfgIn, prog) {
  const cfg = E.makeConfig(cfgIn);
  prog = prog || (() => {});
  const engine = E.getEngine(cfg);
  const W = doc.W, H = doc.H;
  let findings = [];
  const layerImg = new Map();
  if (!doc.pixelEditSupported) findings.push(E.finding({ type: "document", engine: engine.name, scope: "document", confidence: UNSUPPORTED,
    proposed_action: "픽셀 분석/수정 건너뜀, 구조 정보만 보고", evidence: { reasons: doc.unsupportedReasons } }));
  if (doc.pixelEditSupported) for (const l of doc.layers) if (l.kind === "pixel") { const im = doc.readImg(l.index); if (im) doc.images.set(l.index, im); }

  let sep = null, sepReason = engine.name !== "apparel" ? "어패럴이 아님" : "설정에서 끔";
  if (engine.name === "apparel" && cfg.separation_mode === "auto" && doc.images.size) [sep, sepReason] = S.detectSeparation(doc, W, H);
  const sepSlots = new Set(sep ? sep.slots : []);

  doc.layers.forEach((layer, i) => {
    prog(0.05 + 0.6 * i / Math.max(1, doc.layers.length), "레이어 검사: " + layer.path);
    if (layer.kind === "group") return;
    if (layer.kind !== "pixel") {
      findings.push(engine.f(layer, { type: "unsupported_layer", confidence: UNSUPPORTED, proposed_action: "검사·수정 제외 (원본 그대로 유지)",
        evidence: { kind: layer.kind }, bounds: layer.bbox.slice() }));
      return;
    }
    const img = doc.images.get(layer.index);
    if (!img) { findings = findings.concat(engine.hygiene(layer, null)); return; }
    const approved = E.matchAny(cfg.approved_texture_layers, layer.name, layer.path);
    let lf = sepSlots.has(layer.index) ? engine.hygiene(layer, img) : engine.analyzeLayer(layer, img, approved);
    lf = lf.concat(seamFindings(img, layer.bbox[0], layer.bbox[1], W, H, cfg.repeat, engine.name, layer));
    const seams = lf.filter((f) => (f.type === "internal_seam" || f.type === "repeat_discontinuity") && f.bounds).map((f) => f.bounds);
    if (seams.length) for (const f of lf) if (f.confidence === CERTAIN && f.bounds && seams.some((b) => near(f.bounds, b))) {
      f.confidence = UNCERTAIN; f.fix = null; f.proposed_action = "보고만 (리핏 이음매 위 — 리핏을 먼저 맞춘 뒤 다시 검사)";
    }
    const prot = E.matchAny(cfg.protected_layers, layer.name, layer.path);
    for (const f of lf) {
      if (f.confidence !== CERTAIN) continue;
      if (prot) { f.confidence = INTENTIONAL; f.skip_reason = "보호 레이어(프로젝트 설정)"; }
      else if (f.bounds && (cfg.protected_regions || []).some((r) => intersects(f.bounds, r) && (!(r.layers && r.layers.length) || E.matchAny(r.layers, layer.name, layer.path)))) {
        f.confidence = INTENTIONAL; f.skip_reason = "보호 영역(프로젝트 설정)";
      } else if (!layer.editable) { f.confidence = UNSUPPORTED; f.skip_reason = layer.unsupported_reasons.join("; "); }
    }
    findings = findings.concat(lf);
    layerImg.set(layer.index, img);
  });

  const VIRTUAL = -100;
  if (sep) {
    prog(0.6, "분판 전체(색 지도) 검사");
    const vlayer = { index: VIRTUAL, layer_id: null, name: "[분판 합성]", path: "[분판 합성]", kind: "pixel", visible: true, effective_visible: true,
      opacity: 255, fill_opacity: 255, blend_mode: "NORMAL", bbox: [0, 0, W, H], clipping: false, has_mask: false, has_vector_mask: false,
      has_effects: false, locks: {}, has_alpha_channel: sep.holes > 0, editable: true, unsupported_reasons: [] };
    const skip = new Set(["layer_hygiene", "color_count", "layer_multi_color", "semi_alpha_edge"]);
    const vf = engine.analyzeLayer(vlayer, sep.virtualImg(), false).filter((f) => !skip.has(f.type));
    const seams = findings.filter((f) => (f.type === "internal_seam" || f.type === "repeat_discontinuity") && f.bounds).map((f) => f.bounds);
    for (const f of vf) {
      if (f.confidence !== CERTAIN) continue;
      if (f.bounds && seams.some((b) => near(f.bounds, b))) { f.confidence = UNCERTAIN; f.fix = null; f.proposed_action = "보고만 (리핏 이음매 위 — 리핏을 먼저 맞춘 뒤 다시 검사)"; }
      else if (f.bounds && (cfg.protected_regions || []).some((r) => intersects(f.bounds, r))) { f.confidence = INTENTIONAL; f.skip_reason = "보호 영역(프로젝트 설정)"; }
    }
    findings.push(E.finding({ type: "separation_info", engine: engine.name, scope: "document", confidence: INFO,
      proposed_action: "분판 모드: 분판 전체를 하나의 색 지도로 검사하고, 1픽셀 수정은 원래 분판 레이어에서 빼서 새 색의 분판 레이어로 옮김 (구멍·겹침 생성 금지)",
      evidence: { layers: sep.names, colors: sep.colors.map(E.toHex), uncovered_pixels: sep.holes, overlaps: 0 } }));
    findings = findings.concat(vf);
  } else if (engine.name === "apparel") {
    findings.push(E.finding({ type: "separation_info", engine: engine.name, scope: "document", confidence: INFO,
      proposed_action: "분판 모드 아님 → 레이어별 검사", evidence: { reason: sepReason } }));
  }

  if (cfg.remove_paper && engine.name === "interior") {
    const paper = cfg.paper_color ? E.parseHex(cfg.paper_color) : null;
    for (const [idx, img] of Array.from(layerImg.entries())) {
      const l = doc.layer(idx);
      if (!l.editable || !l.has_alpha_channel || !l.effective_visible || E.matchAny(cfg.protected_layers, l.name, l.path)) continue;
      const pr = S.removePaper(img, paper);
      if (!pr) continue;
      const ys = [], xs = [], news = [];
      for (let k = 0; k < img.w * img.h; k++) {
        const p = k * 4;
        if (pr.img.d[p] !== img.d[p] || pr.img.d[p + 1] !== img.d[p + 1] || pr.img.d[p + 2] !== img.d[p + 2] || pr.img.d[p + 3] !== img.d[p + 3]) {
          ys.push((k / img.w) | 0); xs.push(k % img.w); news.push([pr.img.d[p], pr.img.d[p + 1], pr.img.d[p + 2], pr.img.d[p + 3]]);
        }
      }
      if (!ys.length) continue;
      findings.unshift(engine.f(l, { type: "paper_removed", confidence: CERTAIN,
        proposed_action: "바깥 종이 → 투명, 모티브 외곽은 물감 농도로 부드럽게 + 종이색 테두리 제거",
        evidence: { paper_color: E.toHex(pr.paperRgb), removed_px: pr.removedPx, softened_edge_px: pr.softenedPx, dust_removed: pr.dustComponents,
                    thin_lines_removed: pr.thinLineComponents, "enclosed_paper_kept(x0,y0,x1,y1,area)": pr.enclosedPaperRegions.slice(0, 30) },
        bounds: l.bbox.slice(), pdf_ref: "기준: 스캔한 모티브의 배경이 외곽에 남아 있을 때 / 8쪽 / 12쪽", fix: { ys, xs, news } }));
      for (const [x0, y0, x1, y1, area] of pr.thinLinesKept.slice(0, 50)) findings.push(engine.f(l, { type: "line_artifact", confidence: UNCERTAIN,
        proposed_action: "종이 제거 후 남은 떨어진 가는 선 — 줄기·꽃술인지 자국인지 확인 필요(지우지 않음)", evidence: { area_px: area },
        bounds: [x0 + l.bbox[0], y0 + l.bbox[1], x1 + l.bbox[0], y1 + l.bbox[1]], pdf_ref: "8쪽 정리 안 된 부분" }));
    }
  }

  if (engine.name === "apparel" && cfg.one_color_per_layer) {
    const lc = new Map();
    for (const [idx, img] of layerImg) {
      const l = doc.layer(idx);
      if (!(l.effective_visible && l.has_alpha_channel)) continue;
      const set = new Set();
      for (let k = 0; k < img.w * img.h; k++) if (img.d[k * 4 + 3] === 255) set.add(img.d[k * 4] + "," + img.d[k * 4 + 1] + "," + img.d[k * 4 + 2]);
      if (set.size) lc.set(l.path, Array.from(set).slice(0, 4096));
    }
    findings = findings.concat(engine.analyzeSeparation(lc));
  }

  prog(0.68, "합성 결과 검사");
  const merged = doc.compositeImg ? doc.compositeImg() : null;
  if (merged) {
    findings = findings.concat(engine.analyzeComposite(merged));
    findings = findings.concat(seamFindings(merged, 0, 0, W, H, cfg.repeat, engine.name, null));
  }

  // ---------------------------------------------------------------- 수정 적용 (계획)
  prog(0.72, "안전한 수정 계획");
  const expectedNew = new Map();
  const fixTypes = engine.fixTypes;
  const byLayer = new Map();
  for (const f of findings) {
    if (f.confidence === CERTAIN && f.fix && f.layer_index !== null && f.layer_index !== undefined) {
      if (!cfg.auto_fix) f.skip_reason = "검수 전용 모드(auto_fix=false)";
      else if (!fixTypes.has(f.type)) f.skip_reason = `${engine.name} 엔진에서 자동 수정하지 않는 유형`;
      else { if (!byLayer.has(f.layer_index)) byLayer.set(f.layer_index, []); byLayer.get(f.layer_index).push(f); }
    }
  }
  const palette = new Set((cfg.allowed_colors || []).map((c) => E.parseHex(c).join(",")));
  const vfixes = byLayer.get(VIRTUAL) || [];
  byLayer.delete(VIRTUAL);
  if (sep && vfixes.length) applySeparationFixes(doc, sep, vfixes, cfg, W, H, expectedNew);
  for (const [idx, fl] of byLayer) {
    const layer = doc.layer(idx), orig = layerImg.get(idx);
    const nw = new Uint8Array(orig.d);
    const touched = new Uint8Array(orig.w * orig.h);
    const applied = [];
    for (const f of fl) {
      const fx = f.fix;
      if (fx.ys.some((y, k) => touched[y * orig.w + fx.xs[k]])) { f.skip_reason = "다른 수정과 픽셀 충돌 → 건너뜀"; continue; }
      if (!layer.has_alpha_channel && fx.news.some((c) => c[3] !== 255)) { f.skip_reason = "투명 채널 없는 레이어(배경)에서 투명 처리 불가"; continue; }
      fx.ys.forEach((y, k) => { const p = (y * orig.w + fx.xs[k]) * 4; nw.set(fx.news[k], p); touched[y * orig.w + fx.xs[k]] = 1; });
      applied.push(f);
    }
    const changed = new Uint8Array(orig.w * orig.h);
    let nChanged = 0;
    for (let k = 0; k < orig.w * orig.h; k++) {
      const p = k * 4;
      const diff = nw[p] !== orig.d[p] || nw[p + 1] !== orig.d[p + 1] || nw[p + 2] !== orig.d[p + 2] || nw[p + 3] !== orig.d[p + 3];
      if (diff && !(nw[p + 3] === 0 && orig.d[p + 3] === 0)) { changed[k] = 1; nChanged++; }
    }
    const limit = Math.max(64, Math.trunc(cfg.max_layer_change_ratio * orig.h * orig.w));
    let reason = null;
    if (nChanged > limit && !applied.some((f) => f.type === "paper_removed")) reason = `레이어 변경량 ${nChanged}px 가 안전 한도 ${limit}px 초과 → 이 레이어 수정 전부 취소`;
    if (!reason && engine.name === "apparel") {
      const existing = new Set();
      for (let k = 0; k < orig.w * orig.h; k++) if (orig.d[k * 4 + 3] > 0) existing.add(orig.d[k * 4] + "," + orig.d[k * 4 + 1] + "," + orig.d[k * 4 + 2]);
      let bad = 0;
      const seen = new Set();
      for (let k = 0; k < orig.w * orig.h; k++) if (changed[k] && nw[k * 4 + 3] > 0) {
        const c = nw[k * 4] + "," + nw[k * 4 + 1] + "," + nw[k * 4 + 2];
        if (!existing.has(c) && !palette.has(c) && !seen.has(c)) { seen.add(c); bad++; }
      }
      if (bad) reason = `수정 결과에 기존에 없던 색 ${bad}개 → 이 레이어 수정 취소`;
    }
    if (reason) { for (const f of applied) f.skip_reason = reason; continue; }
    for (const f of applied) {
      f.applied = true;
      f.changed_pixel_count = f.fix.ys.reduce((s, y, k) => s + changed[y * orig.w + f.fix.xs[k]], 0);
    }
    if (nChanged) expectedNew.set(idx, { w: orig.w, h: orig.h, d: nw });
  }
  return { engine, findings, layerImg, expectedNew, sep, sepReason, W, H };
}

function applySeparationFixes(doc, sep, vfixes, cfg, W, H, expectedNew) {
  const label = new Int16Array(sep.label);
  const touched = new Uint8Array(W * H);
  const moves = [];
  for (const f of vfixes) {
    const fx = f.fix;
    if (fx.ys.some((y, k) => touched[y * W + fx.xs[k]])) { f.skip_reason = "다른 수정과 픽셀 충돌 → 건너뜀"; continue; }
    const mv = [];
    let why = null;
    for (let k = 0; k < fx.ys.length; k++) {
      const y = fx.ys[k], x = fx.xs[k], nv = fx.news[k];
      const oldK = label[y * W + x];
      let newK = nv[3] === 0 ? -1 : sep.slotOfColor(nv);
      if (newK === null || newK === undefined || (nv[3] !== 0 && newK < 0)) { why = "새 색이 어느 분판 레이어 색과도 같지 않음"; break; }
      if (newK === -1 && sep.holes === 0) { why = "분판이 캔버스를 빈틈없이 채우는 파일에서 구멍을 만들 수 없음"; break; }
      for (const kk of [oldK, newK]) {
        if (kk >= 0) {
          const l = doc.layer(sep.slots[kk]);
          if (!(l.bbox[0] <= x && x < l.bbox[2] && l.bbox[1] <= y && y < l.bbox[3])) why = `대상 분판 레이어(${l.path}) 경계 밖 픽셀 — 레이어 크기는 바꾸지 않음`;
          if (E.matchAny(cfg.protected_layers, l.name, l.path)) why = "보호 레이어(프로젝트 설정)와 관련된 픽셀";
        }
      }
      if (why) break;
      mv.push([y, x, oldK, newK]);
    }
    if (why) { f.skip_reason = why; continue; }
    for (const [y, x, , n] of mv) { label[y * W + x] = n; touched[y * W + x] = 1; }
    moves.push([f, mv]);
  }
  let nChanged = 0;
  for (let i = 0; i < W * H; i++) if (label[i] !== sep.label[i]) nChanged++;
  const limit = Math.max(64, Math.trunc(cfg.max_layer_change_ratio * W * H));
  if (nChanged > limit) { for (const [f] of moves) f.skip_reason = `분판 변경량 ${nChanged}px 가 안전 한도 ${limit}px 초과 → 분판 수정 전부 취소`; return; }
  const news = new Map(sep.slots.map((idx) => { const im = doc.images.get(idx); return [idx, { w: im.w, h: im.h, d: new Uint8Array(im.d) }]; }));
  for (const [f, mv] of moves) {
    f.applied = true; f.changed_pixel_count = mv.length;
    const tr = [];
    for (const [y, x, o, n] of mv) {
      if (o >= 0) { const l = doc.layer(sep.slots[o]); const im = news.get(l.index); im.d[((y - l.bbox[1]) * im.w + (x - l.bbox[0])) * 4 + 3] = 0; }
      if (n >= 0) { const l = doc.layer(sep.slots[n]); const im = news.get(l.index); im.d.set([...sep.colors[n], 255], ((y - l.bbox[1]) * im.w + (x - l.bbox[0])) * 4); }
      tr.push([o >= 0 ? sep.names[o] : "(없음)", n >= 0 ? sep.names[n] : "(없음)"]);
    }
    f.evidence.moved_from_to = tr.slice(0, 5);
  }
  for (const idx of sep.slots) {
    const a = doc.images.get(idx).d, b = news.get(idx).d;
    let same = true;
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) { same = false; break; }
    if (!same) expectedNew.set(idx, news.get(idx));
  }
}

// ------------------------------------------------------------------ 패치 (Python exchange.process_job 과 동일)
function trimmedHash(img, comps, left, top) {
  const { w, h, d } = img;
  if (comps !== 4) {
    const out = new Uint8Array(w * h * 3);
    for (let k = 0; k < w * h; k++) { out[k * 3] = d[k * 4]; out[k * 3 + 1] = d[k * 4 + 1]; out[k * 3 + 2] = d[k * 4 + 2]; }
    return { hash: crc32(out), bounds: [left, top, left + w, top + h] };
  }
  let x0 = w, y0 = h, x1 = -1, y1 = -1;
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) if (d[(y * w + x) * 4 + 3] > 0) { if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
  if (x1 < 0) return { hash: null, bounds: null };
  const tw = x1 - x0 + 1, th = y1 - y0 + 1, out = new Uint8Array(tw * th * 4);
  for (let y = 0; y < th; y++) for (let x = 0; x < tw; x++) {
    const s = ((y + y0) * w + x + x0) * 4, t = (y * tw + x) * 4;
    if (d[s + 3] === 0) continue;
    out[t] = d[s]; out[t + 1] = d[s + 1]; out[t + 2] = d[s + 2]; out[t + 3] = d[s + 3];
  }
  return { hash: crc32(out), bounds: [left + x0, top + y0, left + x1 + 1, top + y1 + 1] };
}

function makePatches(doc, plan) {
  const patches = [];
  for (const [idx, nw] of Array.from(plan.expectedNew.entries()).sort((a, b) => a[0] - b[0])) {
    const l = doc.layer(idx), orig = doc.images.get(idx), comps = l.components;
    let x0 = nw.w, y0 = nw.h, x1 = -1, y1 = -1, n = 0;
    for (let y = 0; y < nw.h; y++) for (let x = 0; x < nw.w; x++) {
      const p = (y * nw.w + x) * 4;
      const va = nw.d[p + 3] === 0, vb = orig.d[p + 3] === 0;
      const diff = va !== vb || (!va && (nw.d[p] !== orig.d[p] || nw.d[p + 1] !== orig.d[p + 1] || nw.d[p + 2] !== orig.d[p + 2] || nw.d[p + 3] !== orig.d[p + 3]));
      if (diff) { n++; if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
    }
    if (!n) continue;
    const rw = x1 - x0 + 1, rh = y1 - y0 + 1, region = new Uint8Array(rw * rh * comps);
    for (let y = 0; y < rh; y++) for (let x = 0; x < rw; x++) {
      const s = ((y + y0) * nw.w + x + x0) * 4, t = (y * rw + x) * comps;
      for (let c = 0; c < comps; c++) region[t + c] = nw.d[s + c];
    }
    const th = trimmedHash(nw, comps, l.bbox[0], l.bbox[1]);
    patches.push({ order: l.order, id: l.layer_id, path: l.path, left: l.bbox[0] + x0, top: l.bbox[1] + y0, width: rw, height: rh,
                   components: comps, changed_pixels: n, expected_bounds: th.bounds, expected_hash: th.hash, data: region });
  }
  return patches;
}

function summarize(plan, patches, kind) {
  const counts = {}, byType = {};
  for (const f of plan.findings) {
    counts[f.confidence] = (counts[f.confidence] || 0) + 1;
    byType[f.type] = byType[f.type] || {};
    byType[f.type][f.confidence] = (byType[f.type][f.confidence] || 0) + 1;
  }
  const unresolved = plan.findings.filter((f) => f.confidence === UNCERTAIN || f.confidence === UNSUPPORTED || (f.confidence === CERTAIN && !f.applied));
  return {
    kind, engine: plan.engine.name, applied_fixes: plan.findings.filter((f) => f.applied).length,
    changed_pixels_total: patches.reduce((s, p) => s + p.changed_pixels, 0), patched_layers: patches.map((p) => p.path),
    separation_mode: !!plan.sep, findings_by_confidence: counts, findings_by_type: byType, unresolved_count: unresolved.length, unresolved,
  };
}

function findingForReport(f) {
  return { id: f.id, type: f.type, type_label: E.TYPE_LABELS[f.type] || f.type, engine: f.engine, scope: f.scope, layer_index: f.layer_index,
           layer_name: f.layer_name, layer_id: f.layer_id, bounds: f.bounds, confidence: f.confidence, confidence_label: E.CONF_LABELS[f.confidence],
           evidence: f.evidence, proposed_action: f.proposed_action, pdf_ref: f.pdf_ref, applied: f.applied,
           changed_pixel_count: f.changed_pixel_count, skip_reason: f.skip_reason };
}

// 한 번에: 요청 + 픽셀 → 결과 (Python exchange.process_job 과 같은 모양)
function runCleanup(req, data, kind, prog) {
  const doc = buildDocument(req, data);
  const cfg = Object.assign({}, req.config || {});
  if (kind === "recheck") cfg.auto_fix = false;
  const plan = planCleanup(doc, cfg, prog);
  const patches = makePatches(doc, plan);
  const summary = summarize(plan, patches, kind);
  return {
    status: "ok", kind, summary: Object.assign({}, summary, { unresolved: undefined }),
    skipped_layers: doc.layers.filter((l) => !l.editable && l.kind !== "group").map((l) => ({ path: l.path, kind: l.kind, reasons: l.unsupported_reasons })),
    patches, unresolved: summary.unresolved.slice(0, 3000).map(findingForReport), findings: plan.findings.slice(0, 5000).map(findingForReport),
    _plan: plan, _doc: doc,
  };
}

module.exports = { buildDocument, planCleanup, makePatches, runCleanup, seamFindings, findingForReport };
