"""인테리어(Interior) 클린업 엔진 — 기준 PDF 1쪽(인테리어 기준), 7~12쪽 사례.

핵심 성격: 손그림/스캔 질감, 반투명 번짐, 배경 위 합성.
금지: 전체 블러, 경계 선명화 일괄 적용, 반투명 번짐을 오염으로 제거.
→ 자동 수정은 '몸체와 떨어진 잔여'(덜 지워진 점/반투명 찌꺼기)만. 외곽 자체는 건드리지 않음.
"""
from __future__ import annotations

import numpy as np

from .. import analysis as A
from ..config import parse_hex, to_hex
from ..findings import CERTAIN, INFO, INTENTIONAL, UNCERTAIN, Finding, PixelFix
from .base import BaseEngine

# 표현 방식별 보호 프로필 (12쪽: 수채화·번짐 의도 표현 보호)
STYLE_PROFILES = {
    "digital": dict(speck_auto_max_area=2, speck_auto_min_gap=3, faint_report_alpha=64,
                    faint_auto_alpha=40, faint_auto_max_area=5000, faint_auto_min_gap=2,
                    pixel_fix=True, hole_max_area=64),
    "watercolor": dict(speck_auto_max_area=0, speck_auto_min_gap=99, faint_report_alpha=48,
                       faint_auto_alpha=16, faint_auto_max_area=200, faint_auto_min_gap=6,
                       pixel_fix=False, hole_max_area=0),
    "pencil": dict(speck_auto_max_area=0, speck_auto_min_gap=99, faint_report_alpha=48,
                   faint_auto_alpha=16, faint_auto_max_area=200, faint_auto_min_gap=6,
                   pixel_fix=False, hole_max_area=0),
    "crayon": dict(speck_auto_max_area=0, speck_auto_min_gap=99, faint_report_alpha=48,
                   faint_auto_alpha=16, faint_auto_max_area=200, faint_auto_min_gap=6,
                   pixel_fix=False, hole_max_area=0),
}


