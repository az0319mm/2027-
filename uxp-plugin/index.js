// Photoshop UXP 패널: 분석 → 자동 클린업(복제본) → 전후 비교 → 재검사 → 새 PSD 저장 → 보고서 저장
// 클린업 엔진은 플러그인 안(engine/*.js)에 들어 있어 따로 설치할 프로그램이 없습니다.
const { app, core, imaging } = require("photoshop");
const uxp = require("uxp");
const os = require("os");
const fs = uxp.storage.localFileSystem;
const formats = uxp.storage.formats;
const types = uxp.storage.types;
const R = require("./lib/runner.js");
const E = require("./lib/env.js");
const { buildReport } = require("./lib/report.js");
const { runPhase1 } = require("./lib/phase1.js");

const deps = { app, core, imaging, fs, formats, types, os };
const $ = (id) => document.getElementById(id);
const lines = [];
const log = (s) => { lines.push(s); $("out").value = lines.join("\n"); };
const clear = () => { lines.length = 0; $("out").value = ""; };
const msg = (s) => { $("msg").textContent = s; };
const prog = (f, m) => { $("prog").value = Math.round((f || 0) * 100); if (m) msg(m); };
const radio = (id, d) => ($(id) && $(id).selected) || d;

const state = { srcDoc: null, snap: null, result: null, cfg: null, dup: null, saved: null, recheck: null };

