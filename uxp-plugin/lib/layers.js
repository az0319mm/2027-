// 레이어 트리 탐색 / 비교 / 해시 (Photoshop 과 Node 테스트 양쪽에서 사용)
"use strict";

function safe(fn, dflt) {
  try { const v = fn(); return v === undefined ? dflt : v; } catch (e) { return dflt; }
}

// Photoshop DOM 레이어 트리 → 위→아래 순서의 평면 목록 (그룹 먼저, 그 안의 레이어 다음)
function walkLayers(layers, prefix, depth, out) {
  out = out || [];
  prefix = prefix || "";
  depth = depth || 0;
  for (const l of layers || []) {
    const kind = String(safe(() => l.kind, "unknown"));
    const b = safe(() => l.bounds, null);
    const info = {
      order: out.length,
      id: safe(() => l.id, null),
      name: safe(() => l.name, ""),
      path: prefix + safe(() => l.name, ""),
      depth,
      kind,
      visible: !!safe(() => l.visible, true),
      opacity: Math.round(Number(safe(() => l.opacity, 100))),
      fillOpacity: Math.round(Number(safe(() => l.fillOpacity, 100))),
      blendMode: String(safe(() => l.blendMode, "")),
      bounds: b ? [Number(b.left), Number(b.top), Number(b.right), Number(b.bottom)] : null,
      isClippingMask: !!safe(() => l.isClippingMask, false),
      isBackgroundLayer: !!safe(() => l.isBackgroundLayer, false),
      allLocked: !!safe(() => l.allLocked, false),
      pixelsLocked: !!safe(() => l.pixelsLocked, false),
      positionLocked: !!safe(() => l.positionLocked, false),
      transparentPixelsLocked: !!safe(() => l.transparentPixelsLocked, false),
      childCount: kind === "group" ? (safe(() => l.layers, []) || []).length : 0,
      _ref: l,
    };
    out.push(info);
    if (kind === "group") walkLayers(safe(() => l.layers, []), info.path + "/", depth + 1, out);
  }
  return out;
}

function stripRefs(list) {
  return list.map((x) => { const y = Object.assign({}, x); delete y._ref; return y; });
}

const COMPARE_KEYS = ["path", "kind", "visible", "opacity", "fillOpacity", "blendMode", "isClippingMask",
  "isBackgroundLayer", "allLocked", "pixelsLocked", "positionLocked", "transparentPixelsLocked", "childCount", "bounds"];

function compareTrees(a, b) {
  const problems = [];
  if (a.length !== b.length) problems.push(`레이어 수가 다름: 원본 ${a.length}개 / 저장본 ${b.length}개`);
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    for (const k of COMPARE_KEYS) {
      const va = JSON.stringify(a[i][k]);
      const vb = JSON.stringify(b[i][k]);
      if (va !== vb) problems.push(`[${i + 1}] ${a[i].path} 의 ${k} 다름: 원본 ${va} / 저장본 ${vb}`);
    }
  }
  return problems;
}

// FNV-1a 32bit (빠르고 결정적인 픽셀 지문)
function fnv1a(bytes) {
  let h = 0x811c9dc5;
  for (let i = 0; i < bytes.length; i++) {
    h ^= bytes[i];
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return ("00000000" + h.toString(16)).slice(-8);
}

module.exports = { walkLayers, stripRefs, compareTrees, fnv1a, COMPARE_KEYS };
