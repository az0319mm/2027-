// node run_bridge.js <scenario> <tmpdir> <python> <repo>
// 플러그인 교환 코드(bridge.js) + 실제 Python 엔진을 끝까지 연결해 실행 (Photoshop 은 모의)
"use strict";
const pathm = require("path");
const fsn = require("fs");
const { execFileSync } = require("child_process");
const { makeEnv, Layer } = require("./mock_disk.js");
const B = require("../../uxp-plugin/lib/bridge.js");

(async () => {
  const [scenario, tmp, py, repo] = process.argv.slice(2);
  const W = 120, H = 90;
  const env = makeEnv(scenario === "corrupt_save" ? { corruptSave: (s) => s.replace('"opacity":80', '"opacity":79') } : {});
  env.engineRoot = pathm.join(tmp, "PSDCleanup");
  env.pollMs = 20;
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
  const job = await B.exportJob(env, doc, { work_type: "apparel" }, "cleanup", (m) => logs.push(m));
  execFileSync(py, ["-m", "psd_cleanup", "process-job", job.dir],
               { cwd: repo, env: Object.assign({}, process.env, { PYTHONPATH: repo }) });
  if (scenario === "tamper") {             // 패치 파일 변조 → 적용 직후 재확인에서 잡혀야 함
    const p = pathm.join(job.dir, "patch_1.raw");
    const b = fsn.readFileSync(p);
    b[0] ^= 0xff;
    fsn.writeFileSync(p, b);
  }
  const result = await B.waitResult(env, job);
  const ap = await B.applyToDuplicate(env, doc, job, result, (m) => logs.push(m));
  let saved = null;
  if (ap.dup) saved = await B.saveAndVerify(env, doc, ap.dup, job, result, (m) => logs.push(m));
  const io = B.makeIO(env);
  await io.removeFolder(job.dir);
  console.log(JSON.stringify({
    status: result.status, error: result.error, applied: result.summary && result.summary.applied_fixes,
    patches: result.patches && result.patches.map((p) => [p.path, p.changed_pixels]),
    skipped: (result.skipped_layers || []).map((x) => x.path), applyProblems: ap.problems,
    saved: saved && { outPath: pathm.basename(saved.outPath), passed: saved.passed, checks: saved.checks },
    originalFile: fsn.readFileSync(origPath, "utf8"),
    originalDocUntouched: doc.layers[0].layers[0].pixels[(30 * W + 60) * 4 + 3] === 255,
    openDocs: env.docs.map((d) => d.name), files: fsn.readdirSync(tmp).sort(),
    jobsLeft: fsn.readdirSync(pathm.join(tmp, "PSDCleanup", "jobs")),
  }));
})().catch((e) => { console.error(e.stack || e); process.exit(1); });
