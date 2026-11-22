"""Photoshop 플러그인 ↔ Python 클린업 엔진: 로컬 파일 교환 (macOS 우선).

공유 폴더: ~/PSDCleanup  (홈 폴더 바로 아래 → macOS '문서/데스크탑' 보호 폴더 권한 팝업이 필요 없음)
  engine.json            엔진 생존 신호 (몇 초마다 갱신)
  jobs/<job_id>/         작업 하나
     request.json        플러그인이 마지막에 써서 '준비 완료'를 알림
     layer_*.raw, composite.raw
     progress.json       엔진 진행률
     result.json         엔진 결과 (원자적 쓰기)
     patch_<order>.raw   레이어별 '바뀌는 영역' 픽셀 (플러그인이 putPixels 로 원래 위치에 씀)
     compare.png, report.html, report.json
작업 폴더는 플러그인이 결과를 적용한 뒤 지우며, 엔진도 24시간 지난 작업 폴더를 정리한다.
네트워크를 사용하지 않는다.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import traceback
from pathlib import Path

import numpy as np

from . import __version__
from .config import ProjectConfig

ROOT = Path(os.environ.get("PSD_CLEANUP_HOME", Path.home() / "PSDCleanup"))


def crc32_hex(data: bytes | np.ndarray) -> str:
    """플러그인(layers.js)의 crc32 와 같은 지문 (zlib 표준 CRC-32)."""
    import zlib
    b = np.ascontiguousarray(data).tobytes() if isinstance(data, np.ndarray) else bytes(data)
    return "%08x" % (zlib.crc32(b) & 0xFFFFFFFF)


def _atomic_write(path: Path, data: bytes) -> None:
    import threading
    # 스레드마다 다른 임시 이름 (생존 신호 스레드와 작업 처리가 같은 파일을 동시에 쓰면 이름이 겹쳐 실패하던 문제)
    tmp = path.with_name(f".tmp_{os.getpid()}_{threading.get_ident()}_" + path.name)
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write_json(path: Path, obj) -> None:
    _atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=1, default=str).encode("utf-8"))


def _trim_layer(rgba: np.ndarray, comps: int, left: int, top: int):
    """Photoshop getPixels 처럼 '픽셀이 있는 영역'으로 잘라낸 결과 (지문 비교용)."""
    if comps == 4:
        ys, xs = np.nonzero(rgba[..., 3] > 0)
        if len(ys) == 0:
            return None, None
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        sub = rgba[y0:y1, x0:x1].copy()
        sub[sub[..., 3] == 0, :3] = 0            # 완전 투명 픽셀의 색값은 의미 없음 → 0 으로 정규화
        return sub, [int(left + x0), int(top + y0), int(left + x1), int(top + y1)]
    return rgba[..., :3].copy(), [left, top, left + rgba.shape[1], top + rgba.shape[0]]


def process_job(job_dir: str | Path) -> dict:
    from .memdoc import MemoryDocument
    from .pipeline import plan_cleanup
    from .findings import CERTAIN

    job = Path(job_dir)
    t0 = time.time()

    def prog(f, m):
        try:
            _write_json(job / "progress.json", {"progress": round(float(f), 3), "message": m, "time": time.time()})
        except OSError:
            pass

    doc = MemoryDocument(job)
    cfg_d = dict(doc.req.get("config") or {})
    cfg_d["write_reports"] = False
    if doc.req.get("kind") == "recheck":
        cfg_d["auto_fix"] = False
    cfg = ProjectConfig.from_dict(cfg_d)
    prog(0.05, "분석 시작")
    p = plan_cleanup(doc, cfg, prog)

    patches = []
    by_order = {int(L["order"]): L for L in doc.req["layers"]}
    n = len(doc.req["layers"])
    for idx, new in sorted(p.expected_new.items()):
        L = by_order[n - 1 - idx]
        comps = int(L["components"])
        orig = doc.read_rgba(idx)
        vis = lambda x: np.where(x[..., 3:4] == 0, 0, x)  # noqa: E731
        changed = np.any(vis(new) != vis(orig), axis=2)
        if not changed.any():
            continue
        ys, xs = np.nonzero(changed)
        y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
        region = new[y0:y1, x0:x1]
        if comps == 3:
            region = region[..., :3]
        fname = f"patch_{int(L['order'])}.raw"
        _atomic_write(job / fname, np.ascontiguousarray(region).tobytes())
        b = L["bounds"]
        full = new if comps == 4 else new[..., :3]
        trimmed, tb = _trim_layer(full if comps == 4 else new, comps, b[0], b[1])
        patches.append({
            "order": int(L["order"]), "id": L.get("id"), "path": L.get("path"), "file": fname,
            "left": int(b[0] + x0), "top": int(b[1] + y0), "width": x1 - x0, "height": y1 - y0, "components": comps,
            "changed_pixels": int(changed.sum()),
            "expected_bounds": tb, "expected_hash": crc32_hex(trimmed) if trimmed is not None else None,
        })

    # 비교 이미지 (근사 합성: 보이는 픽셀 레이어 Normal 합성)
    compare = None
    if p.expected_new and doc.doc_info.pixel_edit_supported:
        try:
            from . import report
            before = doc.simple_composite()
            after = doc.simple_composite(override=p.expected_new)
            changed_canvas = np.any(before != after, axis=2)
            sheet = report.fixes_sheet(before, after, changed_canvas, max_items=40)
            sheet.save(job / "compare.png")
            compare = "compare.png"
        except Exception:  # 비교 이미지는 보조 기능
            compare = None

    findings = [f.to_dict() for f in p.findings]
    counts: dict[str, int] = {}
    by_type: dict[str, dict[str, int]] = {}
    for f in p.findings:
        counts[f.confidence] = counts.get(f.confidence, 0) + 1
        d = by_type.setdefault(f.type, {})
        d[f.confidence] = d.get(f.confidence, 0) + 1
    unresolved = [f for f in findings if f["confidence"] in ("uncertain", "unsupported") or
                  (f["confidence"] == CERTAIN and not f["applied"])]
    result = {
        "status": "ok", "engine_version": __version__, "kind": doc.req.get("kind", "cleanup"),
        "summary": {
            "engine": p.engine.name, "applied_fixes": sum(1 for f in p.findings if f.applied),
            "changed_pixels_total": int(sum(x["changed_pixels"] for x in patches)),
            "patched_layers": [x["path"] for x in patches], "separation_mode": bool(p.sep),
            "findings_by_confidence": counts, "findings_by_type": by_type,
            "unresolved_count": len(unresolved), "elapsed_sec": round(time.time() - t0, 2),
        },
        "document": doc.doc_info.to_dict(),
        "skipped_layers": [{"path": l.path, "kind": l.kind, "reasons": l.unsupported_reasons}
                           for l in doc.layers if not l.editable and l.kind != "group"],
        "patches": patches, "compare_png": compare,
        "unresolved": unresolved[:3000], "findings": findings[:5000],
    }
    # 보고서 (요청 시 플러그인이 PSD 옆으로 복사)
    try:
        _write_report_html(job / "report.html", result, doc.req)
        _write_json(job / "report.json", result)
    except Exception:
        pass
    prog(1.0, "완료")
    return result


def _write_report_html(path: Path, result: dict, req: dict) -> None:
    import html as H
    e = H.escape
    s = result["summary"]
    rows = "".join(
        f"<tr><td>{e(str(f.get('layer_name') or '(합성)'))}</td><td>{e(f['type_label'])}</td><td>{e(f['confidence_label'])}</td>"
        f"<td>{e(str(f.get('bounds')))}</td><td>{'수정됨' if f['applied'] else e(f.get('skip_reason') or '수정 안 함')}</td>"
        f"<td>{e(f['proposed_action'])}</td></tr>" for f in result["findings"][:3000] if f["confidence"] != "info")
    skipped = "".join(f"<li>{e(x['path'])} [{e(x['kind'])}] — {e('; '.join(x['reasons']))}</li>" for x in result["skipped_layers"])
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>PSD 클린업 보고서</title>
<style>body{{font-family:-apple-system,"Apple SD Gothic Neo",sans-serif;margin:16px}}table{{border-collapse:collapse;font-size:12px}}
td,th{{border:1px solid #ccc;padding:3px 6px}}.w{{background:#fff3f3;border:1px solid #e99;padding:8px}}</style></head><body>
<h1>PSD 클린업 보고서 — {e(req['document'].get('name', ''))}</h1>
<p class="w">규칙 기반 자동 클린업 결과입니다. 졸업 검수 통과를 보장하지 않으며 '불확실' 항목은 사람이 확인해야 합니다.</p>
<p>엔진: {e(s['engine'])} · 자동 수정 {s['applied_fixes']}건 · 변경 {s['changed_pixels_total']}px ·
수정된 레이어: {e(', '.join(s['patched_layers']) or '없음')} · 미해결 {s['unresolved_count']}건 · 분판 모드: {'예' if s['separation_mode'] else '아니오'}</p>
<h2>건너뛴 레이어</h2><ul>{skipped or '<li>없음</li>'}</ul>
<h2>검출 항목</h2><table><tr><th>레이어</th><th>유형</th><th>신뢰도</th><th>위치</th><th>결과</th><th>제안</th></tr>{rows}</table>
</body></html>"""
    path.write_text(page, encoding="utf-8")


