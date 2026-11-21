"""어패럴(Apparel) 클린업 엔진 — 기준 PDF 1쪽(어패럴 기준), 2~6쪽 사례.

핵심 성격: 날선(하드 엣지) 색면 · 분판(도수) · 1픽셀 정확도.
금지: 안티앨리어싱 추가(새 중간색 생성), 고립 픽셀 일괄 삭제, 임의 색 변경.
→ 모든 수정은 '이미 주변에 있는 색'으로만 칠하거나(alpha=0 포함) 하는 방식으로 제한.
"""
from __future__ import annotations

import numpy as np

from .. import analysis as A
from ..config import parse_hex, to_hex
from ..findings import CERTAIN, INFO, INTENTIONAL, UNCERTAIN, Finding, PixelFix
from .base import BaseEngine


class ApparelEngine(BaseEngine):
    name = "apparel"
    default_fix_types = frozenset({"isolated_speck", "odd_pixel", "pinhole", "edge_spur", "edge_notch",
                                   "off_palette_edge"})

    SPECK_MAX_AREA = 4         # 이 이하 면적의 고립 성분을 후보로
    SPECK_AUTO_MAX_AREA = 2    # 자동 수정은 1~2px 만
    SPECK_AUTO_MIN_GAP = 3     # 다른 픽셀과 3px 이상 떨어져야 자동 수정
    FLAT_MAX_COLORS = 256      # 이보다 색이 많으면 '분판 전 연속톤 레이어'로 보고 픽셀 자동 수정 안 함

    @property
    def palette(self) -> np.ndarray | None:
        if not self.cfg.allowed_colors:
            return None
        return np.array([parse_hex(c) for c in self.cfg.allowed_colors], dtype=np.uint8)

    def analyze_layer(self, layer, rgba, approved_texture=False) -> list[Finding]:
        out: list[Finding] = []
        out += self.hygiene(layer, rgba)
        n_colors, tops, _ = A.color_stats(rgba, top=8)
        flat = n_colors <= self.FLAT_MAX_COLORS
        if not flat:
            out.append(self._f(layer, type="color_count", confidence=UNCERTAIN,
                               proposed_action=("연속톤(색 수 많음) 레이어 → 어패럴 1픽셀/경계 규칙의 자동 수정을 "
                                                "적용하지 않고 보고만 함 (분판·인덱스 정리 후 다시 실행 권장)"),
                               evidence={"unique_opaque_colors": n_colors, "flat_limit": self.FLAT_MAX_COLORS},
                               bounds=tuple(layer.bbox), pdf_ref="1쪽 Index 도수 / 6쪽 분판"))
        # 1쪽 원픽셀의 점 / 5쪽 의도적 반복
        out += self.specks(layer, rgba, max_area=self.SPECK_MAX_AREA, auto_max_area=self.SPECK_AUTO_MAX_AREA,
                           auto_min_gap=self.SPECK_AUTO_MIN_GAP, exact_repeat_rule=True)
        if not approved_texture:
            # 3~4쪽 1픽셀 잘못 칠함 / 1픽셀 구멍
            out += self.pixel_anomalies(layer, rgba, certain_allowed=flat)
            # 2쪽 회전으로 깨진 경계 (곧은 경계 위 1픽셀 돌출/패임만)
            for c in A.edge_bumps(rgba, self.cfg.repeat_min, layer.has_alpha_channel):
                ev = {"direction_to_body": c.direction, "old": list(c.old), "new": list(c.new),
                      "same_neighborhood_repeats": c.repeat_count, "straight_edge_len": list(c.plateau),
                      "convex_apex": c.apex}
                if c.intentional:
                    conf, act = INTENTIONAL, "보존 (같은 모양이 반복 → 의도적 반복)"
                elif c.apex:
                    conf, act = UNCERTAIN, "보고만 (곡선 정점의 1px — 정상적인 픽셀 계단일 수 있음)"
                elif not flat:
                    conf, act = UNCERTAIN, "보고만 (연속톤 레이어)"
                else:
                    conf, act = CERTAIN, ("투명 처리" if c.new[3] == 0 else f"바깥 색 {to_hex(c.new)} 로 채움")
                fix = PixelFix(np.array([c.y]), np.array([c.x]), np.array([c.new], dtype=np.uint8)) \
                    if conf == CERTAIN else None
                out.append(self._f(layer, type=c.kind, confidence=conf, proposed_action=act, evidence=ev,
                                   bounds=self._bounds(layer, c.x, c.y, c.x + 1, c.y + 1),
                                   pdf_ref="2쪽 모티프 회전으로 깨진 경계", fix=fix))
        # 1쪽 경계 여러 색 / 6쪽 분판
        out += self._palette_checks(layer, rgba)
        out += self._semi_alpha(layer, rgba)
        if self.cfg.one_color_per_layer:
            n, tops, _ = A.color_stats(rgba)
            if n > 1:
                out.append(self._f(layer, type="layer_multi_color", confidence=UNCERTAIN,
                                   proposed_action="보고만 (분판 레이어에 색이 2개 이상)",
                                   evidence={"unique_colors": n,
                                             "top_colors": [[to_hex(t[:3]), t[3]] for t in tops[:10]]},
                                   bounds=tuple(layer.bbox), pdf_ref="6쪽 컬러 분판 틀림"))
        return out

    def _palette_checks(self, layer, rgba) -> list[Finding]:
        pal = self.palette
        if pal is None:
            return []
        out = []
        for g in A.off_palette_groups(rgba, pal):
            x0, y0, x1, y1 = g.bbox
            ev = {"pixels": int(len(g.ys)), "colors": [to_hex(c) for c in g.colors]}
            if g.certain:
                new = np.concatenate([g.new_rgb, np.full((len(g.ys), 1), 255, np.uint8)], axis=1)
                out.append(self._f(layer, type="off_palette_edge", confidence=CERTAIN,
                                   proposed_action="경계 혼합색 → 이웃한 허용 색 중 가장 가까운 색으로",
                                   evidence=ev, bounds=self._bounds(layer, x0, y0, x1, y1),
                                   pdf_ref="1쪽 Index 도수는 맞지만 경계에 여러 색 / 6쪽",
                                   fix=PixelFix(g.ys, g.xs, new)))
            else:
                out.append(self._f(layer, type="off_palette", confidence=UNCERTAIN,
                                   proposed_action="보고만 (허용 색 사이 혼합색으로 설명되지 않음)",
                                   evidence=ev, bounds=self._bounds(layer, x0, y0, x1, y1),
                                   pdf_ref="6쪽 컬러 분판 틀림"))
        return out

    def _semi_alpha(self, layer, rgba) -> list[Finding]:
        if not layer.has_alpha_channel:
            return []
        a = rgba[..., 3]
        m = (a > 0) & (a < 255)
        n = int(m.sum())
        if n == 0:
            return []
        ys, xs = np.nonzero(m)
        return [self._f(layer, type="semi_alpha_edge", confidence=UNCERTAIN,
                        proposed_action="보고만 (반투명 경계는 분판 시 중간 톤이 될 수 있음. 자동 경화 미지원)",
                        evidence={"pixels": n},
                        bounds=self._bounds(layer, xs.min(), ys.min(), xs.max() + 1, ys.max() + 1),
                        pdf_ref="1쪽 경계에 여러 색")]

    def analyze_composite(self, rgb) -> list[Finding]:
        out = []
        n, tops, total = A.color_stats(rgb)
        major = [t for t in tops if t[3] >= max(1, total * 0.0005)]
        ev = {"unique_colors": n, "major_colors(>=0.05%)": len(major),
              "top_colors": [[to_hex(t[:3]), t[3]] for t in tops[:24]]}
        exp = self.cfg.expected_color_count
        if exp is not None:
            ev["expected_color_count(user)"] = exp
            conf = INFO if n == exp else UNCERTAIN
            act = "일치" if n == exp else "불일치 — 사용자 입력 도수와 합성 결과 색 수가 다름 (배경색 포함 여부 확인)"
        else:
            conf, act = INFO, "도수 미입력 → 색 수만 보고"
        out.append(self._f(None, type="color_count", confidence=conf, proposed_action=act, evidence=ev,
                           pdf_ref="1쪽 Index 도수 / 6쪽 분판"))
        if self.palette is None:
            rare = [t for t in tops[::-1] if t[3] <= max(3, total * 0.00002)] if n <= 4096 else []
            if rare and n > 1:
                _, allc, _ = A.color_stats(rgb, top=n)
                rare = [t for t in allc if t[3] <= max(3, total * 0.00002)]
                out.append(self._f(None, type="rare_color", confidence=UNCERTAIN,
                                   proposed_action="보고만 (허용 색 목록을 입력하면 경계 혼합색을 자동 판정)",
                                   evidence={"rare_color_count": len(rare),
                                             "samples": [[to_hex(t[:3]), t[3]] for t in rare[:20]]},
                                   pdf_ref="1쪽 경계에 여러 색"))
        rgba = np.dstack([rgb, np.full(rgb.shape[:2], 255, np.uint8)])
        for c in A.single_pixel_anomalies(rgba, self.cfg.repeat_min, has_alpha=False):
            if c.intentional:
                continue
            out.append(self._f(None, type=c.kind, confidence=UNCERTAIN,
                               proposed_action="합성 결과 기준 후보 — 수정은 레이어 단위에서만",
                               evidence={"old": list(c.old), "new": list(c.new)},
                               bounds=(c.x, c.y, c.x + 1, c.y + 1), pdf_ref="3~4쪽"))
        return out
