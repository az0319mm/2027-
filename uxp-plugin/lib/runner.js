// 플러그인 안에서 끝까지: 픽셀 읽기(메모리) → 클린업 엔진(engine/*.js) → 복제본에 적용 → 즉시 재확인 → 새 PSD 저장 → 다시 열어 검증
// Python·외부 프로그램·임시 파일 없음. 디스크에 쓰는 것은 사용자가 저장을 누른 '_CLEAN.psd'(와 원할 때 보고서)뿐.
"use strict";

const { walkLayers, stripRefs, compareTrees, crc32 } = require("./layers.js");
const P = require("../engine/plan.js");

function toFileUrl(p) {
  let s = String(p).replace(/\\/g, "/");
  if (/^[A-Za-z]:\//.test(s)) s = "/" + s;
  return "file:" + s;
}

function splitPath(p) {
  const s = String(p).replace(/\\/g, "/");
  const i = s.lastIndexOf("/");
  const file = s.slice(i + 1);
  const dot = file.lastIndexOf(".");
  return { dir: s.slice(0, i), base: dot > 0 ? file.slice(0, dot) : file };
}

// 투명 픽셀의 색값은 의미가 없으므로 0 으로 정규화한 뒤 지문 (엔진 trimmedHash 와 같은 규칙)
function layerHash(data, comps) {
  if (comps !== 4) return crc32(data);
  const d = new Uint8Array(data);
  for (let i = 0; i < d.length; i += 4) if (d[i + 3] === 0) { d[i] = 0; d[i + 1] = 0; d[i + 2] = 0; }
  return crc32(d);
}

async function exists(fs, path) {
  try { await fs.getEntryWithUrl(toFileUrl(path)); return true; } catch (e) { return false; }
}

async function renameFile(fs, path, newName) {
  const f = await fs.getEntryWithUrl(toFileUrl(path));
  const parent = await fs.getEntryWithUrl(toFileUrl(splitPath(path).dir));
  await f.moveTo(parent, { newName, overwrite: true });
}

async function readPixels(imaging, docId, layerId) {
  const opt = { documentID: docId, componentSize: 8 };
  if (layerId != null) opt.layerID = layerId;
  const r = await imaging.getPixels(opt);
  const img = r.imageData;
  const data = new Uint8Array(await img.getData({ chunky: true }));
  const b = r.sourceBounds;
  const out = { data, width: img.width, height: img.height, components: img.components,
                colorSpace: img.colorSpace, colorProfile: img.colorProfile,
                bounds: b ? [b.left, b.top, b.right, b.bottom] : [0, 0, img.width, img.height] };
  if (img.dispose) await img.dispose();
  return out;
}

async function hasUserMask(imaging, docId, layerId) {
  try {
    const r = await imaging.getLayerMask({ documentID: docId, layerID: layerId, kind: "user" });
    const ok = !!(r && r.imageData && r.imageData.width > 0);
    if (r && r.imageData && r.imageData.dispose) await r.imageData.dispose();
    return ok;
  } catch (e) { return false; }
}

const tick = () => new Promise((r) => setTimeout(r, 30));      // 패널 글자가 화면에 그려질 틈

// ------------------------------------------------------------------ 1) 문서 읽기 (메모리)
async function readDocument(deps, doc, config, say) {
  const { core, imaging } = deps;
  say = say || (() => {});
  const tree = walkLayers(doc.layers);
  const visAt = [];
  const layers = [];
  const pixels = new Map();      // order → Uint8Array (알파>0 영역으로 잘린 레이어)
  const orig = {};               // order → {hash, bounds, components}
  let profile = null, composite = null, compositeData = null;
  await core.executeAsModal(async () => {
    for (const l of tree) {
      visAt[l.depth] = l.visible && (l.depth === 0 || visAt[l.depth - 1]);
      const e = Object.assign({}, l, { effectiveVisible: !!visAt[l.depth] });
      delete e._ref;
      if (l.kind === "pixel") {
        try {
          const px = await readPixels(imaging, doc.id, l.id);
          profile = profile || px.colorProfile;
          pixels.set(l.order, px.data);
          e.bounds = px.bounds;
          e.components = px.components;
          e.hasMask = await hasUserMask(imaging, doc.id, l.id);
          orig[l.order] = { hash: layerHash(px.data, px.components), bounds: px.bounds, components: px.components };
        } catch (err) {
          e.readError = String(err.message || err);          // 빈 레이어 등
        }
      }
      layers.push(e);
      say(`읽기: ${l.path}`);
    }
    try {
      const c = await readPixels(imaging, doc.id, null);
      compositeData = c.data;
      composite = { components: c.components, bounds: c.bounds };
    } catch (err) { /* 합성 이미지는 선택 (없으면 엔진이 근사 합성) */ }
  }, { commandName: "클린업: 레이어 픽셀 읽기" });
  const req = {
    version: 1, created: Date.now(),
    document: { name: doc.name, path: doc.path || "", width: Number(doc.width), height: Number(doc.height),
                resolution: Number(doc.resolution), mode: String(doc.mode), bitsPerChannel: String(doc.bitsPerChannel),
                colorProfileName: String(doc.colorProfileName) },
    config, layers, composite,
  };
  return { tree, orig, profile, req, data: { layers: pixels, composite: compositeData } };
}

// ------------------------------------------------------------------ 2) 엔진 실행 (플러그인 안 JavaScript)
async function analyze(snap, kind, onProgress) {
  if (onProgress) onProgress(0.02, "검사 시작 (큰 PSD 는 1분 정도 걸리고, 그동안 Photoshop 이 멈춘 듯 보일 수 있습니다)");
  await tick();
  const t0 = Date.now();
  const r = P.runCleanup(snap.req, snap.data, kind, onProgress);
  r.summary.elapsed_sec = Math.round((Date.now() - t0) / 100) / 10;
  r.changes = changedPoints(snap, r.patches);
  delete r._doc;
  delete r._plan;
  return r;
}

// 패치로 실제 바뀌는 캔버스 좌표 (비교 화면·보고서용). 패치는 레이어 전체이므로 원래 영역과 새 영역을 합쳐 비교
function changedPoints(snap, patches) {
  const pts = [];
  for (const p of patches) {
    const L = snap.req.layers[p.order], src = snap.data.layers.get(p.order), c = p.components;
    const b = L.bounds || [p.left, p.top, p.left, p.top], w = b[2] - b[0];
    const nb = [p.left, p.top, p.left + p.width, p.top + p.height];
    const at = (data, bb, ww, x, y, k) => (data && x >= bb[0] && x < bb[2] && y >= bb[1] && y < bb[3]
      ? data[((y - bb[1]) * ww + (x - bb[0])) * c + k] : 0);
    for (let y = Math.min(b[1], nb[1]); y < Math.max(b[3], nb[3]); y++) for (let x = Math.min(b[0], nb[0]); x < Math.max(b[2], nb[2]); x++) {
      const a0 = c === 4 ? at(src, b, w, x, y, 3) : 255, a1 = c === 4 ? at(p.data, nb, p.width, x, y, 3) : 255;
      let diff = (a0 === 0) !== (a1 === 0);
      if (!diff && a0 !== 0) for (let k = 0; k < c; k++) if (at(src, b, w, x, y, k) !== at(p.data, nb, p.width, x, y, k)) { diff = true; break; }
      if (diff) pts.push([x, y, p.path]);
    }
  }
  return pts;
}

// ------------------------------------------------------------------ 3) 복제본에 적용 + 즉시 검증
async function applyToDuplicate(deps, doc, snap, result, say) {
  const { core, imaging } = deps;
  say = say || (() => {});
  const problems = [];
  let dup = null;
  if (!result.patches || result.patches.length === 0) return { dup: null, applied: 0, problems };
  const base = splitPath(doc.path || doc.name).base;
  await core.executeAsModal(async () => {
    dup = await doc.duplicate(`${base}_CLEAN`);
    const dupTree = walkLayers(dup.layers);
    if (dupTree.length !== snap.tree.length) throw new Error("복제본의 레이어 수가 원본과 다름");
    for (const p of result.patches) {
      const t = dupTree[p.order];
      if (!t || t.path !== p.path) { problems.push(`복제본에서 레이어를 찾지 못함: ${p.path}`); continue; }
      if (p.data.length !== p.width * p.height * p.components) { problems.push(`패치 크기 오류: ${p.path}`); continue; }
      const img = await imaging.createImageDataFromBuffer(p.data, {
        width: p.width, height: p.height, components: p.components, chunky: true,
        colorSpace: "RGB", colorProfile: snap.profile || undefined,
      });
      await imaging.putPixels({ documentID: dup.id, layerID: t.id, imageData: img,
                                targetBounds: { left: p.left, top: p.top }, replace: true, commandName: "자동 클린업" });
      if (img.dispose) await img.dispose();
      say(`적용: ${p.path} (${p.changed_pixels}px)`);
    }
    // 즉시 재확인: 수정한 레이어는 엔진이 계산한 결과와, 나머지는 원본과 같아야 함
    const patched = {};
    result.patches.forEach((p) => { patched[p.order] = p; });
    for (const l of dupTree) {
      if (l.kind !== "pixel" || !snap.orig[l.order]) continue;
      let got;
      try { got = await readPixels(imaging, dup.id, l.id); } catch (e) { got = null; }
      const p = patched[l.order];
      if (p) {
        if (!p.expected_hash) {
          if (got) problems.push(`${l.path}: 레이어가 비어야 하는데 픽셀이 남음`);
        } else if (!got || layerHash(got.data, got.components) !== p.expected_hash ||
                   JSON.stringify(got.bounds) !== JSON.stringify(p.expected_bounds)) {
          problems.push(`${l.path}: 적용 결과가 엔진 계산과 다름`);
        }
      } else if (!got || layerHash(got.data, got.components) !== snap.orig[l.order].hash) {
        problems.push(`${l.path}: 수정하지 않은 레이어가 바뀜`);
      }
    }
    if (problems.length) { await dup.closeWithoutSaving(); dup = null; }     // 불확실하면 적용하지 않음
  }, { commandName: "클린업: 복제본에 적용" });
  return { dup, applied: problems.length ? 0 : result.patches.length, problems };
}

// ------------------------------------------------------------------ 4) 전후 비교 문서 (저장하지 않는 새 문서)
//  레이어: 수정 전 / 수정 후 / 바뀐 곳(빨강) — 눈 아이콘으로 '수정 후'를 껐다 켜며 비교
async function makeCompareDoc(deps, srcDoc, dup, result, say) {
  const { core, imaging, app } = deps;
  say = say || (() => {});
  const W = Number(srcDoc.width), H = Number(srcDoc.height);
  let cmp = null;
  await core.executeAsModal(async () => {
    const before = await readPixels(imaging, srcDoc.id, null);
    const after = await readPixels(imaging, dup.id, null);
    cmp = await app.documents.add({ name: `${splitPath(srcDoc.name).base}_전후비교`, width: W, height: H,
                                    resolution: Number(srcDoc.resolution) || 72, mode: "RGBColorMode", fill: "white" });
    const put = async (name, rgba) => {
      const layer = await cmp.createLayer("pixel", { name });
      const img = await imaging.createImageDataFromBuffer(rgba, { width: W, height: H, components: 4, chunky: true, colorSpace: "RGB" });
      await imaging.putPixels({ documentID: cmp.id, layerID: layer.id, imageData: img, targetBounds: { left: 0, top: 0 },
                                replace: true, commandName: "전후 비교" });
      if (img.dispose) await img.dispose();
      return layer;
    };
    await put("수정 전 (원본)", toRGBA(before, W, H));
    await put("수정 후 (눈 아이콘으로 껐다 켜기)", toRGBA(after, W, H));
    await put("바뀐 곳 (빨강 점·원)", markLayer(result.changes || [], W, H));
  }, { commandName: "클린업: 전후 비교 문서 만들기" });
  say(`전후 비교 문서 '${cmp.name}' (저장되지 않음 — 다 보면 닫으세요)`);
  return cmp;
}

function toRGBA(px, W, H) {
  const out = new Uint8Array(W * H * 4).fill(255);
  const [x0, y0, x1, y1] = px.bounds, w = x1 - x0, c = px.components;
  for (let y = Math.max(0, y0); y < Math.min(H, y1); y++) for (let x = Math.max(0, x0); x < Math.min(W, x1); x++) {
    const s = ((y - y0) * w + (x - x0)) * c, t = (y * W + x) * 4;
    out[t] = px.data[s]; out[t + 1] = px.data[s + 1]; out[t + 2] = px.data[s + 2];
    out[t + 3] = c === 4 ? 255 : 255;
  }
  return out;
}

// 바뀐 픽셀은 빨강, 주변은 빨간 원 (원 반지름은 그림 크기에 비례해 작은 수정도 보이게)
function markLayer(points, W, H) {
  const out = new Uint8Array(W * H * 4);
  const R = Math.max(10, Math.round(Math.max(W, H) / 150)), T = Math.max(2, Math.round(R / 6));
  const seen = new Set();
  const red = (x, y) => { if (x >= 0 && y >= 0 && x < W && y < H) out.set([255, 0, 0, 255], (y * W + x) * 4); };
  for (const [x, y] of points) {
    const key = `${Math.floor(x / R)},${Math.floor(y / R)}`;
    if (!seen.has(key)) {
      seen.add(key);
      for (let dy = -R - T; dy <= R + T; dy++) for (let dx = -R - T; dx <= R + T; dx++) {
        const d = Math.sqrt(dx * dx + dy * dy);
        if (d >= R && d < R + T) red(x + dx, y + dy);
      }
    }
  }
  for (const [x, y] of points) red(x, y);
  return out;
}

// ------------------------------------------------------------------ 5) 새 PSD 로 저장 + 다시 열어 검증
async function saveAndVerify(deps, doc, dup, snap, result, say) {
  const { core, imaging, app, fs } = deps;
  say = say || (() => {});
  const sp = splitPath(doc.path || doc.name);
  let outPath, picked = null;
  if (doc.path) {
    outPath = `${sp.dir}/${sp.base}_CLEAN.psd`;
    for (let k = 2; (await exists(fs, outPath)) && k < 100; k++) outPath = `${sp.dir}/${sp.base}_CLEAN_${k}.psd`;
  } else {                                          // 한 번도 저장하지 않은 문서 → 저장 위치를 물어봄
    picked = await fs.getFileForSaving(`${sp.base}_CLEAN.psd`, { types: ["psd"] });
    if (!picked) throw new Error("저장을 취소했습니다");
    outPath = picked.nativePath;
  }
  if (outPath === String(doc.path).replace(/\\/g, "/")) throw new Error("원본 덮어쓰기 방지");
  const checks = [];
  let re = null;
  await core.executeAsModal(async () => {
    const entry = picked || await fs.createEntryWithUrl(toFileUrl(outPath), { overwrite: false });
    await dup.saveAs.psd(entry, { embedColorProfile: true }, false);
    await dup.closeWithoutSaving();
    re = await app.open(await fs.getEntryWithUrl(toFileUrl(outPath)));
  }, { commandName: "클린업: 새 PSD 저장" });
  say(`저장: ${outPath}`);
  const d = snap.req.document;
  const dd = [];
  for (const [k, v] of [["width", Number(re.width)], ["height", Number(re.height)], ["resolution", Number(re.resolution)],
    ["mode", String(re.mode)], ["bitsPerChannel", String(re.bitsPerChannel)], ["colorProfileName", String(re.colorProfileName)]]) {
    if (d[k] !== v) dd.push(`${k}: ${d[k]} → ${v}`);
  }
  checks.push({ name: "캔버스 크기·해상도·색상 모드·비트·색상 프로파일", ok: dd.length === 0, detail: dd.join("; ") });
  // 레이어 구조 (수정한 레이어는 경계가 바뀔 수 있으므로 경계는 엔진 예상값과 비교)
  const patched = {};
  (result.patches || []).forEach((p) => { patched[p.order] = p; });
  const a = stripRefs(snap.tree).map((x) => (patched[x.order] ? Object.assign({}, x, { bounds: null }) : x));
  const reTree = walkLayers(re.layers);
  const b = stripRefs(reTree).map((x) => (patched[x.order] ? Object.assign({}, x, { bounds: null }) : x));
  const probs = compareTrees(a, b);
  checks.push({ name: "레이어 이름·그룹·순서·가시성·불투명도·블렌드·잠금·위치", ok: probs.length === 0, detail: probs.slice(0, 6).join(" | ") });
  const pixProbs = [];
  await core.executeAsModal(async () => {
    for (const l of reTree) {
      const o = snap.orig[l.order];
      if (l.kind !== "pixel" || !o) continue;
      let got = null;
      try { got = await readPixels(imaging, re.id, l.id); } catch (e) { got = null; }
      const p = patched[l.order];
      const expHash = p ? p.expected_hash : o.hash;
      const expBounds = p ? p.expected_bounds : o.bounds;
      if (!expHash) { if (got) pixProbs.push(`${l.path}: 비어야 함`); continue; }
      if (!got || layerHash(got.data, got.components) !== expHash || JSON.stringify(got.bounds) !== JSON.stringify(expBounds)) {
        pixProbs.push(`${l.path}: ${p ? "수정 결과" : "원본 픽셀"}와 다름`);
      }
    }
  }, { commandName: "클린업: 저장본 검증" });
  checks.push({ name: "픽셀: 수정한 곳만 바뀌고 나머지는 원본과 동일", ok: pixProbs.length === 0, detail: pixProbs.slice(0, 6).join(" | ") });
  const passed = checks.every((c) => c.ok);
  if (!passed) {
    await core.executeAsModal(async () => { await re.closeWithoutSaving(); }, { commandName: "클린업: 검증 실패 파일 닫기" });
    const failedName = `${splitPath(outPath).base}_VERIFY_FAILED.psd`;
    await renameFile(fs, outPath, failedName);
    outPath = `${splitPath(outPath).dir}/${failedName}`;
  }
  return { outPath, passed, checks, reopened: passed ? re : null };
}

module.exports = { readDocument, analyze, applyToDuplicate, makeCompareDoc, saveAndVerify, changedPoints, markLayer,
                   layerHash, toFileUrl, splitPath, readPixels };
