// 플러그인 ↔ Python 클린업 엔진: 로컬 파일 교환 (~/PSDCleanup/jobs)
//  export → (엔진 처리) → 결과 읽기 → 복제본에 putPixels → 즉시 재확인 → 새 PSD 저장 → 다시 열어 검증
"use strict";

const { walkLayers, stripRefs, compareTrees, crc32 } = require("./layers.js");

const ENGINE_TIMEOUT_MS = 10 * 60 * 1000;

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

// 투명 픽셀의 색값은 의미가 없으므로 0 으로 정규화한 뒤 지문 (엔진 exchange._trim_layer 와 동일 규칙)
function layerHash(data, comps) {
  if (comps !== 4) {
    if (comps === 3) return crc32(data);
    // RGB 외 형식은 그대로
    return crc32(data);
  }
  const d = new Uint8Array(data);
  for (let i = 0; i < d.length; i += 4) if (d[i + 3] === 0) { d[i] = 0; d[i + 1] = 0; d[i + 2] = 0; }
  return crc32(d);
}

function makeIO(deps) {
  const { fs, formats, types } = deps;
  const ab = (u8) => u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength);
  return {
    async folder(path) {
      try { return await fs.getEntryWithUrl(toFileUrl(path)); }
      catch (e) { return await fs.createEntryWithUrl(toFileUrl(path), { type: types.folder }); }
    },
    async writeBinary(path, u8) {
      const f = await fs.createEntryWithUrl(toFileUrl(path), { overwrite: true });
      await f.write(ab(u8), { format: formats.binary });
    },
    async writeText(path, text) {
      const f = await fs.createEntryWithUrl(toFileUrl(path), { overwrite: true });
      await f.write(text, { format: formats.utf8 });
    },
    async readText(path) {
      const f = await fs.getEntryWithUrl(toFileUrl(path));
      return await f.read({ format: formats.utf8 });
    },
    async readBinary(path) {
      const f = await fs.getEntryWithUrl(toFileUrl(path));
      return new Uint8Array(await f.read({ format: formats.binary }));
    },
    async exists(path) {
      try { await fs.getEntryWithUrl(toFileUrl(path)); return true; } catch (e) { return false; }
    },
    async rename(path, newName) {
      const f = await fs.getEntryWithUrl(toFileUrl(path));
      const parent = await fs.getEntryWithUrl(toFileUrl(splitPath(path).dir));
      await f.moveTo(parent, { newName, overwrite: true });
    },
    async removeFolder(path) {
      try {
        const d = await fs.getEntryWithUrl(toFileUrl(path));
        for (const e of await d.getEntries()) await e.delete();
        await d.delete();
      } catch (e) { /* 이미 없음 */ }
    },
  };
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

function engineRoot(deps) {
  return deps.engineRoot || (deps.os.homedir().replace(/\/$/, "") + "/PSDCleanup");
}

async function engineStatus(deps) {
  const io = makeIO(deps);
  try {
    const j = JSON.parse(await io.readText(engineRoot(deps) + "/engine.json"));
    const age = Date.now() / 1000 - j.time;
    return { alive: age < 15, age, version: j.version, state: j.state };
  } catch (e) { return { alive: false, error: "엔진 상태 파일 없음" }; }
}

