// Photoshop UXP 패널: 분석 → 자동 클린업(복제본) → 전후 비교 → 재검사 → 새 PSD 저장 → 보고서 저장
const { app, core, imaging } = require("photoshop");
const uxp = require("uxp");
const os = require("os");
const fs = uxp.storage.localFileSystem;
const formats = uxp.storage.formats;
const types = uxp.storage.types;
const B = require("./lib/bridge.js");
const E = require("./lib/env.js");
const { runPhase1 } = require("./lib/phase1.js");

const deps = { app, core, imaging, fs, formats, types, os };
const $ = (id) => document.getElementById(id);
const lines = [];
const log = (s) => { lines.push(s); $("out").value = lines.join("\n"); };
const clear = () => { lines.length = 0; $("out").value = ""; };
const msg = (s) => { $("msg").textContent = s; };
const prog = (f) => { $("prog").value = Math.round((f || 0) * 100); };
const radio = (id, d) => ($(id) && $(id).selected) || d;

const state = { srcDoc: null, job: null, result: null, dup: null, saved: null, lastJobDir: null };

function setButtons() {
  const has = !!state.dup;
  $("btnCompare").disabled = !state.result || !state.result.compare_png;
  $("btnRecheck").disabled = !has && !state.saved;
  $("btnSave").disabled = !has;
  $("btnReport").disabled = !state.result;
}

$("type").addEventListener("change", () => {
  const t = radio("type", "apparel");
  $("apparelOpts").style.display = t === "apparel" ? "block" : "none";
  $("interiorOpts").style.display = t === "interior" ? "block" : "none";
});

function config() {
  const t = radio("type", "apparel");
  const c = { work_type: t, repeat: radio("repeat", "none") };
  if (t === "apparel") c.fix_repeats = !!$("fixRepeats").checked;
  else { c.interior_style = radio("style", "watercolor"); c.remove_paper = !!$("removePaper").checked; }
  return c;
}

async function busy(label, fn) {
  const btns = ["btnAnalyze", "btnClean", "btnCompare", "btnRecheck", "btnSave", "btnReport", "btnPhase1"];
  btns.forEach((b) => { $(b).disabled = true; });
  msg(label);
  try { await fn(); }
  catch (e) { log("❌ 오류: " + (e && e.message ? e.message : e)); msg("오류가 났습니다 — 아래 내용을 캡처해서 보내 주세요"); }
  finally { $("btnAnalyze").disabled = false; $("btnClean").disabled = false; $("btnPhase1").disabled = false; setButtons(); }
}

async function dropOldJob() {
  if (state.lastJobDir) { await B.makeIO(deps).removeFolder(state.lastJobDir); state.lastJobDir = null; }
}

async function runEngine(doc, kind) {
  const st = await B.engineStatus(deps);
  if (!st.alive) throw new Error("클린업 엔진이 실행 중이 아닙니다 (마지막 신호 " + (st.age ? Math.round(st.age) + "초 전" : "없음") + "). 'PSD 클린업 엔진 설치.command'를 한 번 실행했는지 확인하세요.");
  await dropOldJob();
  log(`레이어 픽셀 내보내는 중… (${doc.name})`);
  const job = await B.exportJob(deps, doc, config(), kind, () => {});
  state.lastJobDir = job.dir;
  const result = await B.waitResult(deps, job, (f, m) => { prog(f); msg(m || ""); });
  if (result.status !== "ok") throw new Error("엔진 처리 실패: " + result.error);
  return { job, result };
}

function showSummary(r, title) {
  const s = r.summary;
  log(`\n■ ${title}`);
  log(`엔진: ${s.engine === "apparel" ? "어패럴" : "인테리어"}${s.separation_mode ? " (분판 모드)" : ""} · 처리 ${s.elapsed_sec}초`);
  if (r.kind !== "recheck") log(`자동 수정 ${s.applied_fixes}건 · 변경 ${s.changed_pixels_total}px · 수정 레이어: ${s.patched_layers.join(", ") || "없음"}`);
  log(`검토 필요(미해결) ${s.unresolved_count}건`);
  (r.skipped_layers || []).forEach((x) => log(`  건너뜀: ${x.path} [${x.kind}] — ${x.reasons.join("; ")}`));
  const top = (r.unresolved || []).slice(0, 25);
  top.forEach((f) => log(`  · ${f.layer_name || "(합성)"} — ${f.type_label} ${f.bounds ? "@" + f.bounds.slice(0, 2).join(",") : ""} : ${f.proposed_action}`));
  if ((r.unresolved || []).length > top.length) log(`  … 외 ${r.unresolved.length - top.length}건 (검수 보고서에 전체 목록)`);
}

$("btnEnv").addEventListener("click", () => busy("환경 확인 중", async () => {
  const chip = E.describeChip(os.arch());
  const mac = E.describeMacOS(os.platform(), os.release());
  const ps = E.describePhotoshop(uxp.host.version);
  const st = await B.engineStatus(deps);
  $("env").textContent =
    `칩: ${chip.chip} · ${mac.name}${mac.note ? " (" + mac.note + ")" : ""} · ${ps.name} ${ps.ok ? "✅" : "❌ " + ps.note} · ` +
    `클린업 엔진: ${st.alive ? "실행 중 ✅ v" + st.version : "꺼짐 ❌ (엔진 설치 파일을 실행하세요)"}`;
  msg("");
}));

