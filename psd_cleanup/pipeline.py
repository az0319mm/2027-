"""분석 → 안전한 오류만 수정 → 원본과 비교 검증 → 결과 PSD 저장 → 보고서 출력."""
from __future__ import annotations

import fnmatch
import json
import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import __version__
from . import repeat as R
from .config import ProjectConfig, parse_hex
from .engines import get_engine
from .findings import CERTAIN, INFO, INTENTIONAL, UNCERTAIN, UNSUPPORTED, Finding
from .psd_io import PSDDocument, sha256_file, write_patched_psd

ProgressFn = Callable[[float, str], None]


class Cancelled(Exception):
    pass


@dataclass
class RunResult:
    input_path: str
    status: str = "pending"          # ok / failed / cancelled
    outputs: dict[str, str] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    qa: dict[str, Any] = field(default_factory=dict)


def output_paths(src: Path, out_dir: Path) -> dict[str, Path]:
    stem = src.stem
    return {
        "clean_psd": out_dir / f"{stem}_CLEAN{src.suffix.lower()}",
        "qa_html": out_dir / f"{stem}_QA.html",
        "qa_json": out_dir / f"{stem}_QA.json",
        "diff_png": out_dir / f"{stem}_DIFF.png",
        "fixes_png": out_dir / f"{stem}_FIXES.png",
        "repeat_png": out_dir / f"{stem}_repeat_preview.png",
        "bgcheck_png": out_dir / f"{stem}_BGCHECK.png",
    }


def _match_any(patterns: list[str], *names: str) -> bool:
    return any(fnmatch.fnmatchcase(n, p) for p in patterns for n in names)


def _intersects(b, r) -> bool:
    x0, y0, x1, y1 = b
    return x0 < r.x + r.w and r.x < x1 and y0 < r.y + r.h and r.y < y1


