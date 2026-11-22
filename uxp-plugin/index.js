// Photoshop UXP 패널 진입점
const { app, core, imaging } = require("photoshop");
const uxp = require("uxp");
const fs = uxp.storage.localFileSystem;
const formats = uxp.storage.formats;
const { runPhase1 } = require("./lib/phase1.js");
const { flattenPsLayers, compareLayerTrees } = require("./compare.js");

const SERVER = "http://127.0.0.1:8765";
const H = { "Content-Type": "application/json", "X-PSD-Cleanup": "1" };
const $ = (id) => document.getElementById(id);
const lines = [];
const log = (s) => { lines.push(s); $("out").value = lines.join("\n"); };
const msg = (s) => { $("msg").textContent = s; };
const radio = (id, d) => ($(id) && $(id).selected) || d;

// ------------------------------------------------------------ 1단계
$("btnPhase1").addEventListener("click", async () => {
  lines.length = 0;
  $("btnPhase1").disabled = true;
  msg("1단계 테스트 실행 중… (문서가 크면 1~2분 걸릴 수 있습니다)");
  try {
    const rep = await runPhase1({ app, core, imaging, fs, formats }, log);
    msg(rep.passed ? "1단계 통과 — 아래 결과를 캡처해서 보내 주세요" : "실패한 항목이 있습니다 — 아래 결과를 캡처해서 보내 주세요");
    log("");
    log("---- 레이어 목록 (위→아래) ----");
    (rep.layers || []).forEach((l) => log(`${"  ".repeat(l.depth)}${l.kind === "group" ? "📁" : "▫️"} ${l.name} [${l.kind}] ${l.visible ? "" : "(숨김)"} 불투명도 ${l.opacity}% ${l.blendMode}${l.allLocked || l.pixelsLocked ? " 🔒" : ""}`));
  } catch (e) {
    log("오류: " + (e && e.message ? e.message : e));
    msg("오류가 났습니다 — 아래 내용을 캡처해서 보내 주세요");
  } finally {
    $("btnPhase1").disabled = false;
  }
});

// ------------------------------------------------------------ 2단계 (Python 로컬 서버)
async function api(path, body) {
  const opt = body === undefined ? { headers: H } : { method: "POST", headers: H, body: JSON.stringify(body) };
  const r = await fetch(SERVER + path, opt);
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}

$("btnHealth").addEventListener("click", async () => {
  try { const h = await api("/api/health"); msg("서버 연결됨 v" + h.version); }
  catch (e) { msg("서버 연결 실패: " + e.message + " — run_mac.command 를 먼저 실행하세요"); }
});

$("btnRun").addEventListener("click", async () => {
  lines.length = 0;
  try {
    const doc = app.activeDocument;
    if (!doc || !doc.path) throw new Error("저장된 PSD 를 먼저 여세요");
    const t = radio("type", "apparel");
    const cfg = { work_type: t, repeat: radio("repeat", "none") };
    if (t === "interior") { cfg.interior_style = "watercolor"; cfg.remove_paper = !!$("removePaper").checked; }
    else cfg.fix_repeats = !!$("fixRepeats").checked;
    const origFlat = flattenPsLayers(doc.layers);
    const { job_id } = await api("/api/run", { inputs: [doc.path], config: cfg, overwrite_outputs: true });
    let j;
    for (;;) {
      j = await api("/api/jobs/" + job_id);
      $("prog").value = Math.round((j.progress || 0) * 100);
      msg(j.message || "");
      if (j.status !== "running") break;
      await new Promise((r) => setTimeout(r, 600));
    }
    const r = (j.results || [])[0];
    if (!r || r.status !== "ok") throw new Error((r && r.error) || j.message);
    log(`자동 수정 ${r.summary.applied_fixes}건, 변경 ${r.summary.changed_pixels_total}px`);
    log(`결과: ${r.outputs.clean_psd}`);
    let re = null;
    await core.executeAsModal(async () => {
      re = await app.open(await fs.getEntryWithUrl("file:" + r.outputs.clean_psd));
    }, { commandName: "클린업 결과 열기" });
    const cmp = compareLayerTrees(flattenPsLayers(re.layers), r.layers);
    const cmp2 = compareLayerTrees(origFlat, r.layers);
    log(`Photoshop 재열기: ${re.name} / 레이어 구조 ${cmp.ok && cmp2.ok ? "원본과 일치" : "불일치"}`);
    cmp.problems.concat(cmp.warnings).forEach((p) => log(" - " + p));
    msg("완료");
  } catch (e) {
    msg("오류: " + e.message);
  }
});
