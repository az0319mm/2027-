"""저장 결과 재검증 (명세 5장 1·2·3·4번 자동 검사).

1. 원본 해시 불변
2. 결과 PSD 재열기 (psd-tools + 독립 파서 Pillow) — Photoshop 재열기는 UXP 플러그인에서 별도 수행
3. 문서/레이어 구조 불변 (이름·순서·가시성·불투명도·블렌드·경계·마스크·태그 블록 바이트·이미지 리소스 바이트)
4. 비수정 레이어 채널 데이터 바이트 동일, 수정 레이어는 '계획된 픽셀만' 변경
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np

from .psd_io import PSDDocument, sha256_file

STRUCT_FIELDS = ("name", "path", "kind", "visible", "opacity", "fill_opacity", "blend_mode", "bbox",
                 "clipping", "has_mask", "has_vector_mask", "has_effects", "locks", "channel_ids", "layer_id")
DOC_FIELDS = ("version", "width", "height", "channels", "depth", "color_mode", "has_icc",
              "icc_bytes_sha256", "resolution", "layer_count")


def _bytes_of(element) -> bytes:
    b = io.BytesIO()
    element.write(b)
    return b.getvalue()


def verify_output(src: Path, sha_before: str, out: Path, doc: PSDDocument,
                  patches: dict[int, dict[int, np.ndarray]], expected_new: dict[int, np.ndarray]) -> dict:
    failures: list[str] = []
    warnings: list[str] = []
    checks: dict[str, bool] = {}

    checks["original_hash_unchanged"] = sha256_file(src) == sha_before
    if not checks["original_hash_unchanged"]:
        failures.append("원본 파일 해시가 바뀜")

    try:
        new = PSDDocument(out)
        checks["reopen_psd_tools"] = True
    except Exception as e:
        return {"passed": False, "checks": {**checks, "reopen_psd_tools": False},
                "failures": failures + [f"결과 PSD 재열기 실패: {e}"], "warnings": warnings}

    try:
        from PIL import Image
        with Image.open(out) as im:
            im.load()
            checks["reopen_pillow"] = im.size == (doc.doc_info.width, doc.doc_info.height)
    except Exception as e:  # Pillow 는 일부 PSD 변형을 지원하지 않음 → 경고만
        checks["reopen_pillow"] = False
        warnings.append(f"Pillow 재열기 실패(보조 검사): {e}")

    for k in DOC_FIELDS:
        if getattr(doc.doc_info, k) != getattr(new.doc_info, k):
            failures.append(f"문서 속성 변경: {k}")
    checks["document_properties_equal"] = not any(f.startswith("문서 속성") for f in failures)

    r0 = _bytes_of(doc.psd._record.image_resources)
    r1 = _bytes_of(new.psd._record.image_resources)
    checks["image_resources_bytes_equal"] = r0 == r1
    if r0 != r1:
        failures.append("이미지 리소스(ICC·해상도 등) 바이트 변경")

    if len(doc.layers) != len(new.layers):
        failures.append("레이어 수 변경")
    else:
        for a, b in zip(doc.layers, new.layers):
            for k in STRUCT_FIELDS:
                if getattr(a, k) != getattr(b, k):
                    failures.append(f"레이어 속성 변경: [{a.index}] {a.path} .{k}")
    checks["layer_structure_equal"] = not any(f.startswith("레이어") for f in failures)

    recs0 = doc._records
    recs1 = new._records
    tb_ok = True
    for i, (ra, rb) in enumerate(zip(recs0, recs1)):
        if _bytes_of(ra.tagged_blocks) != _bytes_of(rb.tagged_blocks):
            tb_ok = False
            failures.append(f"레이어 [{i}] 태그 블록(효과·잠금·이름 등) 바이트 변경")
        if (ra.mask_data is None) != (rb.mask_data is None) or (
                ra.mask_data is not None and _bytes_of(ra.mask_data) != _bytes_of(rb.mask_data)):
            tb_ok = False
            failures.append(f"레이어 [{i}] 마스크 데이터 변경")
    checks["tagged_blocks_and_masks_equal"] = tb_ok

    px_ok = True
    for i in range(min(len(recs0), len(recs1))):
        if i not in patches:
            if doc.raw_channel_bytes(i) != new.raw_channel_bytes(i):
                px_ok = False
                failures.append(f"수정 대상이 아닌 레이어 [{i}] 픽셀 데이터 변경")
        else:
            got = new.read_rgba(i)
            exp = expected_new[i]
            if got is None or got.shape != exp.shape:
                px_ok = False
                failures.append(f"레이어 [{i}] 재열기 픽셀 읽기 실패")
                continue
            vis = lambda x: np.where(x[..., 3:4] == 0, 0, x)  # noqa: E731  투명 픽셀 RGB 무시
            if not np.array_equal(vis(got), vis(exp)):
                px_ok = False
                failures.append(f"레이어 [{i}] 저장된 픽셀이 계획된 수정과 다름")
            # 마스크 채널 등 수정하지 않은 채널은 바이트 동일해야 함
            ids = [int(c.id) for c in recs0[i].channel_info]
            for j, cid in enumerate(ids):
                if cid not in patches[i] and doc.raw_channel_bytes(i)[j] != new.raw_channel_bytes(i)[j]:
                    px_ok = False
                    failures.append(f"레이어 [{i}] 채널 {cid} 이(가) 의도치 않게 변경")
    checks["pixel_invariance"] = px_ok

    m0, m1 = doc.merged_rgb(), new.merged_rgb()
    if m0 is not None and m1 is not None:
        changed = np.any(m0 != m1, axis=2)
        checks["merged_preview_changed_pixels"] = int(changed.sum())  # type: ignore[assignment]
        if changed.any() and not patches:
            failures.append("수정이 없는데 병합 이미지가 바뀜")
    return {"passed": not failures, "checks": checks, "failures": failures, "warnings": warnings,
            "not_verified_here": ["Photoshop 에서 실제로 다시 열리는지 (UXP 플러그인의 재열기 검증으로 확인)"]}
