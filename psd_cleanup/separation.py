"""분판(컬러 세퍼레이션) 구조 감지와 '레이어 간 픽셀 이동' 수정.

어패럴 분판 PSD 는 보통 '레이어 하나 = 색 하나'이고 레이어끼리 겹치지 않으며 합치면 캔버스를 빈틈없이 채운다.
이런 파일에서 레이어를 하나씩 따로 고치면(예: 점 지우기) 그 자리에 구멍이 생기거나 다른 분판과 겹친다.
→ 분판 전체를 하나의 '색 지도(label map)'로 보고 검사하고, 1픽셀을 고칠 때는
   원래 분판 레이어에서 빼고(alpha 0) 새 색의 분판 레이어에 넣는다(alpha 255, 그 레이어의 색).
   구멍·겹침 수는 수정 전후 반드시 같아야 한다.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Separation:
    slots: list[int]                      # 분판 레이어 인덱스
    names: list[str]
    colors: list[tuple[int, int, int]]
    label: np.ndarray                     # HxW int16, -1 = 어느 분판에도 없음
    holes: int
    overlaps: int

    def virtual_rgba(self) -> np.ndarray:
        pal = np.array(self.colors + [(0, 0, 0)], np.uint8)
        lab = np.where(self.label < 0, len(self.colors), self.label)
        rgba = np.dstack([pal[lab], np.where(self.label < 0, 0, 255).astype(np.uint8)])
        return rgba

    def slot_of_color(self, rgb) -> int | None:
        try:
            return self.colors.index(tuple(int(v) for v in rgb[:3]))
        except ValueError:
            return None


def coverage(doc, layer_rgba: dict, indices: list[int], W: int, H: int) -> np.ndarray:
    cov = np.zeros((H, W), np.int16)
    for idx in indices:
        l = doc.layer_by_index(idx)
        a = layer_rgba[idx][..., 3] > 0
        x0, y0, x1, y1 = l.bbox
        cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
        if cx1 > cx0 and cy1 > cy0:
            cov[cy0:cy1, cx0:cx1] += a[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
    return cov


def detect_separation(doc, layer_rgba: dict, W: int, H: int) -> tuple[Separation | None, str]:
    """조건을 모두 만족할 때만 분판 모드. 아니면 (None, 이유)."""
    if doc.doc_info.color_mode != 3 or doc.doc_info.depth != 8:
        return None, "RGB 8bit 문서가 아님"
    slots, names, colors = [], [], []
    for l in doc.layers:
        if l.kind == "group":
            continue
        if not l.effective_visible:
            continue
        if l.kind != "pixel" or l.index not in layer_rgba:
            return None, f"보이는 비픽셀/읽기 불가 레이어: {l.path}"
        rgba = layer_rgba[l.index]
        a = rgba[..., 3]
        if not l.has_alpha_channel:
            return None, f"투명 채널 없는 레이어: {l.path}"
        if not np.isin(a, (0, 255)).all():
            return None, f"반투명 픽셀이 있는 레이어: {l.path}"
        if l.opacity != 255 or (l.fill_opacity not in (None, 255)) or l.blend_mode != "NORMAL" \
                or l.clipping or l.has_mask or l.has_vector_mask or l.has_effects:
            return None, f"불투명도/블렌드/마스크/효과가 있는 레이어: {l.path}"
        if not l.editable:
            return None, f"수정 불가 레이어: {l.path}"
        px = rgba[a == 255][:, :3]
        if len(px) == 0:
            continue
        first = px[0]
        if not (px == first).all():
            return None, f"색이 2개 이상인 레이어: {l.path}"
        c = tuple(int(v) for v in first)
        if c in colors:
            return None, f"같은 색 레이어가 2개 이상: {l.path}"
        slots.append(l.index)
        names.append(l.path)
        colors.append(c)
    if len(slots) < 2:
        return None, "분판 레이어가 2개 미만"
    cov = coverage(doc, layer_rgba, slots, W, H)
    overlaps = int((cov > 1).sum())
    if overlaps:
        return None, f"분판 레이어끼리 겹치는 픽셀 {overlaps}개"
    label = np.full((H, W), -1, np.int16)
    for k, idx in enumerate(slots):
        l = doc.layer_by_index(idx)
        a = layer_rgba[idx][..., 3] > 0
        x0, y0, x1, y1 = l.bbox
        cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
        if cx1 > cx0 and cy1 > cy0:
            sub = a[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
            label[cy0:cy1, cx0:cx1][sub] = k
    return Separation(slots, names, colors, label, int((cov == 0).sum()), 0), "ok"
