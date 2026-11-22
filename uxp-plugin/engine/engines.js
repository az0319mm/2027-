// 판정 엔진 (공통 / 어패럴 / 인테리어) — Python psd_cleanup/engines/*.py 와 같은 규칙
"use strict";
const A = require("./analysis.js");
const N = require("./nd.js");

const CERTAIN = "certain", UNCERTAIN = "uncertain", INTENTIONAL = "intentional", UNSUPPORTED = "unsupported", INFO = "info";
const CONF_LABELS = { certain: "확실", uncertain: "불확실(검토 필요)", intentional: "의도적(보존)", unsupported: "지원 불가", info: "정보" };
const TYPE_LABELS = {
  isolated_speck: "고립 점(원픽셀/잔여 점)", odd_pixel: "1픽셀 잘못 칠한 부분", pinhole: "1픽셀 구멍", small_hole: "작은 구멍/단절",
  edge_spur: "경계 1픽셀 돌출", edge_notch: "경계 1픽셀 패임", off_palette_edge: "경계 혼합색(허용 색 외)", off_palette: "허용 색 목록 밖 색",
  color_count: "도수(색 수)", rare_color: "희소 색(경계 혼색 의심)", semi_alpha_edge: "반투명 경계 픽셀", layer_multi_color: "분판 레이어에 복수 색",
  faint_residue: "덜 지워진 반투명 잔여", paper_fringe: "스캔 종이색 외곽 잔여", scan_background: "스캔 배경 미제거 의심",
  repeat_discontinuity: "리핏 연결부 불연속", internal_seam: "캔버스 내부 타일 이음매 어긋남", separation_mismatch: "분판 구성 불일치",
  separation_info: "분판 모드", paper_removed: "스캔 종이 배경 제거(사용자 요청)", edge_foreign_speck: "외곽 이질 점(도장툴 흔적 의심)",
  hard_edge: "외곽 계단(매끄럽지 않음)", line_artifact: "1px 직선 자국", layer_hygiene: "레이어 구조 점검",
  unsupported_layer: "지원하지 않는 레이어", document: "문서 정보",
};

const DEFAULT_CONFIG = {
  work_type: "apparel", repeat: "none", expected_color_count: null, allowed_colors: [], one_color_per_layer: false,
  separation_mode: "auto", interior_style: "digital", paper_color: null, remove_paper: false, protected_layers: [],
  approved_texture_layers: [], protected_regions: [], auto_fix: true, fix_types: null, fix_repeats: false,
  repeat_min: 3, max_layer_change_ratio: 0.005,
};
function makeConfig(c) { return Object.assign({}, DEFAULT_CONFIG, c || {}); }