def run_file(src: str | Path, cfg: ProjectConfig, out_dir: str | Path | None = None, *,
             overwrite_outputs: bool = False, progress: ProgressFn | None = None,
             cancel: threading.Event | None = None) -> RunResult:
    cfg.validate()
    src = Path(src).resolve()
    out_dir = Path(out_dir).resolve() if out_dir else src.parent / "_cleanup_output"
    paths = output_paths(src, out_dir)
    res = RunResult(input_path=str(src))
    prog = progress or (lambda f, m: None)

    def check_cancel():
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    if any(p.resolve() == src for p in paths.values()):
        raise ValueError("출력 경로가 원본과 같습니다.")
    if paths["clean_psd"].exists() and not overwrite_outputs:
        raise FileExistsError(f"출력 파일이 이미 있습니다: {paths['clean_psd']} (--overwrite-outputs 로 결과 파일만 덮어쓰기)")

    t0 = time.time()
    prog(0.02, "PSD 읽는 중")
    doc = PSDDocument(src)
    sha_before = doc.sha256
    engine = get_engine(cfg)
    findings: list[Finding] = []
    layer_rgba: dict[int, np.ndarray] = {}
    analyzed_layers = []

    W, H = doc.doc_info.width, doc.doc_info.height
    if not doc.doc_info.pixel_edit_supported:
        findings.append(Finding(type="document", engine=engine.name, scope="document", confidence=UNSUPPORTED,
                                proposed_action="픽셀 분석/수정 건너뜀, 구조 정보만 보고",
                                evidence={"reasons": doc.doc_info.unsupported_reasons}))

    n_layers = max(1, len(doc.layers))
    for i, layer in enumerate(doc.layers):
        check_cancel()
        prog(0.05 + 0.6 * i / n_layers, f"레이어 검사: {layer.path}")
        if layer.kind == "group":
            continue
        if layer.kind != "pixel":
            findings.append(engine._f(layer, type="unsupported_layer", confidence=UNSUPPORTED,
                                      proposed_action="검사·수정 제외 (원본 그대로 유지)",
                                      evidence={"kind": layer.kind}, bounds=tuple(layer.bbox)))
            continue
        rgba = doc.read_rgba(layer.index) if doc.doc_info.pixel_edit_supported else None
        if rgba is None:
            findings += engine.hygiene(layer, None)
            continue
        approved = _match_any(cfg.approved_texture_layers, layer.name, layer.path)
        lf = engine.analyze_layer(layer, rgba, approved_texture=approved)
        lf += R.seam_findings(rgba, layer.bbox[0], layer.bbox[1], W, H, cfg.repeat, engine.name, layer)
        seams = [f.bounds for f in lf if f.type in ("internal_seam", "repeat_discontinuity") and f.bounds]
        if seams:   # 리핏 이음매 위 픽셀은 리핏을 먼저 바로잡아야 함 → 그 근처 자동 수정 보류
            for f in lf:
                if f.confidence == CERTAIN and f.bounds and any(
                        f.bounds[0] < b[2] + 2 and b[0] - 2 < f.bounds[2] and
                        f.bounds[1] < b[3] + 2 and b[1] - 2 < f.bounds[3] for b in seams):
                    f.confidence, f.fix = UNCERTAIN, None
                    f.proposed_action = "보고만 (리핏 이음매 위 — 리핏을 먼저 맞춘 뒤 다시 검사)"
        protected = _match_any(cfg.protected_layers, layer.name, layer.path)
        for f in lf:
            if f.confidence != CERTAIN:
                continue
            if protected:
                f.confidence, f.skip_reason = INTENTIONAL, "보호 레이어(프로젝트 설정)"
            elif f.bounds and any(_intersects(f.bounds, r) and (not r.layers or _match_any(r.layers, layer.name, layer.path))
                                  for r in cfg.protected_regions):
                f.confidence, f.skip_reason = INTENTIONAL, "보호 영역(프로젝트 설정)"
            elif not layer.editable:
                f.confidence, f.skip_reason = UNSUPPORTED, "; ".join(layer.unsupported_reasons)
        findings += lf
        layer_rgba[layer.index] = rgba
        analyzed_layers.append(layer.index)

    if hasattr(engine, "analyze_separation") and cfg.one_color_per_layer:
        lc = {}
        for idx, rgba in layer_rgba.items():
            l = doc.layer_by_index(idx)
            if l.effective_visible and l.has_alpha_channel:
                px = rgba[rgba[..., 3] == 255][:, :3].astype(np.int64)
                if len(px):
                    keys = np.unique((px[:, 0] << 16) | (px[:, 1] << 8) | px[:, 2])[:4096]
                    lc[l.path] = {(int(k) >> 16, (int(k) >> 8) & 255, int(k) & 255) for k in keys}
        findings += engine.analyze_separation(lc)

    check_cancel()
    prog(0.68, "합성 결과 검사")
    merged = doc.merged_rgb()
    composite_source = "stored_merged_image"
    if merged is None and doc.doc_info.pixel_edit_supported:
        from .psd_io import _composite_rgb8
        merged = _composite_rgb8(doc.psd).astype(np.uint8)
        composite_source = "psd_tools_composite"
    seam_comp: list[Finding] = []
    if merged is not None:
        findings += engine.analyze_composite(merged)
        rgba_m = np.dstack([merged, np.full((H, W), 255, np.uint8)])
        seam_comp = R.seam_findings(rgba_m, 0, 0, W, H, cfg.repeat, engine.name, None)
        findings += seam_comp

    # ----------------------------------------------------------- 수정 적용
    check_cancel()
    prog(0.72, "안전한 수정 적용")
    patches: dict[int, dict[int, np.ndarray]] = {}
    expected_new: dict[int, np.ndarray] = {}
    fix_types = engine.fix_types
    by_layer: dict[int, list[Finding]] = {}
    for f in findings:
        if f.confidence == CERTAIN and f.fix is not None and f.layer_index is not None:
            if not cfg.auto_fix:
                f.skip_reason = "검수 전용 모드(auto_fix=false)"
            elif f.type not in fix_types:
                f.skip_reason = f"{engine.name} 엔진에서 자동 수정하지 않는 유형"
            else:
                by_layer.setdefault(f.layer_index, []).append(f)

    palette = {tuple(parse_hex(c)) for c in cfg.allowed_colors}
    for idx, fl in by_layer.items():
        layer = doc.layer_by_index(idx)
        orig = layer_rgba[idx]
        new = orig.copy()
        touched = np.zeros(orig.shape[:2], dtype=bool)
        applied = []
        for f in fl:
            fx = f.fix
            if touched[fx.ys, fx.xs].any():
                f.skip_reason = "다른 수정과 픽셀 충돌 → 건너뜀"
                continue
            if not layer.has_alpha_channel and (fx.new[:, 3] != 255).any():
                f.skip_reason = "투명 채널 없는 레이어(배경)에서 투명 처리 불가"
                continue
            new[fx.ys, fx.xs] = fx.new
            touched[fx.ys, fx.xs] = True
            applied.append(f)
        changed = np.any(new != orig, axis=2)
        # 완전 투명 픽셀은 RGB 값이 달라도 시각적으로 같으므로 변경으로 치지 않는다
        changed &= ~((new[..., 3] == 0) & (orig[..., 3] == 0))
        n_changed = int(changed.sum())
        limit = max(64, int(cfg.max_layer_change_ratio * orig.shape[0] * orig.shape[1]))
        reason = None
        if n_changed > limit:
            reason = f"레이어 변경량 {n_changed}px 가 안전 한도 {limit}px 초과 → 이 레이어 수정 전부 취소"
        if reason is None and engine.name == "apparel":
            # 색상 테스트: 수정으로 새 색이 생기면 안 됨 (원래 레이어에 있던 색 또는 허용 색만)
            existing = set(map(tuple, orig[orig[..., 3] > 0][:, :3].reshape(-1, 3).tolist()))
            ys, xs = np.nonzero(changed & (new[..., 3] > 0))
            newcols = set(map(tuple, new[ys, xs][:, :3].tolist()))
            bad = newcols - existing - palette
            if bad:
                reason = f"수정 결과에 기존에 없던 색 {len(bad)}개 → 이 레이어 수정 취소"
        if reason:
            for f in applied:
                f.skip_reason = reason
            continue
        for f in applied:
            fx = f.fix
            f.applied = True
            f.changed_pixel_count = int(changed[fx.ys, fx.xs].sum())
        if n_changed == 0:
            continue
        chmap = {}
        ids = layer.channel_ids
        color_ids = [0, 1, 2] if doc.doc_info.color_mode == 3 else [0]
        for ci, cid in enumerate(color_ids):
            src_c = ci if doc.doc_info.color_mode == 3 else 0
            if cid in ids and not np.array_equal(new[..., src_c], orig[..., src_c]):
                chmap[cid] = new[..., src_c]
        if -1 in ids and not np.array_equal(new[..., 3], orig[..., 3]):
            chmap[-1] = new[..., 3]
        if chmap:
            patches[idx] = chmap
            expected_new[idx] = new

    # ----------------------------------------------------------- 저장
    check_cancel()
    prog(0.8, "결과 PSD 저장")
    out_dir.mkdir(parents=True, exist_ok=True)
    write_info: dict[str, Any] = {"patched_layers": [], "merged_image_updated": False}
    if patches:
        write_info = write_patched_psd(src, paths["clean_psd"], patches)
    else:
        tmp = paths["clean_psd"].with_name(".tmp_copy_" + paths["clean_psd"].name)
        shutil.copyfile(src, tmp)
        tmp.replace(paths["clean_psd"])
        write_info["note"] = "수정 사항 없음 → 원본과 바이트 동일한 사본 저장"

    prog(0.86, "저장 결과 재검증")
    from .verify import verify_output
    verification = verify_output(src, sha_before, paths["clean_psd"], doc, patches, expected_new)

    # ----------------------------------------------------------- 보고서
    prog(0.92, "보고서 생성")
    from . import report
    clean_doc = PSDDocument(paths["clean_psd"])
    merged_after = clean_doc.merged_rgb()
    changed_canvas = np.zeros((H, W), dtype=bool)
    for idx, new in expected_new.items():
        l = doc.layer_by_index(idx)
        orig = layer_rgba[idx]
        ch = np.any(new != orig, axis=2) & ~((new[..., 3] == 0) & (orig[..., 3] == 0))
        x0, y0 = l.bbox[0], l.bbox[1]
        ys, xs = np.nonzero(ch)
        ys, xs = ys + y0, xs + x0
        ok = (ys >= 0) & (ys < H) & (xs >= 0) & (xs < W)
        changed_canvas[ys[ok], xs[ok]] = True
    if merged is not None:
        report.diff_image(merged, changed_canvas).save(paths["diff_png"])
        report.fixes_sheet(merged, merged_after if merged_after is not None else merged,
                           changed_canvas).save(paths["fixes_png"])
        res.outputs["diff_png"] = str(paths["diff_png"])
        res.outputs["fixes_png"] = str(paths["fixes_png"])
        if cfg.repeat != "none":
            R.tile_preview(merged_after if merged_after is not None else merged, cfg.repeat,
                           seam_comp).save(paths["repeat_png"])
            res.outputs["repeat_png"] = str(paths["repeat_png"])
    if engine.name == "interior" and layer_rgba:
        img = report.background_check(doc, layer_rgba, W, H)
        if img is not None:
            after_rgba = {**layer_rgba, **expected_new}
            img2 = report.background_check(doc, after_rgba, W, H)
            from PIL import Image as _I
            both = _I.new("RGB", (img.width, img.height * 2 + 6), (128, 128, 128))
            both.paste(img, (0, 0))          # 위: 원본
            both.paste(img2, (0, img.height + 6))  # 아래: 클린업 결과
            both.save(paths["bgcheck_png"])
            res.outputs["bgcheck_png"] = str(paths["bgcheck_png"])

    counts: dict[str, int] = {}
    for f in findings:
        counts[f.confidence] = counts.get(f.confidence, 0) + 1
    by_type: dict[str, dict[str, int]] = {}
    for f in findings:
        d = by_type.setdefault(f.type, {})
        d[f.confidence] = d.get(f.confidence, 0) + 1
    res.summary = {
        "engine": engine.name, "work_type": cfg.work_type, "repeat": cfg.repeat,
        "findings_by_confidence": counts, "findings_by_type": by_type,
        "applied_fixes": sum(1 for f in findings if f.applied),
        "changed_pixels_total": int(sum(f.changed_pixel_count for f in findings)),
        "patched_layers": [doc.layer_by_index(i).path for i in sorted(patches)],
        "verification_passed": verification["passed"],
        "elapsed_sec": round(time.time() - t0, 2),
    }
    res.qa = {
        "tool": {"name": "psd_cleanup", "version": __version__},
        "disclaimer": ("이 결과는 규칙 기반 자동 검수 보조 결과이며 졸업 검수 통과를 보장하지 않습니다. "
                       "'불확실' 항목은 반드시 사람이 확인하세요."),
        "input": str(src), "input_sha256_before": sha_before, "config": cfg.to_dict(),
        "document": doc.doc_info.to_dict(), "composite_source": composite_source,
        "layers": [l.to_dict() for l in doc.layers],
        "analyzed_layer_indices": analyzed_layers,
        "summary": res.summary, "write": write_info, "verification": verification,
        "findings": [f.to_dict() for f in findings],
        "outputs": {k: str(v) for k, v in paths.items()},
    }
    paths["qa_json"].write_text(json.dumps(res.qa, ensure_ascii=False, indent=1), encoding="utf-8")
    report.write_html(res.qa, paths, res.outputs)
    res.outputs.update({"clean_psd": str(paths["clean_psd"]), "qa_json": str(paths["qa_json"]),
                        "qa_html": str(paths["qa_html"])})
    res.status = "ok" if verification["passed"] else "failed"
    if not verification["passed"]:
        res.error = "저장 후 검증 실패: " + "; ".join(verification["failures"][:5])
        failed = paths["clean_psd"].with_name(paths["clean_psd"].stem + "_VERIFY_FAILED" + paths["clean_psd"].suffix)
        paths["clean_psd"].replace(failed)   # 검증 실패 결과는 CLEAN 이름으로 남기지 않음
        res.outputs["clean_psd"] = str(failed)
    if sha256_file(src) != sha_before:     # 절대 일어나면 안 됨
        res.status, res.error = "failed", "원본 해시가 변경됨!"
    prog(1.0, "완료")
    return res