class InteriorEngine(BaseEngine):
    name = "interior"
    default_fix_types = frozenset({"isolated_speck", "faint_residue", "odd_pixel", "pinhole"})

    @property
    def profile(self) -> dict:
        return STYLE_PROFILES[self.cfg.interior_style]

    def analyze_layer(self, layer, rgba, approved_texture=False) -> list[Finding]:
        p = self.profile
        out: list[Finding] = []
        out += self.hygiene(layer, rgba)
        # 1쪽 지무늬·배경 포함 점 / 9쪽 덜 지워진 영역 (불투명한 작은 점)
        out += self.specks(layer, rgba, max_area=6, auto_max_area=p["speck_auto_max_area"],
                           auto_min_gap=p["speck_auto_min_gap"], exclude_alpha_le=p["faint_report_alpha"],
                           pdf_ref="1쪽 지무늬·배경 포함 점 / 9쪽 덜 지워진 영역")
        out += self._faint_residue(layer, rgba)              # 1쪽 지우개 흔적 / 7·9쪽
        out += self._paper(layer, rgba)                      # 1쪽 스캔 배경 외곽 잔여
        if not approved_texture:                             # 11쪽 승인 질감은 내부 검사 제외
            if p["pixel_fix"]:
                # 인테리어: 눈에 띄는 1픽셀(채널 차 >= 16)만 대상. 미세 노이즈는 질감으로 보고 무시
                out += self.pixel_anomalies(layer, rgba, certain_allowed=True, min_diff=16)
            if p["hole_max_area"] > 0 and layer.has_alpha_channel:
                holes = A.enclosed_holes(rgba, 2, p["hole_max_area"])
                for ys, xs in holes[:200]:
                    out.append(self._f(layer, type="small_hole", confidence=UNCERTAIN,
                                       proposed_action="보고만 (의도된 하이라이트·공백일 수 있음)",
                                       evidence={"area": int(len(ys))},
                                       bounds=self._bounds(layer, xs.min(), ys.min(), xs.max() + 1, ys.max() + 1),
                                       pdf_ref="1쪽 밝은 컬러가 여백처럼 표현"))
        return out

    def _faint_residue(self, layer, rgba) -> list[Finding]:
        if not layer.has_alpha_channel:
            return []
        p = self.profile
        out = []
        for c in A.small_components(rgba, max_area=20000, alpha_max=p["faint_report_alpha"]):
            ev = {"area": c.area, "max_alpha": c.max_alpha, "gap_px": c.gap, "color": to_hex(c.mean_rgb),
                  "similar_components": c.similar_count, "style": self.cfg.interior_style}
            if c.gap < 1:
                continue
            if c.similar_count >= self.cfg.repeat_min and c.area <= 16:
                conf, act = INTENTIONAL, "보존 (반복되는 같은 형태 → 지무늬/질감 가능성)"
            elif (c.max_alpha <= p["faint_auto_alpha"] and c.area <= p["faint_auto_max_area"]
                  and c.gap >= p["faint_auto_min_gap"]):
                conf, act = CERTAIN, "투명 처리 (몸체와 떨어진 옅은 잔여)"
            else:
                conf, act = UNCERTAIN, "보존, 사람이 확인 필요 (번짐/질감 가능성)"
            fix = None
            if conf == CERTAIN:
                new = rgba[c.ys, c.xs].copy()
                new[:, 3] = 0
                fix = PixelFix(c.ys, c.xs, new)
            x0, y0, x1, y1 = c.bbox
            out.append(self._f(layer, type="faint_residue", confidence=conf, proposed_action=act, evidence=ev,
                               bounds=self._bounds(layer, x0, y0, x1, y1),
                               pdf_ref="1쪽 다른 배경색 시 지우개 흔적 / 7·9쪽", fix=fix))
        return out

    def _paper(self, layer, rgba) -> list[Finding]:
        out = []
        paper = parse_hex(self.cfg.paper_color) if self.cfg.paper_color else None
        if layer.has_alpha_channel:
            for ys, xs in A.paper_fringe(rgba, paper)[:200]:
                out.append(self._f(layer, type="paper_fringe", confidence=UNCERTAIN,
                                   proposed_action="보고만 (외곽 형태 변경이 필요하므로 자동 수정 안 함)",
                                   evidence={"pixels": int(len(ys))},
                                   bounds=self._bounds(layer, xs.min(), ys.min(), xs.max() + 1, ys.max() + 1),
                                   pdf_ref="1쪽 스캔 모티프 배경이 외곽에 남음 / 7쪽"))
        # 배경이 아닌 레이어가 꽉 찬 사각형이고 테두리가 종이색이면 → 스캔 배경 미제거 의심
        if layer.index > 0 and (rgba[..., 3] == 255).all() and min(rgba.shape[:2]) > 8:
            border = np.concatenate([rgba[0], rgba[-1], rgba[:, 0], rgba[:, -1]])[:, :3].astype(int)
            if paper is not None:
                like = np.abs(border - np.array(paper)).max(axis=1) <= 24
            else:
                lum = border @ np.array([0.299, 0.587, 0.114])
                like = (lum >= 215) & ((border.max(axis=1) - border.min(axis=1)) <= 25)
            if like.mean() >= 0.8:
                out.append(self._f(layer, type="scan_background", confidence=UNCERTAIN,
                                   proposed_action="보고만 (스캔 배경 제거는 자동으로 하지 않음)",
                                   evidence={"border_paper_ratio": round(float(like.mean()), 3)},
                                   bounds=tuple(layer.bbox), pdf_ref="1쪽 스캔 모티프 배경"))
        return out

    def analyze_composite(self, rgb) -> list[Finding]:
        n, tops, _ = A.color_stats(rgb, top=8)
        return [self._f(None, type="document", confidence=INFO, proposed_action="참고",
                        evidence={"unique_colors": n, "style": self.cfg.interior_style})]
