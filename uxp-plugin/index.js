// Photoshop UXP 패널: 로컬 서버(127.0.0.1:8765)에 클린업을 요청하고, 결과 PSD를 Photoshop에서 다시 열어 대조한다.
// UXP는 Python 프로세스를 직접 실행하지 않는다 → 사용자가 서버를 먼저 실행 (python -m psd_cleanup serve).
const { app, core } = require("photoshop");
const { localFileSystem } = require("uxp").storage;
const { flattenPsLayers, compareLayerTrees } = require("./compare.js");

const SERVER = "http://127.0.0.1:8765";
const H = { "Content-Type": "application/json", "X-PSD-Cleanup": "1" };
const $ = (id) => document.getElementById(id);
const out = (s) => { $("out").textContent = s; };
const msg = (s) => { $("msg").textContent = s; };
const list = (s) => String(s || "").split(",").map((x) => x.trim()).filter(Boolean);

function radioValue(id, dflt) {
  const g = $(id);
  return (g && g.selected) || dflt;
}

$("type").addEventListener("change", () => {
  const t = radioValue("type", "apparel");
  $("apparelBox").style.display = t === "apparel" ? "block" : "none";
  $("interiorBox").style.display = t === "interior" ? "block" : "none";
});

function buildConfig() {
  const t = radioValue("type", "apparel");
  const c = { work_type: t, repeat: radioValue("repeat", "none"),
              protected_layers: list($("protect").value), auto_fix: !$("nofix").checked };
  if (t === "apparel") {
    const n = parseInt($("colorCount").value, 10);
    if (n > 0) c.expected_color_count = n;
    c.allowed_colors = list($("colors").value);
  } else {
    c.interior_style = $("style").value || "digital";
    c.approved_texture_layers = list($("texture").value);
  }
  return c;
}

async function api(path, body) {
  const opt = body === undefined ? { headers: H } : { method: "POST", headers: H, body: JSON.stringify(body) };
  const r = await fetch(SERVER + path, opt);
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}

function activeDocOrThrow() {
  const doc = app.activeDocument;
  if (!doc) throw new Error("열린 문서가 없습니다.");
  if (!doc.path) throw new Error("문서를 먼저 PSD로 저장하세요 (경로 없음).");
  if (doc.saved === false) throw new Error("저장되지 않은 변경이 있습니다. 저장 후 실행하세요 (원본 파일 기준으로 처리).");
  return doc;
}

$("btnHealth").addEventListener("click", async () => {
  try {
    const h = await api("/api/health");
    msg("서버 연결됨 v" + h.version);
  } catch (e) {
    msg("서버 연결 실패: " + e.message + " — 터미널에서 python -m psd_cleanup serve 실행");
  }
});

// Phase 1: Photoshop 이 보는 레이어 구조와 파일 해석기(psd-tools)가 읽은 구조를 대조
$("btnLayers").addEventListener("click", async () => {
  try {
    const doc = activeDocOrThrow();
    const psFlat = flattenPsLayers(doc.layers);
    const srv = await api("/api/inspect", { path: doc.path });
    const cmp = compareLayerTrees(psFlat, srv.layers);
    const lines = [
      `문서: ${doc.name} ${srv.document.width}x${srv.document.height} ${srv.document.color_mode_name} ${srv.document.depth}bit`,
      `Photoshop 레이어 ${psFlat.length}개 / 파일 해석 ${srv.layers.length}개`,
      cmp.ok ? "구조 대조: 일치" : "구조 대조: 불일치",
      ...cmp.problems.map((p) => "✗ " + p),
      ...cmp.warnings.map((w) => "△ " + w),
      "",
      ...srv.layers.slice().reverse().map((l) =>
        `${l.editable ? "✎" : "🔒"} ${l.path} [${l.kind}] ${l.visible ? "" : "(숨김)"} ${l.editable ? "" : l.unsupported_reasons.join(", ")}`),
    ];
    out(lines.join("\n"));
    msg(cmp.ok ? "레이어 읽기 완료" : "구조 불일치 — 이 파일은 자동 수정하지 마세요");
  } catch (e) {
    msg("오류: " + e.message);
  }
});

async function poll(jobId) {
  for (;;) {
    const j = await api("/api/jobs/" + jobId);
    $("prog").value = Math.round((j.progress || 0) * 100);
    msg(j.message || "");
    if (j.status !== "running") return j;
    await new Promise((r) => setTimeout(r, 600));
  }
}

$("btnRun").addEventListener("click", async () => {
  try {
    const doc = activeDocOrThrow();
    const origFlat = flattenPsLayers(doc.layers);
    const { job_id } = await api("/api/run", { inputs: [doc.path], config: buildConfig(), overwrite_outputs: true });
    const job = await poll(job_id);
    const r = (job.results || [])[0];
    if (!r || r.status !== "ok") {
      out("실패: " + ((r && r.error) || job.message));
      return;
    }
    const s = r.summary;
    const lines = [`자동 수정 ${s.applied_fixes}건, 변경 ${s.changed_pixels_total}px`,
                   `파일 검증(psd-tools): ${s.verification_passed ? "통과" : "실패"}`,
                   `결과: ${r.outputs.clean_psd}`].concat(r.outputs.qa_html ? [`보고서: ${r.outputs.qa_html}`] : []);
    // Photoshop 재열기 검증 (명세 5장 2번)
    let reopened = null;
    await core.executeAsModal(async () => {
      const entry = await localFileSystem.getEntryWithUrl("file:" + r.outputs.clean_psd);
      reopened = await app.open(entry);
    }, { commandName: "클린업 결과 열기" });
    if (reopened) {
      const cmp = compareLayerTrees(flattenPsLayers(reopened.layers), r.layers);
      const cmp2 = compareLayerTrees(origFlat, r.layers);
      lines.push(`Photoshop 재열기: 성공 (${reopened.name})`);
      lines.push(`재열기 문서 ↔ 원본 레이어 구조: ${cmp.ok && cmp2.ok ? "일치" : "불일치"}`);
      lines.push(...cmp.problems.map((p) => "✗ " + p), ...cmp.warnings.map((w) => "△ " + w));
    }
    out(lines.join("\n"));
    msg("완료");
  } catch (e) {
    msg("오류: " + e.message);
  }
});