$("btnAnalyze").addEventListener("click", () => busy("분석 중…", async () => {
  clear();
  const doc = app.activeDocument;
  if (!doc) throw new Error("PSD 를 먼저 여세요");
  const { job, result } = await runEngine(doc, "recheck");
  state.result = result; state.job = job;
  showSummary(result, `분석 결과 — ${doc.name}`);
  msg("분석 완료 (아무것도 수정하지 않았습니다)");
}));

$("btnClean").addEventListener("click", () => busy("자동 클린업 중…", async () => {
  clear();
  const doc = app.activeDocument;
  if (!doc) throw new Error("PSD 를 먼저 여세요");
  if (state.dup) { try { await core.executeAsModal(async () => state.dup.closeWithoutSaving(), { commandName: "이전 복제본 닫기" }); } catch (e) { /* */ } state.dup = null; }
  state.srcDoc = doc; state.saved = null;
  const { job, result } = await runEngine(doc, "cleanup");
  state.job = job; state.result = result;
  showSummary(result, `클린업 계획 — ${doc.name}`);
  if (!result.patches.length) { msg("자동으로 고칠 확실한 오류가 없습니다"); return; }
  const ap = await B.applyToDuplicate(deps, doc, job, result, log);
  if (ap.problems.length) {
    ap.problems.forEach((p) => log("  ❌ " + p));
    msg("적용 결과가 예상과 달라 모두 취소했습니다 (원본·복제본 변경 없음)");
    return;
  }
  state.dup = ap.dup;
  log(`✅ 복제본 '${ap.dup.name}'에 ${result.summary.applied_fixes}건 적용 · 적용 직후 픽셀 재확인 통과`);
  msg("복제본에 적용됨 — ③ 전후 비교 / ④ 재검사 / ⑤ 새 PSD로 저장");
}));

$("btnCompare").addEventListener("click", () => busy("전후 비교 열기", async () => {
  const p = `${state.job.dir}/${state.result.compare_png}`;
  await core.executeAsModal(async () => { await app.open(await fs.getEntryWithUrl(B.toFileUrl(p))); },
                            { commandName: "전후 비교 이미지 열기" });
  msg("전후 비교 이미지를 열었습니다 (왼쪽: 수정 전 / 가운데: 수정 후 / 오른쪽: 바뀐 픽셀 빨강). 원본·복제본 문서 탭을 번갈아 봐도 됩니다.");
}));

$("btnRecheck").addEventListener("click", () => busy("남은 오류 재검사 중…", async () => {
  const target = state.dup || app.activeDocument;
  const keep = { job: state.job, result: state.result };
  const { job, result } = await runEngine(target, "recheck");
  showSummary(result, `재검사 결과 — ${target.name}`);
  state.recheck = result;
  // 저장·비교에는 클린업 작업 결과가 필요하므로 되돌림 (재검사 작업 폴더는 바로 정리)
  await B.makeIO(deps).removeFolder(job.dir);
  state.lastJobDir = keep.job ? keep.job.dir : null;
  state.job = keep.job; state.result = keep.result;
  msg("재검사 완료");
}));

$("btnSave").addEventListener("click", () => busy("새 PSD로 저장·검증 중…", async () => {
  const sv = await B.saveAndVerify(deps, state.srcDoc, state.dup, state.job, state.result, log);
  state.dup = null;
  sv.checks.forEach((c) => log(`${c.ok ? "✅" : "❌"} ${c.name}${c.detail ? " — " + c.detail : ""}`));
  if (sv.passed) { state.saved = sv.outPath; log(`✅ 저장 완료: ${sv.outPath}`); msg("저장 및 검증 완료"); }
  else { log(`❌ 검증 실패 → 파일 이름을 ${sv.outPath} 로 바꿨습니다`); msg("검증 실패 — 결과를 캡처해서 보내 주세요"); }
}));

$("btnReport").addEventListener("click", () => busy("보고서 저장 중…", async () => {
  const io = B.makeIO(deps);
  const src = state.srcDoc || app.activeDocument;
  const base = B.splitPath(state.saved || src.path || src.name);
  const html = await io.readText(`${state.job.dir}/report.html`);
  let out;
  if (state.saved || src.path) {
    out = `${base.dir}/${base.base.replace(/_CLEAN$/, "")}_QA.html`;
    await io.writeText(out, html);
  } else {                                           // 저장 안 된 문서 → 위치를 물어봄
    const f = await fs.getFileForSaving(`${base.base}_QA.html`, { types: ["html"] });
    if (!f) { msg("취소했습니다"); return; }
    await f.write(html, { format: formats.utf8 });
    out = f.nativePath;
  }
  log(`✅ 검수 보고서 저장: ${out}`);
  msg("보고서를 PSD 옆에 저장했습니다");
}));

$("btnPhase1").addEventListener("click", () => busy("연동 진단 중…", async () => {
  clear();
  const rep = await runPhase1(deps, log);
  msg(rep.passed ? "연동 진단 통과" : "실패 항목이 있습니다 — 캡처해서 보내 주세요");
}));

setButtons();