def run_batch(inputs: list[str | Path], cfg: ProjectConfig, out_dir: str | Path | None = None, *,
              recursive: bool = False, overwrite_outputs: bool = False,
              progress: ProgressFn | None = None, cancel: threading.Event | None = None) -> list[RunResult]:
    files: list[Path] = []
    for p in map(Path, inputs):
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            files += sorted(x for x in it if x.suffix.lower() in (".psd", ".psb") and x.is_file()
                            and "_CLEAN" not in x.stem and "_cleanup_output" not in x.parts)
        else:
            files.append(p)
    results = []
    for i, f in enumerate(files):
        if cancel is not None and cancel.is_set():
            results.append(RunResult(str(f), status="cancelled"))
            continue
        sub = (lambda fr, m, i=i: progress((i + fr) / len(files), f"[{i + 1}/{len(files)}] {f.name}: {m}")) \
            if progress else None
        try:
            results.append(run_file(f, cfg, out_dir, overwrite_outputs=overwrite_outputs, progress=sub, cancel=cancel))
        except Cancelled:
            results.append(RunResult(str(f), status="cancelled", error="사용자 취소 (원본 변경 없음)"))
        except Exception as e:  # 실패 파일 격리: 다음 파일 계속
            results.append(RunResult(str(f), status="failed", error=f"{type(e).__name__}: {e}",
                                     summary={"traceback": traceback.format_exc(limit=5)}))
    return results