// ------------------------------------------------------------------ 1) 내보내기
async function exportJob(deps, doc, config, kind, log) {
  const { core, imaging } = deps;
  const io = makeIO(deps);
  const say = log || (() => {});
  const root = engineRoot(deps);
  await io.folder(root);
  await io.folder(root + "/jobs");
  const jobId = "job_" + Date.now().toString(36) + "_" + Math.floor(Math.random() * 1e6).toString(36);
  const dir = `${root}/jobs/${jobId}`;
  await io.folder(dir);
  const tree = walkLayers(doc.layers);
  const visAt = [];
  const layers = [];
  const orig = {};               // order → {hash, bounds, components}
  let profile = null;
  let composite = null;
  await core.executeAsModal(async () => {
    for (const l of tree) {
      visAt[l.depth] = l.visible && (l.depth === 0 || visAt[l.depth - 1]);
      const e = Object.assign({}, l, { effectiveVisible: !!visAt[l.depth] });
      delete e._ref;
      if (l.kind === "pixel") {
        try {
          const px = await readPixels(imaging, doc.id, l.id);
          profile = profile || px.colorProfile;
          const f = `layer_${l.order}.raw`;
          await io.writeBinary(`${dir}/${f}`, px.data);
          e.file = f;
          e.bounds = px.bounds;
          e.components = px.components;
          e.hasMask = await hasUserMask(imaging, doc.id, l.id);
          orig[l.order] = { hash: layerHash(px.data, px.components), bounds: px.bounds, components: px.components };
        } catch (err) {
          e.readError = String(err.message || err);          // 빈 레이어 등
        }
      }
      layers.push(e);
      say(`내보내기: ${l.path}`);
    }
    try {
      const c = await readPixels(imaging, doc.id, null);
      await io.writeBinary(`${dir}/composite.raw`, c.data);
      composite = { file: "composite.raw", components: c.components, bounds: c.bounds };
    } catch (err) { /* 합성 이미지는 선택 */ }
  }, { commandName: "클린업: 레이어 픽셀 내보내기" });
  const req = {
    version: 1, job_id: jobId, kind, created: Date.now(),
    document: { name: doc.name, path: doc.path || "", width: Number(doc.width), height: Number(doc.height),
                resolution: Number(doc.resolution), mode: String(doc.mode), bitsPerChannel: String(doc.bitsPerChannel),
                colorProfileName: String(doc.colorProfileName) },
    config, layers, composite,
  };
  // request.json 은 마지막에, 임시 이름으로 쓴 뒤 이름을 바꿔 '준비 완료'를 알림 (엔진이 반쯤 쓴 파일을 읽지 않게)
  await io.writeText(`${dir}/request.json.part`, JSON.stringify(req));
  await io.rename(`${dir}/request.json.part`, "request.json");
  return { jobId, dir, tree, orig, profile, req };
}

// ------------------------------------------------------------------ 2) 결과 기다리기
async function waitResult(deps, job, onProgress) {
  const io = makeIO(deps);
  const t0 = Date.now();
  let lastAliveCheck = 0;
  for (;;) {
    if (await io.exists(`${job.dir}/result.json`)) {
      return JSON.parse(await io.readText(`${job.dir}/result.json`));
    }
    try {
      const p = JSON.parse(await io.readText(`${job.dir}/progress.json`));
      if (onProgress) onProgress(p.progress, p.message);
    } catch (e) { /* 아직 없음 */ }
    if (Date.now() - lastAliveCheck > 5000) {
      lastAliveCheck = Date.now();
      const st = await engineStatus(deps);
      if (!st.alive && Date.now() - t0 > 15000) throw new Error("클린업 엔진이 꺼져 있습니다. '엔진 설치·실행'을 확인하세요.");
    }
    if (Date.now() - t0 > ENGINE_TIMEOUT_MS) throw new Error("엔진 응답 시간 초과");
    await new Promise((r) => setTimeout(r, deps.pollMs || 500));
  }
}

