// 1단계: Photoshop 연동 자체 테스트 (Python 불필요)
//  1) 문서 인식  2) 전체 레이어 탐색  3) 픽셀 읽기  4) 복제본에 테스트 픽셀 1개 수정
//  5) 새 PSD 로 저장  6) 저장한 PSD 를 다시 열어 레이어·픽셀 보존 검증
// 원본 문서는 읽기만 한다. 수정은 '복제본'에만 하고, 저장 파일 이름은 원본과 절대 같지 않다.
"use strict";

const { walkLayers, stripRefs, compareTrees, fnv1a } = require("./layers.js");

function toFileUrl(nativePath) {
  let p = String(nativePath).replace(/\\/g, "/");
  if (/^[A-Za-z]:\//.test(p)) p = "/" + p;          // Windows: C:/... → /C:/...  (Windows 는 미검증)
  return "file:" + p;
}

function splitPath(p) {
  const s = String(p).replace(/\\/g, "/");
  const i = s.lastIndexOf("/");
  const dir = s.slice(0, i);
  const file = s.slice(i + 1);
  const dot = file.lastIndexOf(".");
  return { dir, base: dot > 0 ? file.slice(0, dot) : file, ext: dot > 0 ? file.slice(dot) : "", sep: String(p).includes("\\") ? "\\" : "/" };
}

async function readLayerPixels(imaging, docId, layerId) {
  const r = await imaging.getPixels({ documentID: docId, layerID: layerId, componentSize: 8 });
  const img = r.imageData;
  const data = await img.getData({ chunky: true });
  const info = {
    bounds: r.sourceBounds ? [r.sourceBounds.left, r.sourceBounds.top, r.sourceBounds.right, r.sourceBounds.bottom] : null,
    width: img.width, height: img.height, components: img.components,
    colorSpace: img.colorSpace, colorProfile: img.colorProfile, hasAlpha: img.hasAlpha,
  };
  const copy = new Uint8Array(data);           // dispose 전에 복사
  if (img.dispose) await img.dispose();
  return { info, data: copy };
}

function pickTestPixel(info, data) {
  // 레이어 가운데에서 가장 가까운 '완전히 불투명한' 픽셀 (투명 픽셀을 바꾸면 레이어 경계가 바뀔 수 있으므로)
  const { width: w, height: h, components: c } = info;
  const cx = Math.floor(w / 2), cy = Math.floor(h / 2);
  const maxR = Math.max(w, h);
  for (let r = 0; r < maxR; r++) {
    for (let y = Math.max(0, cy - r); y <= Math.min(h - 1, cy + r); y++) {
      for (let x = Math.max(0, cx - r); x <= Math.min(w - 1, cx + r); x++) {
        if (Math.max(Math.abs(x - cx), Math.abs(y - cy)) !== r) continue;
        const i = (y * w + x) * c;
        if (c === 4 || c === 2) { if (data[i + c - 1] === 255) return { x, y, i }; } else return { x, y, i };
      }
    }
  }
  return null;
}

async function hashFile(fs, formats, url) {
  const entry = await fs.getEntryWithUrl(url);
  const buf = await entry.read({ format: formats.binary });
  return fnv1a(new Uint8Array(buf));
}

async function runPhase1(deps, log) {
  const { app, core, imaging, fs, formats } = deps;
  const say = log || (() => {});
  const checks = [];
  const report = { checks, document: null, layers: [], pixelLayers: [], test: null, savedPath: null, passed: false };
  const check = (step, name, ok, detail) => {
    checks.push({ step, name, ok: !!ok, detail: detail || "" });
    say(`${ok ? "✅" : "❌"} [${step}] ${name}${detail ? " — " + detail : ""}`);
    return ok;
  };
  const modal = (name, fn) => core.executeAsModal(fn, { commandName: name });

  // ---------------------------------------------------------------- 1) 문서 인식
  const doc = app.activeDocument;
  if (!check(1, "열린 문서 인식", !!doc, doc ? doc.name : "Photoshop 에서 PSD 를 먼저 열어 주세요")) return report;
  const docPath = doc.path;
  if (!check(1, "저장된 PSD 파일 경로 확인", !!docPath, docPath || "문서를 먼저 저장해 주세요(새 PSD 를 같은 폴더에 만들기 위해 필요)")) return report;
  report.document = {
    id: doc.id, name: doc.name, path: docPath, width: Number(doc.width), height: Number(doc.height),
    resolution: Number(doc.resolution), mode: String(doc.mode), bitsPerChannel: String(doc.bitsPerChannel),
    colorProfileType: String(doc.colorProfileType), colorProfileName: String(doc.colorProfileName), saved: doc.saved,
  };
  check(1, "문서 정보 읽기", true,
    `${report.document.width}×${report.document.height}px, ${report.document.resolution}ppi, ${report.document.mode}, ${report.document.bitsPerChannel}, 프로파일: ${report.document.colorProfileName}`);
  if (doc.saved === false) check(1, "저장되지 않은 변경 확인", false, "저장되지 않은 변경이 있습니다. 디스크의 원본 기준으로 검사하려면 먼저 저장하세요 (계속 진행함)");
  const origUrl = toFileUrl(docPath);
  let origHash = null;
  try { origHash = await hashFile(fs, formats, origUrl); check(1, "원본 파일 지문 기록", true, origHash); }
  catch (e) { check(1, "원본 파일 지문 기록", false, "원본 파일을 읽지 못함: " + e.message); }

  // ---------------------------------------------------------------- 2) 전체 레이어 탐색
  const tree = walkLayers(doc.layers);
  report.layers = stripRefs(tree);
  const kinds = {};
  tree.forEach((l) => { kinds[l.kind] = (kinds[l.kind] || 0) + 1; });
  check(2, "전체 레이어·그룹 탐색", tree.length > 0, `${tree.length}개 (${Object.entries(kinds).map(([k, v]) => k + " " + v).join(", ")})`);

  // ---------------------------------------------------------------- 3) 픽셀 읽기
  let target = null, targetOrig = null;
  let readOk = 0, readFail = 0;
  await modal("레이어 픽셀 읽기", async () => {
    for (const l of tree) {
      if (l.kind !== "pixel") continue;
      try {
        const { info, data } = await readLayerPixels(imaging, doc.id, l.id);
        const entry = { order: l.order, path: l.path, id: l.id, ...info, hash: fnv1a(data) };
        report.pixelLayers.push(entry);
        readOk++;
        const editable = !l.allLocked && !l.pixelsLocked;
        if (!target && editable && info.width > 0 && info.height > 0) {
          const px = pickTestPixel(info, data);
          if (px) { target = { layer: l, info, px }; targetOrig = data; }
        }
      } catch (e) {
        report.pixelLayers.push({ order: l.order, path: l.path, id: l.id, error: String(e.message || e) });
        readFail++;
      }
    }
  });
  check(3, "픽셀 레이어 데이터 읽기", readOk > 0 && readFail === 0,
    `${readOk}개 성공${readFail ? `, ${readFail}개 실패(빈 레이어일 수 있음)` : ""}`);
  if (!check(3, "테스트할 편집 가능한 픽셀 레이어 찾기", !!target, target ? target.layer.path : "잠기지 않은 픽셀 레이어가 없습니다")) return report;

  // ---------------------------------------------------------------- 4) 복제본에 테스트 픽셀 수정
  const sp = splitPath(docPath);
  let outPath = `${sp.dir}${sp.sep}${sp.base}_UXPTEST.psd`;
  if (outPath.replace(/\\/g, "/") === String(docPath).replace(/\\/g, "/")) outPath = `${sp.dir}${sp.sep}${sp.base}_UXPTEST_2.psd`;
  const { info, px } = target;
  const c = info.components;
  const docX = info.bounds[0] + px.x, docY = info.bounds[1] + px.y;
  const before = Array.from(targetOrig.slice(px.i, px.i + c));
  const after = before.map((v, k) => (k < Math.min(3, c) && !(c === 2 && k === 1) ? 255 - v : v));   // 색만 반전, 알파 유지
  if (after.every((v, k) => v === before[k])) after[0] = (before[0] + 128) % 256;
  report.test = { layer: target.layer.path, layerId: target.layer.id, x: docX, y: docY, before, after, outPath };
  let dup = null;
  try {
    await modal("클린업 테스트: 복제본에 1픽셀 수정", async () => {
      dup = await doc.duplicate(`${sp.base}_UXPTEST`);
      const dupTree = walkLayers(dup.layers);
      const t = dupTree[target.layer.order];
      if (!t || t.path !== target.layer.path) throw new Error("복제본에서 같은 레이어를 찾지 못함");
      // putPixels(replace: true) 는 레이어 전체를 바꾸므로 (실제 Photoshop 확인) 1픽셀만 바꾼 레이어 전체를 넣는다
      const whole = new Uint8Array(targetOrig);
      whole.set(after, px.i);
      const imgData = await imaging.createImageDataFromBuffer(whole, {
        width: info.width, height: info.height, components: c, chunky: true, colorSpace: info.colorSpace, colorProfile: info.colorProfile,
      });
      await imaging.putPixels({ documentID: dup.id, layerID: t.id, imageData: imgData,
                                targetBounds: { left: info.bounds[0], top: info.bounds[1] }, replace: true, commandName: "테스트 픽셀" });
      if (imgData.dispose) await imgData.dispose();
    });
    check(4, "복제본에 테스트 픽셀 1개 수정 (원본은 그대로)", true,
      `${target.layer.path} (x=${docX}, y=${docY}) ${JSON.stringify(before)} → ${JSON.stringify(after)}`);
  } catch (e) {
    check(4, "복제본에 테스트 픽셀 1개 수정", false, String(e.message || e));
    return report;
  }

  // ---------------------------------------------------------------- 5) 새 PSD 로 저장
  try {
    await modal("클린업 테스트: 새 PSD 저장", async () => {
      const entry = await fs.createEntryWithUrl(toFileUrl(outPath), { overwrite: true });
      await dup.saveAs.psd(entry, { embedColorProfile: true }, false);
      dup.closeWithoutSaving();
    });
    report.savedPath = outPath;
    check(5, "새 PSD 파일로 저장 (원본 덮어쓰기 없음)", true, outPath);
  } catch (e) {
    check(5, "새 PSD 파일로 저장", false, String(e.message || e));
    return report;
  }
  if (origHash) {
    try {
      const h2 = await hashFile(fs, formats, origUrl);
      check(5, "원본 PSD 파일이 바뀌지 않음", h2 === origHash, `${origHash} → ${h2}`);
    } catch (e) { check(5, "원본 PSD 파일이 바뀌지 않음", false, e.message); }
  }

  // ---------------------------------------------------------------- 6) 다시 열어 보존 검증
  let re = null;
  try {
    await modal("클린업 테스트: 저장본 다시 열기", async () => {
      re = await app.open(await fs.getEntryWithUrl(toFileUrl(outPath)));
    });
    check(6, "저장한 PSD 를 Photoshop 에서 다시 열기", !!re, re ? re.name : "");
  } catch (e) {
    check(6, "저장한 PSD 를 Photoshop 에서 다시 열기", false, String(e.message || e));
    return report;
  }
  const d = report.document;
  const docDiff = [];
  for (const [k, v] of [["width", Number(re.width)], ["height", Number(re.height)], ["resolution", Number(re.resolution)],
    ["mode", String(re.mode)], ["bitsPerChannel", String(re.bitsPerChannel)], ["colorProfileName", String(re.colorProfileName)]]) {
    if (d[k] !== v) docDiff.push(`${k}: ${d[k]} → ${v}`);
  }
  check(6, "캔버스 크기·해상도·색상 모드·비트·색상 프로파일 유지", docDiff.length === 0, docDiff.join("; "));
  const reTree = walkLayers(re.layers);
  const probs = compareTrees(stripRefs(tree), stripRefs(reTree));
  check(6, "레이어 이름·그룹 구조·순서·가시성·불투명도·블렌드 모드·잠금·위치 유지", probs.length === 0,
    probs.length ? probs.slice(0, 8).join(" | ") + (probs.length > 8 ? ` 외 ${probs.length - 8}건` : "") : `${reTree.length}개 일치`);

  let pixelProblems = [];
  let testOk = false;
  await modal("클린업 테스트: 저장본 픽셀 비교", async () => {
    for (const p of report.pixelLayers) {
      if (p.error) continue;
      const l = reTree[p.order];
      if (!l || l.path !== p.path) { pixelProblems.push(`${p.path}: 저장본에서 레이어를 찾지 못함`); continue; }
      let got;
      try { got = await readLayerPixels(imaging, re.id, l.id); }
      catch (e) { pixelProblems.push(`${p.path}: 픽셀 읽기 실패`); continue; }
      if (p.order !== target.layer.order) {
        if (fnv1a(got.data) !== p.hash || JSON.stringify(got.info.bounds) !== JSON.stringify(p.bounds)) {
          pixelProblems.push(`${p.path}: 수정하지 않은 레이어의 픽셀이 바뀜`);
        }
        continue;
      }
      // 테스트 레이어: 정확히 그 1픽셀만, 정확히 그 값으로 바뀌었는지
      if (JSON.stringify(got.info.bounds) !== JSON.stringify(p.bounds) || got.data.length !== targetOrig.length) {
        pixelProblems.push(`${p.path}: 테스트 레이어 크기가 바뀜`);
        continue;
      }
      let diffs = 0, at = -1;
      for (let i = 0; i < got.data.length; i++) if (got.data[i] !== targetOrig[i]) { diffs++; at = i; }
      const val = Array.from(got.data.slice(px.i, px.i + c));
      testOk = JSON.stringify(val) === JSON.stringify(after) &&
               Math.floor(at / c) === Math.floor(px.i / c) && diffs <= c;
      if (!testOk) pixelProblems.push(`${p.path}: 테스트 픽셀 결과 불일치 (바뀐 값 ${diffs}개, 그 자리 값 ${JSON.stringify(val)})`);
    }
  });
  check(6, "테스트 픽셀이 정확히 1개만, 정확한 값으로 저장됨", testOk, testOk ? `(x=${docX}, y=${docY}) = ${JSON.stringify(after)}` : "");
  check(6, "수정하지 않은 모든 레이어의 픽셀이 그대로임", pixelProblems.filter((s) => !s.includes("테스트")).length === 0,
    pixelProblems.join(" | ") || `${report.pixelLayers.length - 1}개 레이어 지문 일치`);
  try { await modal("클린업 테스트: 저장본 닫기", async () => { re.closeWithoutSaving(); }); } catch (e) { /* 무시 */ }

  report.passed = checks.every((x) => x.ok || x.name === "저장되지 않은 변경 확인");
  say(report.passed ? "🎉 1단계 연동 테스트 통과" : "⚠️ 1단계 연동 테스트에 실패한 항목이 있습니다");
  return report;
}

module.exports = { runPhase1, toFileUrl, splitPath, pickTestPixel };
