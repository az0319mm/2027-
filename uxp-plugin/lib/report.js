// 검수 보고서(HTML 한 파일, 외부 리소스 없음) — ⑥ 을 눌렀을 때만 만들어 저장
"use strict";

const E = require("../engine/engines.js");

const esc = (s) => String(s === undefined || s === null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const TYPE = { apparel: "어패럴", interior: "인테리어" };
const REPEAT = { none: "리핏 없음", horizontal: "좌우", vertical: "상하", both: "상하좌우" };
const STYLE = { watercolor: "수채화", pencil: "색연필", crayon: "크레파스", digital: "디지털 색면" };
const CLS = { certain: "c", uncertain: "u", intentional: "i", unsupported: "x", info: "n" };
const ORDER = { certain: 0, uncertain: 1, unsupported: 2, intentional: 3, info: 4 };

function buildReport(o) {
  // o: { docName, config, cleanup (결과), recheck (결과, 선택), saved: {outPath, passed, checks} (선택), date }
  const r = o.cleanup, s = r.summary, cfg = o.config || {};
  const by = Object.keys(s.findings_by_type || {}).sort().map((t) => {
    const d = s.findings_by_type[t];
    return `<tr><td>${esc(E.TYPE_LABELS[t] || t)}</td>` +
      ["certain", "uncertain", "intentional", "unsupported", "info"].map((c) => `<td>${d[c] || 0}</td>`).join("") + "</tr>";
  }).join("");
  const fs = (r.findings || []).slice().sort((a, b) => (ORDER[a.confidence] - ORDER[b.confidence]) ||
    ((a.layer_index === null ? -1 : a.layer_index) - (b.layer_index === null ? -1 : b.layer_index)) || (a.type < b.type ? -1 : a.type > b.type ? 1 : 0));
  const rows = fs.slice(0, 3000).map((f) => {
    const status = f.applied ? "수정됨" : (f.skip_reason ? "수정 안 함: " + f.skip_reason : "수정 안 함");
    const ev = Object.keys(f.evidence || {}).map((k) => `${k}=${JSON.stringify(f.evidence[k])}`).join(", ");
    return `<tr class="${CLS[f.confidence] || ""}"><td>${esc(f.id)}</td><td>${esc(f.layer_name || "(합성 결과)")}</td>` +
      `<td>${esc(f.type_label)}</td><td>${esc(f.confidence_label)}</td><td>${esc(JSON.stringify(f.bounds))}</td>` +
      `<td>${esc(f.proposed_action)}</td><td>${esc(status)}</td><td>${f.changed_pixel_count || 0}</td>` +
      `<td>${esc(f.pdf_ref)}</td><td class="ev">${esc(ev.slice(0, 300))}</td></tr>`;
  }).join("");
  const more = fs.length > 3000 ? `<p>※ 항목이 많아 3000개만 표시</p>` : "";
  const skipped = (r.skipped_layers || []).map((x) => `<li>${esc(x.path)} [${esc(x.kind)}] — ${esc((x.reasons || []).join("; "))}</li>`).join("");
  const sv = o.saved;
  const checks = sv ? sv.checks.map((c) => `<li class="${c.ok ? "" : "bad"}">${c.ok ? "✅" : "❌"} ${esc(c.name)}${c.detail ? " — " + esc(c.detail) : ""}</li>`).join("") : "";
  const rc = o.recheck ? `<br><b>재검사(수정 후)</b>: 남은 검토 항목 ${o.recheck.summary.unresolved_count}건` : "";
  const style = cfg.work_type === "interior" ? ` / 표현: ${esc(STYLE[cfg.interior_style] || cfg.interior_style)}${cfg.remove_paper ? " / 종이 배경 지우기" : ""}` : (cfg.fix_repeats ? " / 반복 1픽셀도 수정" : "");
  return `<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>PSD 클린업 QA</title>
<style>
body{font-family:system-ui,-apple-system,"Apple SD Gothic Neo",sans-serif;margin:16px;color:#222;background:#fff}
table{border-collapse:collapse;font-size:12px;width:100%} td,th{border:1px solid #ddd;padding:3px 5px;vertical-align:top}
th{background:#f4f4f4} tr.c td{background:#eef9ee} tr.u td{background:#fff7e0} tr.i td{background:#eef3ff} tr.x td{background:#f3f3f3}
.ev{color:#666;max-width:380px;word-break:break-all} .bad{color:#b00} .box{border:1px solid #ccc;padding:10px;border-radius:6px;margin:10px 0}
.warn{background:#fff3f3;border-color:#e99}
</style></head><body>
<h1>PSD 클린업 QA — ${esc(o.docName)}</h1>
<div class="box warn">이 결과는 규칙 기반 자동 검수 보조 결과이며 졸업 검수 통과를 보장하지 않습니다. '불확실' 항목은 직접 확인하세요.</div>
<div class="box"><b>유형</b>: ${esc(TYPE[cfg.work_type] || cfg.work_type)} 엔진${s.separation_mode ? " (분판 모드)" : ""} / 리핏: ${esc(REPEAT[cfg.repeat] || cfg.repeat)}${style}
<br><b>자동 수정</b>: ${s.applied_fixes}건, 변경 픽셀 ${s.changed_pixels_total}px, 수정된 레이어: ${esc((s.patched_layers || []).join(", ") || "없음")}
<br><b>검토 필요(미해결)</b>: ${s.unresolved_count}건${rc}
<br><b>결과 PSD</b>: ${sv ? esc(sv.outPath) + (sv.passed ? ' — <b style="color:green">저장 후 검증 통과</b>' : ' — <b class="bad">검증 실패</b>') : "아직 저장하지 않음"}
<br><b>작성</b>: ${esc(o.date || "")}</div>
${checks ? `<h2>저장 후 검증</h2><ul>${checks}</ul>` : ""}
${skipped ? `<h2>자동 수정에서 제외한 레이어</h2><ul>${skipped}</ul>` : ""}
<h2>유형별 집계</h2><table><tr><th>유형</th><th>확실</th><th>불확실</th><th>의도적</th><th>지원 불가</th><th>정보</th></tr>${by}</table>
<h2>검출 항목</h2>${more}
<table><tr><th>ID</th><th>레이어</th><th>유형</th><th>신뢰도</th><th>위치 (x0,y0,x1,y1)</th><th>제안</th><th>결과</th><th>변경 px</th><th>기준</th><th>근거</th></tr>
${rows}</table></body></html>`;
}

module.exports = { buildReport };