// ------------------------------------------------------------------ 3) 복제본에 적용 + 즉시 검증
async function applyToDuplicate(deps, doc, job, result, log) {
  const { core, imaging } = deps;
  const io = makeIO(deps);
  const say = log || (() => {});
  const problems = [];
  let dup = null;
  if (!result.patches || result.patches.length === 0) return { dup: null, applied: 0, problems };
  const base = splitPath(doc.path || doc.name).base;
  await core.executeAsModal(async () => {
    dup = await doc.duplicate(`${base}_CLEAN`);
    const dupTree = walkLayers(dup.layers);
    if (dupTree.length !== job.tree.length) throw new Error("복제본의 레이어 수가 원본과 다름");
    for (const p of result.patches) {
      const t = dupTree[p.order];
      if (!t || t.path !== p.path) { problems.push(`복제본에서 레이어를 찾지 못함: ${p.path}`); continue; }
      const data = await io.readBinary(`${job.dir}/${p.file}`);
      if (data.length !== p.width * p.height * p.components) { problems.push(`패치 크기 오류: ${p.path}`); continue; }
      const img = await imaging.createImageDataFromBuffer(data, {
        width: p.width, height: p.height, components: p.components, chunky: true,
        colorSpace: "RGB", colorProfile: job.profile || undefined,
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
      if (l.kind !== "pixel" || !job.orig[l.order]) continue;
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
      } else if (!got || layerHash(got.data, got.components) !== job.orig[l.order].hash) {
        problems.push(`${l.path}: 수정하지 않은 레이어가 바뀜`);
      }
    }
    if (problems.length) { dup.closeWithoutSaving(); dup = null; }     // 불확실하면 적용하지 않음
  }, { commandName: "클린업: 복제본에 적용" });
  return { dup, applied: problems.length ? 0 : result.patches.length, problems };
}

// ------------------------------------------------------------------ 4) 새 PSD 로 저장 + 다시 열어 검증
async function saveAndVerify(deps, doc, dup, job, result, log) {
  const { core, imaging, app, fs } = deps;
  const io = makeIO(deps);
  const say = log || (() => {});
  const sp = splitPath(doc.path || doc.name);
  let outPath = `${sp.dir}/${sp.base}_CLEAN.psd`;
  for (let k = 2; (await io.exists(outPath)) && k < 100; k++) outPath = `${sp.dir}/${sp.base}_CLEAN_${k}.psd`;
  if (outPath === String(doc.path).replace(/\\/g, "/")) throw new Error("원본 덮어쓰기 방지");
  const checks = [];
  let re = null;
  await core.executeAsModal(async () => {
    const entry = await fs.createEntryWithUrl(toFileUrl(outPath), { overwrite: false });
    await dup.saveAs.psd(entry, { embedColorProfile: true }, false);
    dup.closeWithoutSaving();
    re = await app.open(await fs.getEntryWithUrl(toFileUrl(outPath)));
  }, { commandName: "클린업: 새 PSD 저장" });
  say(`저장: ${outPath}`);
  // 문서 속성
  const d = job.req.document;
  const dd = [];
  for (const [k, v] of [["width", Number(re.width)], ["height", Number(re.height)], ["resolution", Number(re.resolution)],
    ["mode", String(re.mode)], ["bitsPerChannel", String(re.bitsPerChannel)], ["colorProfileName", String(re.colorProfileName)]]) {
    if (d[k] !== v) dd.push(`${k}: ${d[k]} → ${v}`);
  }
  checks.push({ name: "캔버스 크기·해상도·색상 모드·비트·색상 프로파일", ok: dd.length === 0, detail: dd.join("; ") });
  // 레이어 구조 (수정한 레이어는 경계가 바뀔 수 있으므로 경계는 엔진 예상값과 비교)
  const patched = {};
  (result.patches || []).forEach((p) => { patched[p.order] = p; });
  const a = stripRefs(job.tree).map((x) => (patched[x.order] ? Object.assign({}, x, { bounds: null }) : x));
  const reTree = walkLayers(re.layers);
  const b = stripRefs(reTree).map((x) => (patched[x.order] ? Object.assign({}, x, { bounds: null }) : x));
  const probs = compareTrees(a, b);
  checks.push({ name: "레이어 이름·그룹·순서·가시성·불투명도·블렌드·잠금·위치", ok: probs.length === 0, detail: probs.slice(0, 6).join(" | ") });
  // 픽셀
  const pixProbs = [];
  await core.executeAsModal(async () => {
    for (const l of reTree) {
      const o = job.orig[l.order];
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
    await core.executeAsModal(async () => { re.closeWithoutSaving(); }, { commandName: "클린업: 검증 실패 파일 닫기" });
    const failedName = `${splitPath(outPath).base}_VERIFY_FAILED.psd`;
    await io.rename(outPath, failedName);
    outPath = `${splitPath(outPath).dir}/${failedName}`;
  }
  return { outPath, passed, checks, reopened: passed ? re : null };
}

module.exports = { exportJob, waitResult, applyToDuplicate, saveAndVerify, engineStatus, engineRoot,
                   makeIO, layerHash, toFileUrl, splitPath };