# ---------------------------------------------------------------- 백그라운드 감시
def _heartbeat(state: str = "idle", job: str | None = None) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    _write_json(ROOT / "engine.json", {"version": __version__, "pid": os.getpid(), "time": time.time(),
                                       "state": state, "job": job})


def cleanup_old_jobs(max_age_h: float = 24.0) -> int:
    jobs = ROOT / "jobs"
    n = 0
    if not jobs.exists():
        return 0
    for d in jobs.iterdir():
        try:
            if d.is_dir() and time.time() - d.stat().st_mtime > max_age_h * 3600:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            pass
    return n


STATE = {"state": "idle", "job": None}


def recover_interrupted_jobs() -> int:
    """엔진이 처리 도중 꺼졌던 작업(.processing 만 남고 결과 없음)에 실패 결과를 남겨 플러그인이 기다리지 않게 함."""
    n = 0
    jobs = ROOT / "jobs"
    if not jobs.exists():
        return 0
    for d in jobs.iterdir():
        if d.is_dir() and (d / ".processing").exists() and not (d / "result.json").exists():
            _write_json(d / "result.json", {"status": "failed",
                                            "error": "엔진이 처리 도중 중단되었습니다 (메모리 부족 등). 다시 시도해 주세요."})
            try:
                (d / ".processing").unlink()
            except OSError:
                pass
            n += 1
    return n


