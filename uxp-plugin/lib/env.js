// Mac 칩·macOS·Photoshop 버전 판정 (순수 함수 → Node 로 테스트)
"use strict";

const DARWIN_TO_MACOS = { 20: "11 Big Sur", 21: "12 Monterey", 22: "13 Ventura", 23: "14 Sonoma", 24: "15 Sequoia", 25: "26 Tahoe" };

function describeChip(arch) {
  const a = String(arch || "").toLowerCase();
  if (a.includes("arm")) return { chip: "Apple Silicon (M 시리즈)", ok: true };
  if (a.includes("64") || a.includes("x86") || a.includes("intel")) return { chip: "Intel", ok: true };
  return { chip: `알 수 없음 (${arch})`, ok: true };
}

function describeMacOS(platform, release) {
  const p = String(platform || "");
  const r = String(release || "");
  if (!/darwin|mac/i.test(p)) return { name: `${p} ${r}`, ok: false, note: "이 플러그인은 macOS 전용입니다" };
  const major = parseInt(r.split(".")[0], 10);
  if (major >= 20 && DARWIN_TO_MACOS[major]) return { name: `macOS ${DARWIN_TO_MACOS[major]} (Darwin ${r})`, ok: true };
  if (major >= 11 && major <= 30) return { name: `macOS ${r}`, ok: true };
  return { name: `macOS (커널 ${r})`, ok: major >= 20, note: major < 20 ? "macOS 11 이상 권장" : "" };
}

function compareVersion(a, b) {
  const pa = String(a).split(".").map((x) => parseInt(x, 10) || 0);
  const pb = String(b).split(".").map((x) => parseInt(x, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    if ((pa[i] || 0) !== (pb[i] || 0)) return (pa[i] || 0) - (pb[i] || 0);
  }
  return 0;
}

function describePhotoshop(version) {
  const ok = compareVersion(version, "24.4") >= 0;
  return { name: `Photoshop ${version}`, ok, note: ok ? "" : "픽셀 읽기·쓰기(Imaging API)는 24.4 이상 필요 — Creative Cloud 에서 업데이트하세요" };
}

module.exports = { describeChip, describeMacOS, describePhotoshop, compareVersion };