function setButtons() {
  $("btnCompare").disabled = !state.dup;
  $("btnRecheck").disabled = !state.dup && !state.saved;
  $("btnSave").disabled = !state.dup;
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

async function run(doc, kind, cfg) {
  log(`레이어 픽셀 읽는 중… (${doc.name})`);
  prog(0.01, "레이어 픽셀 읽는 중…");
  const snap = await R.readDocument(deps, doc, cfg, () => {});
  const result = await R.analyze(snap, kind, prog);
  prog(1, "");
  return { snap, result };
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
  if ((r.unresolved || []).length > top.length) log(`  … 외 ${r.unresolved.length - top.length}건 (⑥ 검수 보고서에 전체 목록)`);
}

async function closeDup() {
  if (!state.dup) return;
  try { await core.executeAsModal(async () => { await state.dup.closeWithoutSaving(); }, { commandName: "이전 복제본 닫기" }); } catch (e) { /* 이미 닫힘 */ }
  state.dup = null;
}

$("btnEnv").addEventListener("click", () => busy("환경 확인 중", async () => {
  const chip = E.describeChip(os.arch());
  const mac = E.describeMacOS(os.platform(), os.release());
  const ps = E.describePhotoshop(uxp.host.version);
  $("env").textContent =
    `칩: ${chip.chip} · ${mac.name}${mac.note ? " (" + mac.note + ")" : ""} · ${ps.name} ${ps.ok ? "✅" : "❌ " + ps.note} · ` +
    `클린업 엔진: 플러그인 내장 ✅ (따로 설치할 것 없음)`;
  msg("");
}));

$("btnAnalyze").addEventListener("click", () => busy("분석 중…", async () => {
  clear();
  const doc = app.activeDocument;
  if (!doc) throw new Error("PSD 를 먼저 여세요");
  const cfg = config();
  const { result } = await run(doc, "recheck", cfg);
  state.result = result; state.cfg = cfg; state.reportDoc = doc; state.savedInfo = null; state.recheck = null;
  showSummary(result, `분석 결과 — ${doc.name}`);
  msg("분석 완료 (아무것도 수정하지 않았습니다)");
}));

$("btnClean").addEventListener("click", () => busy("자동 클린업 중…", async () => {
  clear();
  const doc = app.activeDocument;
  if (!doc) throw new Error("PSD 를 먼저 여세요");
  await closeDup();
  state.srcDoc = doc; state.saved = null; state.recheck = null;
  const cfg = config();
  const { snap, result } = await run(doc, "cleanup", cfg);
  state.snap = snap; state.result = result; state.cfg = cfg; state.reportDoc = doc; state.savedInfo = null;
  showSummary(result, `클린업 계획 — ${doc.name}`);
  if (!result.patches.length) { msg("자동으로 고칠 확실한 오류가 없습니다"); return; }
  const ap = await R.applyToDuplicate(deps, doc, snap, result, log);
  if (ap.problems.length) {
    ap.problems.forEach((p) => log("  ❌ " + p));
    msg("적용 결과가 예상과 달라 모두 취소했습니다 (원본·복제본 변경 없음)");
    return;
  }
  state.dup = ap.dup;
  log(`✅ 복제본 '${ap.dup.name}'에 ${result.summary.applied_fixes}건 적용 · 적용 직후 픽셀 재확인 통과`);
  msg("복제본에 적용됨 — ③ 전후 비교 / ④ 재검사 / ⑤ 새 PSD로 저장");
}));

$("btnCompare").addEventListener("click", () => busy("전후 비교 문서 만드는 중…", async () => {
  await R.makeCompareDoc(deps, state.srcDoc, state.dup, state.result, log);
  const pts = state.result.changes || [];
  pts.slice(0, 20).forEach(([x, y, p]) => log(`  바뀐 곳: x=${x}, y=${y} (${p})`));
  if (pts.length > 20) log(`  … 외 ${pts.length - 20}곳`);
  msg("'전후비교' 문서: '수정 후' 레이어 눈 아이콘을 껐다 켜며 비교하세요. 빨간 원 = 바뀐 곳. 이 문서는 저장되지 않습니다.");
}));

$("btnRecheck").addEventListener("click", () => busy("남은 오류 재검사 중…", async () => {
  const target = state.dup || app.activeDocument;
  const { result } = await run(target, "recheck", state.cfg || config());
  showSummary(result, `재검사 결과 — ${target.name}`);
  state.recheck = result;
  msg("재검사 완료");
}));

$("btnSave").addEventListener("click", () => busy("새 PSD로 저장·검증 중…", async () => {
  const sv = await R.saveAndVerify(deps, state.srcDoc, state.dup, state.snap, state.result, log);
  state.dup = null;
  state.savedInfo = sv;
  sv.checks.forEach((c) => log(`${c.ok ? "✅" : "❌"} ${c.name}${c.detail ? " — " + c.detail : ""}`));
  if (sv.passed) { state.saved = sv.outPath; log(`✅ 저장 완료: ${sv.outPath}`); msg("저장 및 검증 완료"); }
  else { log(`❌ 검증 실패 → 파일 이름을 ${sv.outPath} 로 바꿨습니다`); msg("검증 실패 — 결과를 캡처해서 보내 주세요"); }
}));

$("btnReport").addEventListener("click", () => busy("보고서 저장 중…", async () => {
  const src = state.reportDoc || app.activeDocument;
  const html = buildReport({ docName: src.name, config: state.cfg, cleanup: state.result, recheck: state.recheck,
                             saved: state.savedInfo, date: new Date().toLocaleString() });
  const saved = state.reportDoc === state.srcDoc ? state.saved : null;
  const base = R.splitPath(saved || src.path || src.name);
  let out;
  if (saved || src.path) {
    out = `${base.dir}/${base.base.replace(/_CLEAN(_\d+)?$/, "")}_QA.html`;
    const f = await fs.createEntryWithUrl(R.toFileUrl(out), { overwrite: true });
    await f.write(html, { format: formats.utf8 });
  } else {                                           // 저장 안 된 문서 → 위치를 물어봄
    const f = await fs.getFileForSaving(`${base.base}_QA.html`, { types: ["html"] });
    if (!f) { msg("취소했습니다"); return; }
    await f.write(html, { format: formats.utf8 });
    out = f.nativePath;
  }
  log(`✅ 검수 보고서 저장: ${out}`);
  msg("보고서를 저장했습니다 (브라우저로 열어 보세요)");
}));

$("btnPhase1").addEventListener("click", () => busy("연동 진단 중…", async () => {
  clear();
  const rep = await runPhase1(deps, log);
  msg(rep.passed ? "연동 진단 통과" : "실패 항목이 있습니다 — 캡처해서 보내 주세요");
}));

setButtons();