const hex2 = (v) => ("0" + (v | 0).toString(16)).slice(-2).toUpperCase();
function toHex(rgb) { return "#" + hex2(rgb[0]) + hex2(rgb[1]) + hex2(rgb[2]); }
function parseHex(s) {
  const t = String(s).trim().replace(/^#/, "");
  if (t.length !== 6) throw new Error("색상은 #RRGGBB 형식이어야 합니다: " + s);
  return [parseInt(t.slice(0, 2), 16), parseInt(t.slice(2, 4), 16), parseInt(t.slice(4, 6), 16)];
}
// fnmatch.fnmatchcase
function fnmatch(name, pat) {
  let re = "^";
  for (const ch of pat) {
    if (ch === "*") re += ".*";
    else if (ch === "?") re += ".";
    else re += ch.replace(/[.+^${}()|[\]\\/-]/g, "\\$&");
  }
  return new RegExp(re + "$").test(name);
}
const matchAny = (pats, ...names) => (pats || []).some((p) => names.some((n) => fnmatch(n, p)));
const DEFAULT_NAME_RE = /^(Layer|레이어|图层|レイヤー|Ebene|Calque)\s*\d+(\s*(copy|복사|拷贝|のコピー).*)?$/i;

let FID = 1;
function finding(o) {
  return Object.assign({ id: "F" + String(FID++).padStart(5, "0"), evidence: {}, bounds: null, layer_index: null, layer_name: null,
    layer_id: null, pdf_ref: "", fix: null, applied: false, changed_pixel_count: 0, skip_reason: "" }, o);
}

class BaseEngine {
  constructor(cfg) { this.cfg = cfg; }
  get fixTypes() {
    const d = new Set(this.defaultFixTypes);
    if (this.cfg.fix_types) return new Set(this.cfg.fix_types.filter((t) => d.has(t)));
    return d;
  }
  f(layer, o) {
    const r = finding(Object.assign({ engine: this.name, scope: layer ? "layer" : "composite" }, o));
    if (layer) { r.layer_index = layer.index; r.layer_name = layer.path; r.layer_id = layer.layer_id; }
    return r;
  }
  static bounds(layer, x0, y0, x1, y1) {
    const ox = layer ? layer.bbox[0] : 0, oy = layer ? layer.bbox[1] : 0;
    return [x0 + ox, y0 + oy, x1 + ox, y1 + oy];
  }
  specks(layer, img, o) {
    const { maxArea, autoMaxArea, autoMinGap, excludeAlphaLe, exactRepeatRule } = o;
    const pdfRef = o.pdfRef || "1쪽 원픽셀의 점 / 5쪽 의도적 반복";
    if (!layer.has_alpha_channel) return [];
    const out = [];
    const comps = A.smallComponents(img, maxArea);
    const repeated = (c) => (exactRepeatRule ? c.exactRepeat : c.similarCount >= this.cfg.repeat_min);
    const exc = excludeAlphaLe === undefined || excludeAlphaLe === null ? null : excludeAlphaLe;
    const nLoose = comps.filter((c) => c.gap >= autoMinGap && c.area <= Math.max(autoMaxArea, 1) && !repeated(c) &&
                                       (exc === null || c.maxAlpha > exc)).length;
    const nTexture = exactRepeatRule ? comps.filter((c) => c.repeatReason === "texture").length : 0;
    const textureLike = nLoose >= 20 || nTexture > 0;
    for (const c of comps) {
      if (exc !== null && c.maxAlpha <= exc) continue;
      if (c.gap < 1) continue;
      const ev = { area: c.area, gap_px: c.gap, max_alpha: c.maxAlpha, color: toHex(c.meanRgb), similar_components: c.similarCount,
                   layer_loose_specks: nLoose };
      let conf, act;
      if (repeated(c)) { conf = INTENTIONAL; act = "보존 (같은 형태의 점이 반복 사용됨 → 의도적 반복으로 판단)"; }
      else if (textureLike) { conf = UNCERTAIN; act = `보존 (설명 안 되는 고립 점 ${nLoose}개 / 같은 점 대량 반복 ${nTexture}개 → 질감·흩뿌림일 수 있어 일괄 삭제 금지)`; }
      else if (c.area <= autoMaxArea && c.gap >= autoMinGap) { conf = CERTAIN; act = "투명 처리(alpha=0)"; }
      else { conf = UNCERTAIN; act = "보존, 사람이 확인 필요"; }
      let fix = null;
      if (conf === CERTAIN || conf === INTENTIONAL) {
        fix = { ys: c.ys, xs: c.xs, news: c.ys.map((y, k) => { const p = (y * img.w + c.xs[k]) * 4; return [img.d[p], img.d[p + 1], img.d[p + 2], 0]; }) };
      }
      const [x0, y0, x1, y1] = c.bbox;
      out.push(this.f(layer, { type: "isolated_speck", confidence: conf, proposed_action: act, evidence: ev,
                               bounds: BaseEngine.bounds(layer, x0, y0, x1, y1), pdf_ref: pdfRef, fix }));
    }
    return out;
  }
  pixelAnomalies(layer, img, certainAllowed, minDiff) {
    minDiff = minDiff || 1;
    const out = [];
    for (const c of A.singlePixelAnomalies(img, this.cfg.repeat_min, layer.has_alpha_channel)) {
      if (c.kind === "odd_pixel" && Math.max(...c.old.map((v, i) => Math.abs(v - c.new[i]))) < minDiff) continue;
      const ev = { old: c.old, new: c.new, same_neighborhood_repeats: c.repeatCount };
      let conf, act;
      if (!certainAllowed) { conf = UNCERTAIN; act = "보존 (질감 표현일 수 있음)"; }
      else if (c.intentional) { conf = INTENTIONAL; act = "보존 (같은 1픽셀 패턴 반복 → 5쪽 통과 사례와 같은 의도적 반복)"; }
      else { conf = CERTAIN; act = `주변 8픽셀과 같은 색 ${toHex(c.new)} 으로 채움`; }
      const fix = conf === CERTAIN || conf === INTENTIONAL ? { ys: [c.y], xs: [c.x], news: [c.new] } : null;
      out.push(this.f(layer, { type: c.kind, confidence: conf, proposed_action: act, evidence: ev,
                               bounds: BaseEngine.bounds(layer, c.x, c.y, c.x + 1, c.y + 1), pdf_ref: "3~4쪽 1픽셀 잘못 칠함 / 5쪽 의도적 1픽셀", fix }));
    }
    return out;
  }
  hygiene(layer, img) {
    const notes = [];
    if (!layer.visible) notes.push("숨김 레이어");
    if (DEFAULT_NAME_RE.test(String(layer.name).trim())) notes.push("기본 이름(정리 필요 여부 확인)");
    if (layer.kind === "pixel" && img) {
      let any = false;
      for (let i = 3; i < img.d.length; i += 4) if (img.d[i] > 0) { any = true; break; }
      if (!any) notes.push("완전히 투명한 빈 레이어");
    }
    if (layer.kind === "pixel" && (layer.bbox[2] - layer.bbox[0]) * (layer.bbox[3] - layer.bbox[1]) === 0) notes.push("픽셀 없는 빈 레이어");
    if (layer.clipping) notes.push("클리핑 마스크 레이어");
    if (layer.has_effects) notes.push("레이어 효과 있음 (픽셀 수정 시 효과도 따라 변함)");
    if (!notes.length) return [];
    return [this.f(layer, { type: "layer_hygiene", confidence: INFO, proposed_action: "자동 삭제/변경하지 않음 (보고만)",
                            evidence: { notes }, bounds: layer.bbox.slice() })];
  }
  densityGuard(layer, findings, h, w) {
    const T = new Set(["isolated_speck", "odd_pixel", "pinhole", "edge_spur", "edge_notch"]);
    const cands = findings.filter((f) => T.has(f.type) && f.bounds && f.confidence !== INFO);
    if (cands.length < 5) return;
    const ox = layer.bbox[0], oy = layer.bbox[1];
    const pts = cands.map((f) => [Math.min(Math.max(f.bounds[1] - oy, 0), h - 1), Math.min(Math.max(f.bounds[0] - ox, 0), w - 1)]);
    A.denseMask(pts, h, w).forEach((dd, i) => {
      const f = cands[i];
      if (dd && f.confidence === CERTAIN) {
        f.confidence = UNCERTAIN; f.fix = null;
        f.proposed_action = "보고만 (1픽셀 후보가 한곳에 밀집 → 점묘 등 의도 표현일 수 있음, 6쪽)";
        f.evidence.dense_cluster = true;
      }
    });
  }
  internalSeamFindings(layer, img) {
    return A.internalSeams(img).map(([d, pos, a0, a1, n]) => {
      const b = d === "vertical" ? BaseEngine.bounds(layer, pos - 1, 0, pos + 1, img.h) : BaseEngine.bounds(layer, 0, pos - 1, img.w, pos + 1);
      return this.f(layer, { type: "internal_seam", confidence: UNCERTAIN,
        proposed_action: "보고만 (캔버스 안쪽 타일 이음매가 어긋남 의심 — 자동 수정 안 함)",
        evidence: { direction: d, position: pos + (d === "vertical" ? layer.bbox[0] : layer.bbox[1]), straight_cut_segments: n, segment_span: [a0, a1] },
        bounds: b, pdf_ref: "10쪽 지무늬 리핏 / 8쪽 모티프 리핏" });
    });
  }
}

class ApparelEngine extends BaseEngine {
  get name() { return "apparel"; }
  get defaultFixTypes() { return ["isolated_speck", "odd_pixel", "pinhole", "edge_spur", "edge_notch", "off_palette_edge"]; }
  get palette() { return this.cfg.allowed_colors && this.cfg.allowed_colors.length ? this.cfg.allowed_colors.map(parseHex) : null; }

  analyzeLayer(layer, img, approvedTexture) {
    let out = [];
    out = out.concat(this.hygiene(layer, img));
    const cs = A.colorStats(img, 8);
    const flat = cs.n <= 256;
    if (!flat) out.push(this.f(layer, { type: "color_count", confidence: UNCERTAIN,
      proposed_action: "연속톤(색 수 많음) 레이어 → 어패럴 1픽셀/경계 규칙의 자동 수정을 적용하지 않고 보고만 함 (분판·인덱스 정리 후 다시 실행 권장)",
      evidence: { unique_opaque_colors: cs.n, flat_limit: 256 }, bounds: layer.bbox.slice(), pdf_ref: "1쪽 Index 도수 / 6쪽 분판" }));
    out = out.concat(this.specks(layer, img, { maxArea: 4, autoMaxArea: 2, autoMinGap: 3, exactRepeatRule: true }));
    if (!approvedTexture) {
      out = out.concat(this.pixelAnomalies(layer, img, flat));
      for (const c of A.edgeBumps(img, this.cfg.repeat_min, layer.has_alpha_channel)) {
        const ev = { direction_to_body: c.direction, old: c.old, new: c.new, same_neighborhood_repeats: c.repeatCount,
                     straight_edge_len: c.plateau, convex_apex: c.apex };
        let conf, act;
        if (c.apex) { conf = UNCERTAIN; act = "보고만 (곡선 정점의 1px — 정상적인 픽셀 계단일 수 있음)"; }
        else if (!flat) { conf = UNCERTAIN; act = "보고만 (연속톤 레이어)"; }
        else if (c.intentional) { conf = INTENTIONAL; act = "보존 (같은 모양이 반복 → 의도적 반복)"; }
        else { conf = CERTAIN; act = c.new[3] === 0 ? "투명 처리" : `바깥 색 ${toHex(c.new)} 로 채움`; }
        const fix = conf === CERTAIN || conf === INTENTIONAL ? { ys: [c.y], xs: [c.x], news: [c.new] } : null;
        out.push(this.f(layer, { type: c.kind, confidence: conf, proposed_action: act, evidence: ev,
                                 bounds: BaseEngine.bounds(layer, c.x, c.y, c.x + 1, c.y + 1), pdf_ref: "2쪽 모티프 회전으로 깨진 경계", fix }));
      }
      for (const c of (flat ? A.diagonalSpikesAndBoundaryOdd(img, this.cfg.repeat_min, layer.has_alpha_channel) : [])) {
        const ev = { old: c.old, new: c.new, same_color_neighbors: c.sameNeighbors, same_neighborhood_repeats: c.repeatCount,
                     pattern: c.kind === "odd_pixel" ? "boundary_odd_pixel" : "diagonal_spike" };
        let conf, act;
        if (!flat) { conf = UNCERTAIN; act = "보고만 (연속톤 레이어)"; }
        else if (c.intentional) { conf = INTENTIONAL; act = "보존 (같은 모양이 반복 → 의도적 반복)"; }
        else { conf = CERTAIN; act = c.new[3] === 0 ? "투명 처리" : `주변 색 ${toHex(c.new)} 로 채움`; }
        const fix = conf === CERTAIN || conf === INTENTIONAL ? { ys: [c.y], xs: [c.x], news: [c.new] } : null;
        out.push(this.f(layer, { type: c.kind, confidence: conf, proposed_action: act, evidence: ev,
                                 bounds: BaseEngine.bounds(layer, c.x, c.y, c.x + 1, c.y + 1),
                                 pdf_ref: c.kind === "odd_pixel" ? "4쪽 경계 위 1픽셀 잘못 칠함" : "2쪽 모티프 회전으로 깨진 경계(대각선)", fix }));
      }
      const PT = new Set(["isolated_speck", "odd_pixel", "pinhole", "edge_spur", "edge_notch"]);
      const taken = new Set(out.filter((f) => f.bounds && PT.has(f.type)).map((f) => f.bounds[1] + "," + f.bounds[0]));
      const bumps = (flat ? A.lineProfileBumps(img, layer.has_alpha_channel) : [])
        .filter((t) => !taken.has((t[0] + layer.bbox[1]) + "," + (t[1] + layer.bbox[0])));
      if (bumps.length) {
        const K = A.keysOf(img);
        const rep = A.repeatedMask(K, img.w, img.h, bumps.map((t) => t[0]), bumps.map((t) => t[1]), 3, this.cfg.repeat_min,
                                   layer.has_alpha_channel ? 0 : A.OOB);
        bumps.forEach(([y, x, nw, old, kind], i) => {
          const newc = A.keyToRgba(nw);
          const ev = { old: A.keyToRgba(old), new: newc, pattern: "line_profile", same_neighborhood_repeats: rep.counts[i] };
          let conf, act;
          if (!flat) { conf = UNCERTAIN; act = "보고만 (연속톤 레이어)"; }
          else if (rep.flags[i]) { conf = INTENTIONAL; act = "보존 (같은 모양이 반복 → 의도적 반복)"; }
          else { conf = CERTAIN; act = newc[3] === 0 ? "투명 처리" : `경계 직선에 맞춰 ${toHex(newc)} 로`; }
          const fix = conf === CERTAIN || conf === INTENTIONAL ? { ys: [y], xs: [x], news: [newc] } : null;
          out.push(this.f(layer, { type: kind, confidence: conf, proposed_action: act, evidence: ev,
                                   bounds: BaseEngine.bounds(layer, x, y, x + 1, y + 1), pdf_ref: "2쪽 모티프 회전으로 깨진 경계(대각선)", fix }));
        });
      }
    }
    if (this.cfg.repeat !== "none") out = out.concat(this.internalSeamFindings(layer, img));
    if (this.cfg.fix_repeats) {
      const OK = new Set(["odd_pixel", "pinhole", "edge_spur", "edge_notch"]);
      for (const f of out) {
        if (f.confidence === INTENTIONAL && f.fix && !f.skip_reason && OK.has(f.type)) {
          f.confidence = CERTAIN;
          f.proposed_action = "반복된 1픽셀도 결함으로 수정 (fix_repeats 옵션) — " + f.proposed_action;
          f.evidence.repeat_override = true;
        }
      }
    }
    for (const f of out) if (f.confidence !== CERTAIN && f.confidence !== INTENTIONAL) f.fix = null;
    this.densityGuard(layer, out, img.h, img.w);
    out = out.concat(this.paletteChecks(layer, img), this.semiAlpha(layer, img));
    if (this.cfg.one_color_per_layer) {
      const s = A.colorStats(img);
      if (s.n > 1) out.push(this.f(layer, { type: "layer_multi_color", confidence: UNCERTAIN, proposed_action: "보고만 (분판 레이어에 색이 2개 이상)",
        evidence: { unique_colors: s.n, top_colors: s.tops.slice(0, 10).map((t) => [toHex(t), t[3]]) }, bounds: layer.bbox.slice(), pdf_ref: "6쪽 컬러 분판 틀림" }));
    }
    return out;
  }
  paletteChecks(layer, img) {
    const pal = this.palette;
    if (!pal) return [];
    return A.offPaletteGroups(img, pal).map((g) => {
      const [x0, y0, x1, y1] = g.bbox;
      const ev = { pixels: g.ys.length, colors: g.colors.map(toHex) };
      if (g.certain) return this.f(layer, { type: "off_palette_edge", confidence: CERTAIN, proposed_action: "경계 혼합색 → 이웃한 허용 색 중 가장 가까운 색으로",
        evidence: ev, bounds: BaseEngine.bounds(layer, x0, y0, x1, y1), pdf_ref: "1쪽 Index 도수는 맞지만 경계에 여러 색 / 6쪽",
        fix: { ys: g.ys, xs: g.xs, news: g.newRgb.map((c) => [c[0], c[1], c[2], 255]) } });
      return this.f(layer, { type: "off_palette", confidence: UNCERTAIN, proposed_action: "보고만 (허용 색 사이 혼합색으로 설명되지 않음)",
        evidence: ev, bounds: BaseEngine.bounds(layer, x0, y0, x1, y1), pdf_ref: "6쪽 컬러 분판 틀림" });
    });
  }
  semiAlpha(layer, img) {
    if (!layer.has_alpha_channel) return [];
    let n = 0, x0 = Infinity, y0 = Infinity, x1 = -1, y1 = -1;
    for (let y = 0; y < img.h; y++) for (let x = 0; x < img.w; x++) {
      const a = img.d[(y * img.w + x) * 4 + 3];
      if (a > 0 && a < 255) { n++; x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
    }
    if (!n) return [];
    return [this.f(layer, { type: "semi_alpha_edge", confidence: UNCERTAIN,
      proposed_action: "보고만 (반투명 경계는 분판 시 중간 톤이 될 수 있음. 자동 경화 미지원)", evidence: { pixels: n },
      bounds: BaseEngine.bounds(layer, x0, y0, x1 + 1, y1 + 1), pdf_ref: "1쪽 경계에 여러 색" })];
  }
  analyzeComposite(img) {
    const out = [];
    const s = A.colorStats(img, 64);
    const major = s.tops.filter((t) => t[3] >= Math.max(1, s.total * 0.0005));
    const ev = { unique_colors: s.n, "major_colors(>=0.05%)": major.length, top_colors: s.tops.slice(0, 24).map((t) => [toHex(t), t[3]]) };
    const exp = this.cfg.expected_color_count;
    let conf = INFO, act = "도수 미입력 → 색 수만 보고";
    if (exp !== null && exp !== undefined) {
      ev["expected_color_count(user)"] = exp;
      conf = s.n === exp ? INFO : UNCERTAIN;
      act = s.n === exp ? "일치" : "불일치 — 사용자 입력 도수와 합성 결과 색 수가 다름 (배경색 포함 여부 확인)";
    }
    out.push(this.f(null, { type: "color_count", confidence: conf, proposed_action: act, evidence: ev, pdf_ref: "1쪽 Index 도수 / 6쪽 분판" }));
    if (!this.palette && s.n > 1 && s.n <= 4096) {
      const all = A.colorStats(img, s.n);
      const rare = all.tops.filter((t) => t[3] <= Math.max(3, s.total * 0.00002));
      if (rare.length) out.push(this.f(null, { type: "rare_color", confidence: UNCERTAIN,
        proposed_action: "보고만 (허용 색 목록을 입력하면 경계 혼합색을 자동 판정)",
        evidence: { rare_color_count: rare.length, samples: rare.slice(0, 20).map((t) => [toHex(t), t[3]]) }, pdf_ref: "1쪽 경계에 여러 색" }));
    }
    for (const c of A.singlePixelAnomalies(img, this.cfg.repeat_min, false)) {
      if (c.intentional) continue;
      out.push(this.f(null, { type: c.kind, confidence: UNCERTAIN, proposed_action: "합성 결과 기준 후보 — 수정은 레이어 단위에서만",
                              evidence: { old: c.old, new: c.new }, bounds: [c.x, c.y, c.x + 1, c.y + 1], pdf_ref: "3~4쪽" }));
    }
    return out;
  }
  analyzeSeparation(layerColors) {
    if (!this.cfg.one_color_per_layer || !layerColors.size) return [];
    const out = [];
    const where = new Map();
    for (const [name, cols] of layerColors) for (const c of cols) { if (!where.has(c)) where.set(c, []); where.get(c).push(name); }
    const dup = {};
    for (const [c, names] of where) if (names.length > 1) dup[toHex(c.split(",").map(Number))] = names;
    if (Object.keys(dup).length) out.push(this.f(null, { type: "separation_mismatch", confidence: UNCERTAIN,
      proposed_action: "보고만 (같은 색이 여러 분판 레이어에 나뉘어 있음)", evidence: { colors_in_multiple_layers: dup }, pdf_ref: "6쪽 컬러 분판 틀림" }));
    const exp = this.cfg.expected_color_count;
    if (exp !== null && exp !== undefined && layerColors.size !== exp) out.push(this.f(null, { type: "separation_mismatch", confidence: UNCERTAIN,
      proposed_action: "보고만 (분판 레이어 수와 입력한 도수가 다름)",
      evidence: { visible_pixel_layers: layerColors.size, expected_color_count: exp, layers: Array.from(layerColors.keys()) }, pdf_ref: "6쪽 컬러 분판 틀림" }));
    return out;
  }
}

const STYLE_PROFILES = {
  digital: { speckAutoMaxArea: 2, speckAutoMinGap: 3, faintReportAlpha: 64, faintAutoAlpha: 40, faintAutoMaxArea: 5000, faintAutoMinGap: 2, pixelFix: true, holeMaxArea: 64 },
  watercolor: { speckAutoMaxArea: 0, speckAutoMinGap: 99, faintReportAlpha: 48, faintAutoAlpha: 16, faintAutoMaxArea: 200, faintAutoMinGap: 6, pixelFix: false, holeMaxArea: 0 },
  pencil: { speckAutoMaxArea: 0, speckAutoMinGap: 99, faintReportAlpha: 48, faintAutoAlpha: 16, faintAutoMaxArea: 200, faintAutoMinGap: 6, pixelFix: false, holeMaxArea: 0 },
  crayon: { speckAutoMaxArea: 0, speckAutoMinGap: 99, faintReportAlpha: 48, faintAutoAlpha: 16, faintAutoMaxArea: 200, faintAutoMinGap: 6, pixelFix: false, holeMaxArea: 0 },
};

class InteriorEngine extends BaseEngine {
  get name() { return "interior"; }
  get defaultFixTypes() { return ["isolated_speck", "faint_residue", "odd_pixel", "pinhole", "paper_removed"]; }
  get profile() { return STYLE_PROFILES[this.cfg.interior_style]; }
  analyzeLayer(layer, img, approvedTexture) {
    const p = this.profile;
    let out = [];
    out = out.concat(this.hygiene(layer, img));
    out = out.concat(this.specks(layer, img, { maxArea: 6, autoMaxArea: p.speckAutoMaxArea, autoMinGap: p.speckAutoMinGap,
      excludeAlphaLe: p.faintReportAlpha, pdfRef: "1쪽 지무늬·배경 포함 점 / 9쪽 덜 지워진 영역" }));
    out = out.concat(this.faintResidue(layer, img), this.paper(layer, img), this.outlineChecks(layer, img), this.internalSeamFindings(layer, img));
    if (!approvedTexture) {
      if (p.pixelFix) out = out.concat(this.pixelAnomalies(layer, img, true, 16));
      if (p.holeMaxArea > 0 && layer.has_alpha_channel) {
        for (const { ys, xs } of A.enclosedHoles(img, 2, p.holeMaxArea).slice(0, 200)) {
          out.push(this.f(layer, { type: "small_hole", confidence: UNCERTAIN, proposed_action: "보고만 (의도된 하이라이트·공백일 수 있음)",
            evidence: { area: ys.length }, bounds: BaseEngine.bounds(layer, N.amin(xs), N.amin(ys), N.amax(xs) + 1, N.amax(ys) + 1),
            pdf_ref: "1쪽 밝은 컬러가 여백처럼 표현" }));
        }
      }
    }
    this.densityGuard(layer, out, img.h, img.w);
    return out;
  }
  outlineChecks(layer, img) {
    const out = [];
    const bb = (ys, xs) => BaseEngine.bounds(layer, N.amin(xs), N.amin(ys), N.amax(xs) + 1, N.amax(ys) + 1);
    if (layer.has_alpha_channel) {
      for (const { ys, xs } of A.edgeForeignSpecks(img).slice(0, 200)) out.push(this.f(layer, { type: "edge_foreign_speck", confidence: UNCERTAIN,
        proposed_action: "보고만 (외곽의 이질적인 작은 점 — 도장툴/스팟툴 흔적 의심)", evidence: { pixels: ys.length }, bounds: bb(ys, xs),
        pdf_ref: "8쪽 도장툴·스팟툴 사용 후 정리 안 한 부분" }));
      for (const { ys, xs } of A.hardEdgeSegments(img).slice(0, 100)) out.push(this.f(layer, { type: "hard_edge", confidence: UNCERTAIN,
        proposed_action: "보고만 (부드러운 외곽 중 계단처럼 딱딱한 구간 — 매끄럽게 정리 필요 여부 확인)", evidence: { hard_boundary_pixels: ys.length },
        bounds: bb(ys, xs), pdf_ref: "8쪽 외곽 매끄럽게 정리 안 된 부분" }));
    }
    for (const [d, pos, a0, a1] of A.straightLineArtifacts(img).slice(0, 50)) {
      const b = d === "vertical" ? BaseEngine.bounds(layer, pos, a0, pos + 1, a1) : BaseEngine.bounds(layer, a0, pos, a1, pos + 1);
      out.push(this.f(layer, { type: "line_artifact", confidence: UNCERTAIN, proposed_action: "보고만 (1px 직선 자국 — 가이드/잘림선 잔여 의심)",
        evidence: { direction: d, length_px: a1 - a0 }, bounds: b, pdf_ref: "8쪽 정리 안 된 부분" }));
    }
    return out;
  }
  faintResidue(layer, img) {
    if (!layer.has_alpha_channel) return [];
    const p = this.profile;
    const out = [];
    for (const c of A.smallComponents(img, 20000, 8, p.faintReportAlpha)) {
      if (c.gap < 1) continue;
      const ev = { area: c.area, max_alpha: c.maxAlpha, gap_px: c.gap, color: toHex(c.meanRgb), similar_components: c.similarCount, style: this.cfg.interior_style };
      let conf, act;
      if (c.similarCount >= this.cfg.repeat_min && c.area <= 16) { conf = INTENTIONAL; act = "보존 (반복되는 같은 형태 → 지무늬/질감 가능성)"; }
      else if (c.maxAlpha <= p.faintAutoAlpha && c.area <= p.faintAutoMaxArea && c.gap >= p.faintAutoMinGap) { conf = CERTAIN; act = "투명 처리 (몸체와 떨어진 옅은 잔여)"; }
      else { conf = UNCERTAIN; act = "보존, 사람이 확인 필요 (번짐/질감 가능성)"; }
      const fix = conf === CERTAIN ? { ys: c.ys, xs: c.xs, news: c.ys.map((y, k) => { const q = (y * img.w + c.xs[k]) * 4; return [img.d[q], img.d[q + 1], img.d[q + 2], 0]; }) } : null;
      const [x0, y0, x1, y1] = c.bbox;
      out.push(this.f(layer, { type: "faint_residue", confidence: conf, proposed_action: act, evidence: ev,
                               bounds: BaseEngine.bounds(layer, x0, y0, x1, y1), pdf_ref: "1쪽 다른 배경색 시 지우개 흔적 / 7·9쪽", fix }));
    }
    return out;
  }
  paper(layer, img) {
    const out = [];
    const paper = this.cfg.paper_color ? parseHex(this.cfg.paper_color) : null;
    if (layer.has_alpha_channel) {
      for (const { ys, xs } of A.paperFringe(img, paper).slice(0, 200)) out.push(this.f(layer, { type: "paper_fringe", confidence: UNCERTAIN,
        proposed_action: "보고만 (외곽 형태 변경이 필요하므로 자동 수정 안 함)", evidence: { pixels: ys.length },
        bounds: BaseEngine.bounds(layer, N.amin(xs), N.amin(ys), N.amax(xs) + 1, N.amax(ys) + 1),
        pdf_ref: "1쪽 스캔 모티프 배경이 외곽에 남음 / 7쪽" }));
    }
    const { w, h, d } = img;
    let allOpaque = true;
    for (let i = 3; i < d.length; i += 4) if (d[i] !== 255) { allOpaque = false; break; }
    if (layer.index > 0 && allOpaque && Math.min(w, h) > 8) {
      const border = [];
      for (let x = 0; x < w; x++) border.push(0 * w + x);
      for (let x = 0; x < w; x++) border.push((h - 1) * w + x);
      for (let y = 0; y < h; y++) border.push(y * w);
      for (let y = 0; y < h; y++) border.push(y * w + w - 1);
      let like = 0;
      for (const i of border) {
        const r = d[i * 4], g = d[i * 4 + 1], b = d[i * 4 + 2];
        if (paper) { if (Math.max(Math.abs(r - paper[0]), Math.abs(g - paper[1]), Math.abs(b - paper[2])) <= 24) like++; }
        else if (0.299 * r + 0.587 * g + 0.114 * b >= 215 && Math.max(r, g, b) - Math.min(r, g, b) <= 25) like++;
      }
      const ratio = like / border.length;
      if (ratio >= 0.8) out.push(this.f(layer, { type: "scan_background", confidence: UNCERTAIN, proposed_action: "보고만 (스캔 배경 제거는 자동으로 하지 않음)",
        evidence: { border_paper_ratio: Math.round(ratio * 1000) / 1000 }, bounds: layer.bbox.slice(), pdf_ref: "1쪽 스캔 모티프 배경" }));
    }
    return out;
  }
  analyzeComposite(img) {
    const s = A.colorStats(img, 8);
    return [this.f(null, { type: "document", confidence: INFO, proposed_action: "참고", evidence: { unique_colors: s.n, style: this.cfg.interior_style } })];
  }
}

function getEngine(cfg) { return cfg.work_type === "interior" ? new InteriorEngine(cfg) : new ApparelEngine(cfg); }

module.exports = { CERTAIN, UNCERTAIN, INTENTIONAL, UNSUPPORTED, INFO, CONF_LABELS, TYPE_LABELS, makeConfig, toHex, parseHex,
                   fnmatch, matchAny, finding, getEngine, BaseEngine };
