// Photoshop DOM 레이어 트리와 psd-tools(서버) 레이어 목록을 대조한다.
// Photoshop DOM: 위→아래 순서, opacity 0~100, blendMode 문자열 ("normal" 등)
// 서버(psd-tools): 레코드 인덱스 아래→위 순서, opacity 0~255, blend_mode ("NORMAL", "MULTIPLY" 등)

function flattenPsLayers(layers, prefix, out) {
  out = out || [];
  prefix = prefix || "";
  for (const l of layers) {
    const path = prefix + l.name;
    out.push({ name: l.name, path, kind: String(l.kind), visible: !!l.visible,
               opacity: Math.round(Number(l.opacity)),
               blend: String(l.blendMode || "").toLowerCase().replace(/[^a-z]/g, ""),
               isGroup: !!(l.layers && l.layers.length >= 0 && String(l.kind).toLowerCase() === "group"),
               bounds: l.bounds ? [l.bounds.left, l.bounds.top, l.bounds.right, l.bounds.bottom] : null });
    if (l.layers && String(l.kind).toLowerCase() === "group") flattenPsLayers(l.layers, path + "/", out);
  }
  return out;
}

const BLEND_ALIASES = { passthrough: "passthrough", normal: "normal", multiply: "multiply", screen: "screen",
  overlay: "overlay", darken: "darken", lighten: "lighten", colordodge: "colordodge", colorburn: "colorburn",
  lineardodge: "lineardodge", linearburn: "linearburn", softlight: "softlight", hardlight: "hardlight",
  difference: "difference", exclusion: "exclusion", hue: "hue", saturation: "saturation", color: "color",
  luminosity: "luminosity", dissolve: "dissolve", vividlight: "vividlight", linearlight: "linearlight",
  pinlight: "pinlight", hardmix: "hardmix", subtract: "subtract", divide: "divide",
  darkercolor: "darkercolor", lightercolor: "lightercolor" };

function normBlend(s) {
  const k = String(s || "").toLowerCase().replace(/[^a-z]/g, "");
  return BLEND_ALIASES[k] || k;
}

// psd-tools 목록을 Photoshop DOM 과 같은 "위→아래, 그룹 먼저" 순서로 정렬
function serverOrder(layers) {
  // psd-tools path 기준 트리 재구성 (index 큰 것이 위)
  const sorted = layers.slice().sort((a, b) => b.index - a.index);
  return sorted;
}

function compareLayerTrees(psFlat, serverLayers) {
  const problems = [];
  const warnings = [];
  const srv = serverOrder(serverLayers);
  if (psFlat.length !== srv.length) {
    problems.push(`레이어 수 불일치: Photoshop ${psFlat.length} / 파일 해석 ${srv.length}`);
  }
  const byPath = new Map();
  for (const s of srv) byPath.set(s.path, (byPath.get(s.path) || []).concat([s]));
  const seen = new Map();
  for (const p of psFlat) {
    const k = seen.get(p.path) || 0;
    seen.set(p.path, k + 1);
    const cands = byPath.get(p.path) || [];
    const s = cands[k];
    if (!s) { problems.push(`파일 해석에 없는 레이어: ${p.path}`); continue; }
    if (p.visible !== s.visible) problems.push(`가시성 불일치: ${p.path}`);
    const op = Math.round(s.opacity / 2.55);
    if (Math.abs(op - p.opacity) > 1) problems.push(`불투명도 불일치: ${p.path} (PS ${p.opacity}% / 파일 ${op}%)`);
    const isGroup = String(s.kind) === "group";
    if (!isGroup && normBlend(p.blend) !== normBlend(s.blend_mode)) {
      problems.push(`블렌드 모드 불일치: ${p.path} (PS ${p.blend} / 파일 ${s.blend_mode})`);
    }
    if (!isGroup && p.bounds && s.bbox && s.kind === "pixel") {
      const d = p.bounds.map((v, i) => Math.abs(v - s.bbox[i]));
      if (Math.max(...d) > 0) warnings.push(`경계 차이(투명 여백 차이일 수 있음): ${p.path} PS ${p.bounds} / 파일 ${s.bbox}`);
    }
  }
  // 순서 비교 (이름 시퀀스)
  const a = psFlat.map(x => x.path).join("\n");
  const b = srv.map(x => x.path).join("\n");
  if (a !== b) warnings.push("레이어 나열 순서가 다릅니다 (그룹 내 순서 확인 필요)");
  return { ok: problems.length === 0, problems, warnings };
}

module.exports = { flattenPsLayers, compareLayerTrees, normBlend };
