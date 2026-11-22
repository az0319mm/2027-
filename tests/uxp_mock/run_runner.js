// node run_runner.js <scenario> <tmpdir>
// 플러그인 전체 흐름(lib/runner.js + 내장 JS 엔진)을 모의 Photoshop 에서 끝까지 실행 — Python 없음
"use strict";
const pathm = require("path");
const fsn = require("fs");
const { makeEnv, Layer } = require("./mock_disk.js");
const R = require("../../uxp-plugin/lib/runner.js");
const { buildReport } = require("../../uxp-plugin/lib/report.js");

(async () => {
  const [scenario, tmp] = process.argv.slice(2);
  const W = 120, H = 90;
  const env = makeEnv(scenario === "corrupt_save" ? { corruptSave: (s) => s.replace('"opacity":80', '"opacity":79') } : {});
  const motif = new Uint8Array(W * H * 4);
  const set = (x, y, r, g, b, a) => motif.set([r, g, b, a], (y * W + x) * 4);
  for (let y = 10; y < 50; y++) for (let x = 10; x < 60; x++) set(x, y, 220, 80, 120, 255);
  set(60, 30, 220, 80, 120, 255);           // 1px 돌출
  set(30, 25, 10, 200, 10, 255);            // 1px 잘못 칠함
  set(100, 70, 220, 80, 120, 255);          // 고립 점 (레이어 경계 끝 → 지우면 경계가 줄어듦)
  const bg = new Uint8Array(W * H * 3).fill(240);
  const origPath = pathm.join(tmp, "작품.psd");
  fsn.writeFileSync(origPath, "ORIGINAL");
  const doc = new env.Document({ name: "작품.psd", path: origPath, width: W, height: H, layers: [
    new Layer({ name: "그룹", kind: "group", blendMode: "passThrough", bounds: { left: 0, top: 0, right: W, bottom: H }, layers: [
      new Layer({ name: "모티브", kind: "pixel", opacity: 80, blendMode: "multiply",
                  bounds: { left: 10, top: 10, right: 101, bottom: 71 }, pixels: motif })] }),
    new Layer({ name: "스마트", kind: "smartObject", bounds: { left: 0, top: 0, right: 5, bottom: 5 } }),
    new Layer({ name: "배경", kind: "pixel", isBackgroundLayer: true, components: 3,
                bounds: { left: 0, top: 0, right: W, bottom: H }, pixels: bg }),
  ] });
  env.app.activeDocument = doc;
  const logs = [];
  const cfg = { work_type: "apparel", repeat: "none" };
  const snap = await R.readDocument(env, doc, cfg);
  const analysis = await R.analyze(snap, "recheck");
  const result = await R.analyze(snap, "cleanup", () => {});
  if (scenario === "tamper") result.patches[0].data[0] ^= 0xff;    // 패치 변조 → 적용 직후 재확인에서 잡혀야 함
  const ap = await R.applyToDuplicate(env, doc, snap, result, (m) => logs.push(m));
  let saved = null, compare = null, recheck = null;
  if (ap.dup) {
    const cmp = await R.makeCompareDoc(env, doc, ap.dup, result, (m) => logs.push(m));
    compare = { name: cmp.name, layers: cmp.layers.map((l) => l.name),
                red: Array.from(cmp.layers[0].pixels).filter((v, i) => i % 4 === 3 && v === 255).length };
    cmp.closeWithoutSaving();
    const rsnap = await R.readDocument(env, ap.dup, cfg);
    recheck = await R.analyze(rsnap, "recheck");
    saved = await R.saveAndVerify(env, doc, ap.dup, snap, result, (m) => logs.push(m));
  }
  const html = buildReport({ docName: doc.name, config: cfg, cleanup: result, recheck, saved, date: "test" });
  console.log(JSON.stringify({
    analysisApplied: analysis.summary.applied_fixes, analysisPatches: analysis.patches.length,
    applied: result.summary.applied_fixes,
    patches: result.patches.map((p) => [p.path, p.changed_pixels]), changes: result.changes.map((c) => c.slice(0, 2)),
    skipped: result.skipped_layers.map((x) => x.path), applyProblems: ap.problems, compare,
    recheckApplicable: recheck && recheck.summary.findings_by_confidence.certain || 0,
    saved: saved && { outPath: pathm.basename(saved.outPath), passed: saved.passed, checks: saved.checks },
    reportOk: html.includes("PSD 클린업 QA") && html.includes("작품.psd"),
    originalFile: fsn.readFileSync(origPath, "utf8"),
    originalDocUntouched: doc.layers[0].layers[0].pixels[(30 * W + 60) * 4 + 3] === 255,
    openDocs: env.docs.map((d) => d.name), files: fsn.readdirSync(tmp).sort(),
  }));
})().catch((e) => { console.error(e.stack || e); process.exit(1); });
