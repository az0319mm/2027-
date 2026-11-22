"""리핏(상하/좌우/상하좌우) 연결부 검사와 타일 미리보기.

검사는 레이어 단위 + 합성 결과 단위 모두 수행한다. 자동 수정은 하지 않는다(명세 8·10쪽: 복잡한 이동/재생성 금지).
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from . import analysis as A
from .findings import UNCERTAIN, Finding


def _directions(mode: str) -> list[str]:
    return {"none": [], "horizontal": ["horizontal"], "vertical": ["vertical"],
            "both": ["horizontal", "vertical"]}[mode]


def seam_findings(rgba: np.ndarray, left: int, top: int, W: int, H: int, mode: str, engine: str,
                  layer=None) -> list[Finding]:
    out = []
    if W < 4 or H < 4:
        return out
    for d in _directions(mode):
        if d == "horizontal":   # 좌우 리핏: 오른쪽 끝 열과 왼쪽 시작 열이 이어져야 함
            lines = A.canvas_lines(rgba, left, top, W, H, "col", [W - 2, W - 1, 0, 1])
        else:                   # 상하 리핏: 아래 끝 행과 위 시작 행
            lines = A.canvas_lines(rgba, left, top, W, H, "row", [H - 2, H - 1, 0, 1])
        segs, stats = A.seam_discontinuities(lines)
        for s0, s1, mx in segs:
            if _pattern_seen_elsewhere(rgba, left, top, W, H, d, s0, s1):
                continue   # 이음매를 가로지르는 무늬가 디자인 안 다른 곳에도 그대로 있음 → 정상 연결(예: 깅엄 경계가 캔버스 끝과 일치)
            if d == "horizontal":
                bounds = (W - 2, s0, W, s1)
                where = f"오른쪽 끝 ↔ 왼쪽 시작, y={s0}~{s1 - 1}"
            else:
                bounds = (s0, H - 2, s1, H)
                where = f"아래 끝 ↔ 위 시작, x={s0}~{s1 - 1}"
            f = Finding(type="repeat_discontinuity", engine=engine, scope="layer" if layer else "composite",
                        confidence=UNCERTAIN, proposed_action="보고만 (리핏 자동 수정은 지원하지 않음)",
                        evidence={"direction": d, "where": where, "length_px": s1 - s0,
                                  "max_seam_diff": round(mx, 1), **stats},
                        bounds=bounds, pdf_ref="8쪽 모티프 리핏 / 10쪽 지무늬 리핏")
            if layer is not None:
                f.layer_index, f.layer_name, f.layer_id = layer.index, layer.path, layer.layer_id
            out.append(f)
    return out


def tile_preview(rgb: np.ndarray, mode: str, seam_findings_list: list[Finding], max_side: int = 3000):
    """2x2(+3x3) 연결 미리보기. 좌우=3x1, 상하=1x3, 상하좌우=3x3. 불연속 위치는 빨간 테두리."""
    H, W = rgb.shape[:2]
    nx, ny = {"horizontal": (3, 1), "vertical": (1, 3), "both": (3, 3), "none": (1, 1)}[mode]
    tiled = np.tile(rgb, (ny, nx, 1))
    im = Image.fromarray(tiled)
    dr = ImageDraw.Draw(im)
    for f in seam_findings_list:
        x0, y0, x1, y1 = f.bounds
        for ty in range(ny):
            for tx in range(nx):
                if f.evidence["direction"] == "horizontal" and tx < nx - 1:
                    bx, by = (tx + 1) * W, ty * H + y0
                    dr.rectangle([bx - 6, by - 2, bx + 6, ty * H + y1 + 2], outline=(255, 0, 0), width=2)
                if f.evidence["direction"] == "vertical" and ty < ny - 1:
                    bx, by = tx * W + x0, (ty + 1) * H
                    dr.rectangle([bx - 2, by - 6, tx * W + x1 + 2, by + 6], outline=(255, 0, 0), width=2)
    scale = min(1.0, max_side / max(im.size))
    if scale < 1.0:
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.NEAREST)
    return im


def _canvas_band(rgba, left, top, W, H, axis, a0, a1):
    """캔버스 좌표의 행 a0..a1 (axis='rows') 또는 열 a0..a1 (axis='cols') 를 레이어에서 잘라 옴. 범위 밖은 투명."""
    h, w = rgba.shape[:2]
    if axis == "rows":
        out = np.zeros((a1 - a0, W, 4), np.uint8)
        ly0, ly1 = max(a0, top), min(a1, top + h)
        x0, x1 = max(0, left), min(W, left + w)
        if ly1 > ly0 and x1 > x0:
            out[ly0 - a0:ly1 - a0, x0:x1] = rgba[ly0 - top:ly1 - top, x0 - left:x1 - left]
        return out
    out = np.zeros((H, a1 - a0, 4), np.uint8)
    lx0, lx1 = max(a0, left), min(a1, left + w)
    y0, y1 = max(0, top), min(H, top + h)
    if lx1 > lx0 and y1 > y0:
        out[y0:y1, lx0 - a0:lx1 - a0] = rgba[y0 - top:y1 - top, lx0 - left:lx1 - left]
    return out.transpose(1, 0, 2)


def _pattern_seen_elsewhere(rgba, left, top, W, H, direction, s0, s1, k: int = 8) -> bool:
    """이음매를 가로지르는 2k 픽셀 조각이 같은 줄의 다른 위치에도 정확히 있는 줄이 80% 이상인지
    (깅엄·스트라이프처럼 무늬 경계가 캔버스 끝과 일치하는 정상 리핏을 걸러냄)."""
    if direction == "horizontal":
        band = _canvas_band(rgba, left, top, W, H, "rows", s0, s1)      # (len, W, 4)
    else:
        band = _canvas_band(rgba, left, top, W, H, "cols", s0, s1)      # (len, H, 4)
    K = A.pixel_keys(band)
    L = K.shape[1]
    if L < 4 * k:
        return False
    block = np.concatenate([K[:, L - k:], K[:, :k]], axis=1)             # (len, 2k)
    win = np.lib.stride_tricks.sliding_window_view(K, 2 * k, axis=1)    # (len, L-2k+1, 2k)
    row_match = np.all(win == block[:, None, :], axis=2).any(axis=1)     # 줄마다: 같은 줄 다른 곳에 같은 조각?
    return bool(row_match.mean() >= 0.8)

