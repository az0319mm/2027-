"""스캔 종이 배경 제거 (인테리어, 사용자가 켰을 때만).

기준 PDF: '스캔한 모티브의 배경이 외곽에 남아 있을 때' / 8쪽 외곽 정리 / 12쪽 수채화 번짐 보호.

방법 (그림을 새로 그리지 않는 규칙 기반):
 1) 종이색 p 추정: 밝고 채도 낮은 불투명 픽셀의 중앙값 (또는 사용자가 지정한 paper_color)
 2) 물감 픽셀 = 종이색과 충분히 다른 픽셀. 작은 틈을 닫기(closing)로 메워 외곽선이 끊긴 곳으로 새지 않게 함
 3) 모티브 = 물감 + 물감에 완전히 둘러싸인 안쪽(꽃잎 안 흰 부분 등)
    바깥 종이 = 모티브가 아닌 불투명 픽셀 → 투명
 4) 모티브 외곽 띠(몇 px)는 '물감 농도'로 반투명 알파를 만들고, 색은 종이색 기준으로 되돌려(un-multiply)
    파란 배경 등 다른 바탕 위에서도 흰 종이 테두리(헤일로)가 남지 않게 함 → 수채화 번짐은 부드럽게 유지
 5) 종이 위의 작은 먼지·얼룩 조각(모티브와 떨어진 작은 물감 성분)은 제거
결과는 '사용자 요청 변환'이며, 바뀐 픽셀과 남긴 안쪽 종이 영역을 보고서에 모두 기록한다.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage


@dataclass
class PaperResult:
    rgba: np.ndarray
    paper_rgb: tuple[int, int, int]
    removed_px: int
    softened_px: int
    dust_components: int
    thin_line_components: int
    thin_lines_kept: list
    enclosed_paper_regions: list[tuple[int, int, int, int, int]]   # (x0,y0,x1,y1,area) 남긴 안쪽 종이
    paper_fraction: float


def estimate_paper(rgba: np.ndarray) -> tuple[int, int, int] | None:
    a = rgba[..., 3]
    rgb = rgba[..., :3].astype(np.int32)
    lum = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    chroma = rgb.max(axis=2) - rgb.min(axis=2)
    m = (a == 255) & (lum >= 200) & (chroma <= 30)
    if m.sum() < max(200, 0.02 * (a > 0).sum()):
        return None
    px = rgb[m]
    return tuple(int(v) for v in np.median(px, axis=0))


def edt_to_zero(mask: np.ndarray, border_is_zero: bool) -> np.ndarray:
    """mask 의 True 픽셀에서 가장 가까운 False 픽셀까지의 유클리드 거리.
    border_is_zero=True 면 레이어 바깥을 False(투명)로 본다. False 픽셀이 하나도 없으면 무한대.
    (scipy 는 False 가 없을 때 임의 기준점을 쓰므로 직접 처리)"""
    P = np.pad(mask, 1, constant_values=not border_is_zero)
    if P.all():
        return np.full(mask.shape, np.inf)
    return ndimage.distance_transform_edt(P)[1:-1, 1:-1]


def _disk(r: int) -> np.ndarray:
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    return (yy * yy + xx * xx) <= r * r


def remove_paper(rgba: np.ndarray, paper_rgb: tuple[int, int, int] | None = None, *,
                 ink_thr: float = 30.0, soft_lo: float = 10.0, close_r: int = 3, band: int = 3,
                 dust_max_area: int = 40, keep_enclosed_max_frac: float = 0.02,
                 edge_band: int = 4, ink_full: float = 90.0) -> PaperResult | None:
    a0 = rgba[..., 3]
    if paper_rgb is None:
        paper_rgb = estimate_paper(rgba)
        if paper_rgb is None:
            return None
    p = np.array(paper_rgb, np.float32)
    rgb = rgba[..., :3].astype(np.float32)
    present = a0 > 0
    dist = np.abs(rgb - p).max(axis=2)                       # 종이색과의 차이(채널 최대)
    # 종이 가장자리(투명과 맞닿은 곳에서 edge_band px 이내)의 선·그림자는 '벽'이 되지 않도록 물감 판정에서 제외
    d_edge = edt_to_zero(present, border_is_zero=False)       # 레이어 경계는 종이 가장자리로 보지 않음 (캔버스에서 잘린 모티브 보호)
    ink = present & (dist > ink_thr) & (d_edge > edge_band)
    # 옅은 물감(흰 꽃잎의 회색 음영, 옅은 수채 번짐): 종이 질감 노이즈보다 확실히 진하고
    # 진한 물감과 이어져 있으면 모티브로 인정 (종이 질감은 이 기준 아래라 퍼지지 않음)
    paperish = present & (dist <= ink_thr)
    noise = float(np.std(dist[paperish])) if paperish.any() else 3.0
    light_thr = max(12.0, 4.0 * noise)
    light = present & (dist > light_thr) & (d_edge > edge_band)
    ink = ndimage.binary_propagation(ink, mask=light | ink)

    # 2) 틈 닫기 → 3) 둘러싸인 안쪽 채우기
    closed = ndimage.binary_closing(ink, structure=_disk(close_r), border_value=0) | ink
    filled = ndimage.binary_fill_holes(closed)
    enclosed = filled & ~closed & present
    # 아주 큰 '둘러싸인 종이'는 줄기 사이에 갇힌 종이일 가능성이 큼 → 지우고 보고
    lab, n = ndimage.label(enclosed)
    total = max(1, int(present.sum()))
    kept_regions = []
    big = np.zeros_like(enclosed)
    for i, sl in enumerate(ndimage.find_objects(lab)):
        reg = lab[sl] == i + 1
        area = int(reg.sum())
        # 둥근 덩어리(꽃잎 안쪽 흰 부분 등)만 남기고, 좁고 길쭉한 띠(줄기 사이에 갇힌 종이)는 지움
        r_in = float(ndimage.distance_transform_edt(np.pad(reg, 1))[1:-1, 1:-1].max())
        elongated = r_in < 0.2 * np.sqrt(area)
        if area > keep_enclosed_max_frac * total or elongated:
            big[sl] |= reg
        else:
            kept_regions.append((sl[1].start, sl[0].start, sl[1].stop, sl[0].stop, area))
    keep = (filled & ~big) & present

    # 5) 먼지: 모티브와 떨어진 작은 조각
    lab2, n2 = ndimage.label(keep, structure=np.ones((3, 3), bool))
    sizes = np.bincount(lab2.ravel())
    dust_ids = np.nonzero(sizes <= dust_max_area)[0]
    dust_ids = dust_ids[dust_ids > 0]
    if len(dust_ids):
        keep &= ~np.isin(lab2, dust_ids)

    # 5-b) 몸체와 떨어진 가느다란 선 조각(폭 3px 미만: 잘림선·가이드선·종이 테두리 잔여) 제거
    #   - 종이 가장자리 가까이(투명 경계에서 4*edge_band 이내가 절반 이상)인 것만 자동 제거
    #   - 안쪽의 떨어진 가는 선은 줄기·꽃술일 수 있어 남기고 보고(thin_line_regions)
    lab3, n3 = ndimage.label(keep, structure=np.ones((3, 3), bool))
    thin_lines = 0
    thin_kept: list[tuple[int, int, int, int, int]] = []
    if n3 > 1:
        sizes3 = np.bincount(lab3.ravel())
        main = sizes3[1:].max()
        dt = edt_to_zero(keep, border_is_zero=False)
        rmax = ndimage.maximum(dt, lab3, index=np.arange(1, n3 + 1))
        near_edge = ndimage.mean((d_edge <= 4 * edge_band).astype(float), lab3, index=np.arange(1, n3 + 1))
        slices = ndimage.find_objects(lab3)
        for L in range(1, n3 + 1):
            r_ = float(rmax[L - 1])
            thin = sizes3[L] < main and r_ < 2.5 and sizes3[L] >= 12 * max(r_, 0.5) ** 2 \
                and sizes3[L] < 0.02 * total
            if not thin:
                continue
            if near_edge[L - 1] >= 0.5:
                keep &= lab3 != L
                thin_lines += 1
            else:
                sl = slices[L - 1]
                thin_kept.append((sl[1].start, sl[0].start, sl[1].stop, sl[0].stop, int(sizes3[L])))

    # 4) 외곽 띠: 모티브 경계 안팎 band px 은 물감 농도로 부드러운 알파 + 종이색 un-multiply
    outside = present & ~keep
    d_out = edt_to_zero(~outside, border_is_zero=False)       # 바깥 종이까지 거리
    d_in = edt_to_zero(~keep, border_is_zero=False)           # 모티브까지 거리
    edge_in = keep & (d_out <= band)                         # 모티브 안쪽 테두리 (옅은 물감 포함)
    edge_out = outside & (d_in <= band)                      # 바깥 바로 옆 번짐
    soft = edge_in | edge_out
    strength = np.clip((dist - soft_lo) / (ink_full - soft_lo), 0.0, 1.0)
    # 안쪽으로 갈수록 점점 불투명 (경계에서 band px 들어가면 완전 불투명) → 옅은 물감 외곽만 반투명
    ramp = np.where(keep & ink, np.clip(d_out / (band + 1.0), 0.0, 1.0), 0.0)   # 종이색 픽셀은 제외(흰 테두리 방지)
    strength = np.maximum(strength, ramp)

    # 종이 가장자리 테두리선(바깥 종이 쪽, 투명 경계에서 edge_band px 이내)은 부드러운 띠에서도 제외
    soft &= ~(outside & (d_edge <= edge_band))
    alpha = np.where(keep, 1.0, 0.0).astype(np.float32)
    alpha[soft] = strength[soft]
    alpha = alpha * (a0.astype(np.float32) / 255.0)
    a_new = np.clip(np.round(alpha * 255), 0, 255).astype(np.uint8)

    out = rgba.copy()
    # 색 보정(un-multiply): 반투명 테두리가 종이색 위에서 원래와 같아 보이도록 c' = p - (p - c)/α
    s = soft & (a_new > 0)
    af = np.clip(strength[s], 1e-3, 1.0)[:, None]
    out[s, :3] = np.clip(np.round(p - (p - rgb[s]) / af), 0, 255).astype(np.uint8)
    out[..., 3] = np.where(present, a_new, a0)

    removed = present & (a_new == 0)
    return PaperResult(out, tuple(int(v) for v in p), int(removed.sum()), int((s & ~removed).sum()),
                       int(len(dust_ids)), thin_lines, thin_kept, kept_regions, float((present & ~ink).sum() / total))