def handle_one(job: Path) -> None:
    marker = job / ".processing"
    if (job / "result.json").exists() or marker.exists() or not (job / "request.json").exists():
        return
    marker.write_text(str(os.getpid()))
    STATE.update(state="busy", job=job.name)
    _heartbeat("busy", job.name)
    try:
        res = process_job(job)
    except Exception as ex:
        res = {"status": "failed", "error": f"{type(ex).__name__}: {ex}", "traceback": traceback.format_exc(limit=6)}
    _write_json(job / "result.json", res)
    try:
        marker.unlink()
    except OSError:
        pass
    STATE.update(state="idle", job=None)
    _heartbeat("idle")


def watch(poll: float = 0.5, once: bool = False) -> None:
    import fcntl
    (ROOT / "jobs").mkdir(parents=True, exist_ok=True)
    lock = open(ROOT / "engine.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)        # 엔진은 한 개만 실행
    except OSError:
        print("이미 다른 엔진이 실행 중입니다.")
        return
    cleanup_old_jobs()
    recover_interrupted_jobs()
    # 살아 있음 신호: 큰 PSD 를 처리하는 동안에도 2초마다 갱신 (별도 스레드)
    import threading
    stop = threading.Event()

    def beat():
        while not stop.is_set():
            try:
                _heartbeat(STATE["state"], STATE["job"])
            except OSError:
                pass
            stop.wait(2.0)
    threading.Thread(target=beat, daemon=True).start()
    print(f"PSD 클린업 엔진 v{__version__} 감시 중: {ROOT / 'jobs'}", flush=True)
    try:
        while True:
            try:
                jobs = sorted((ROOT / "jobs").iterdir())
            except OSError:
                (ROOT / "jobs").mkdir(parents=True, exist_ok=True)
                jobs = []
            for job in jobs:
                if job.is_dir() and not job.name.startswith("."):     # '.' 으로 시작 = 플러그인이 아직 쓰는 중
                    try:
                        handle_one(job)
                    except Exception as ex:                          # 플러그인이 작업 폴더를 지운 경우 등 → 엔진은 계속
                        print(f"작업 처리 중 오류(무시하고 계속): {job.name}: {ex}", flush=True)
                        STATE.update(state="idle", job=None)
            if once:
                return
            time.sleep(poll)
    finally:
        stop.set()


def selftest() -> bool:
    """설치 직후 자가 테스트: 작은 작업 폴더를 만들어 처리하고 결과를 확인."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        job = Path(td) / "selftest"
        job.mkdir()
        W, H = 60, 40
        m = np.zeros((H, W, 4), np.uint8)
        m[5:30, 5:40] = (220, 80, 120, 255)
        m[20, 40] = (220, 80, 120, 255)            # 곧은 경계 1px 돌출
        m[15, 20] = (10, 200, 10, 255)             # 1px 잘못 칠함
        m[35, 55] = (220, 80, 120, 255)            # 고립 점
        ys, xs = np.nonzero(m[..., 3])
        sub = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        sub.tofile(job / "layer_0.raw")
        req = {"version": 1, "kind": "cleanup",
               "document": {"name": "selftest", "width": W, "height": H, "mode": "RGBColorMode", "bitsPerChannel": "bitDepth8"},
               "config": {"work_type": "apparel"},
               "layers": [{"order": 0, "id": 1, "name": "m", "path": "m", "kind": "pixel", "visible": True,
                           "bounds": [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1],
                           "components": 4, "file": "layer_0.raw"}]}
        (job / "request.json").write_text(json.dumps(req), encoding="utf-8")
        r = process_job(job)
        ok = r["status"] == "ok" and r["summary"]["applied_fixes"] == 3 and len(r["patches"]) == 1
        print("엔진 자가 테스트:", "통과" if ok else "실패", r["summary"])
        return ok
