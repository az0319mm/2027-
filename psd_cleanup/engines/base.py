"""엔진 공통 뼈대. 어패럴/인테리어는 각자 서브클래스에서 탐지 구성과 판정 규칙을 따로 가진다."""
from __future__ import annotations

import numpy as np

from .. import analysis as A
from ..config import ProjectConfig, to_hex
from ..findings import CERTAIN, INFO, INTENTIONAL, UNCERTAIN, Finding, PixelFix
from ..psd_io import LayerInfo


class BaseEngine:
    name = "base"
    TEXTURE_SPECK_COUNT = 20   # 레이어당 고립 점 후보가 이 이상이면 질감으로 보고 자동 삭제 보류
    #: 이 엔진이 자동 수정을 허용하는 유형 (그 외 유형은 확실해도 보고만)
    default_fix_types: frozenset[str] = frozenset()

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg

    @property
    def fix_types(self) -> set[str]:
        if self.cfg.fix_types is not None:
            return set(self.cfg.fix_types) & set(self.default_fix_types)
        return set(self.default_fix_types)

    # ---- 서브클래스 구현
    def analyze_layer(self, layer: LayerInfo, rgba: np.ndarray, approved_texture: bool) -> list[Finding]:
        raise NotImplementedError

    def analyze_composite(self, rgb: np.ndarray) -> list[Finding]:
        raise NotImplementedError

    # ---- 공통 헬퍼
    def _f(self, layer: LayerInfo | None, **kw) -> Finding:
        f = Finding(engine=self.name, scope="layer" if layer else "composite", **kw)
        if layer is not None:
            f.layer_index, f.layer_name, f.layer_id = layer.index, layer.path, layer.layer_id
        return f

    @staticmethod
    def _bounds(layer: LayerInfo | None, x0, y0, x1, y1):
        ox, oy = (layer.bbox[0], layer.bbox[1]) if layer else (0, 0)
        return (int(x0 + ox), int(y0 + oy), int(x1 + ox), int(y1 + oy))

    def specks(self, layer, rgba, *, max_area, auto_max_area, auto_min_gap, exclude_alpha_le=None,
               pdf_ref="1쪽 원픽셀의 점 / 5쪽 의도적 반복", exact_repeat_rule: bool = False) -> list[Finding]:
        """exact_repeat_rule=True (어패럴): 정확히 같은 점의 등간격 배열·질감만 의도적 반복으로 본다.
        False (인테리어): 비슷한 점이 repeat_min 개 이상이면 의도적(지무늬)으로 보존 — 더 보수적."""
        if not layer.has_alpha_channel:
            return []
        out = []
        comps = A.small_components(rgba, max_area=max_area)
        # 레이어 전체에 고립 점이 많으면(질감·흩뿌림 표현) 개별 점을 '실수'로 단정할 수 없음
        def _repeated(c):
            return c.exact_repeat_intentional if exact_repeat_rule else c.similar_count >= self.cfg.repeat_min
        # '설명되지 않는' 자동 삭제 후보만 센다 (의도적 반복으로 판정된 도트·점선은 제외)
        n_loose = sum(1 for c in comps if c.gap >= auto_min_gap and c.area <= max(auto_max_area, 1)
                      and not _repeated(c) and (exclude_alpha_le is None or c.max_alpha > exclude_alpha_le))
        n_texture = sum(1 for c in comps if c.repeat_reason == "texture") if exact_repeat_rule else 0
        texture_like = n_loose >= self.TEXTURE_SPECK_COUNT or n_texture > 0
        for c in comps:
            if exclude_alpha_le is not None and c.max_alpha <= exclude_alpha_le:
                continue  # 반투명 잔여 탐지기가 담당
            ev = {"area": c.area, "gap_px": c.gap, "max_alpha": c.max_alpha, "color": to_hex(c.mean_rgb),
                  "similar_components": c.similar_count, "layer_loose_specks": n_loose}
            if c.gap < 1:
                continue
            if _repeated(c):
                conf, act = INTENTIONAL, "보존 (같은 형태의 점이 반복 사용됨 → 의도적 반복으로 판단)"
            elif texture_like:
                conf, act = UNCERTAIN, f"보존 (설명 안 되는 고립 점 {n_loose}개 / 같은 점 대량 반복 {n_texture}개 → 질감·흩뿌림일 수 있어 일괄 삭제 금지)"
            elif c.area <= auto_max_area and c.gap >= auto_min_gap:
                conf, act = CERTAIN, "투명 처리(alpha=0)"
            else:
                conf, act = UNCERTAIN, "보존, 사람이 확인 필요"
            fix = None
            if conf == CERTAIN:
                new = rgba[c.ys, c.xs].copy()
                new[:, 3] = 0
                fix = PixelFix(c.ys, c.xs, new)
            x0, y0, x1, y1 = c.bbox
            out.append(self._f(layer, type="isolated_speck", confidence=conf, proposed_action=act, evidence=ev,
                               bounds=self._bounds(layer, x0, y0, x1, y1), pdf_ref=pdf_ref, fix=fix))
        return out

    def pixel_anomalies(self, layer, rgba, *, certain_allowed: bool, min_diff: int = 1) -> list[Finding]:
        """min_diff: 주변색과의 최대 채널 차이가 이보다 작으면(눈에 안 보이는 노이즈) 보고하지 않음."""
        out = []
        for c in A.single_pixel_anomalies(rgba, self.cfg.repeat_min, layer.has_alpha_channel):
            if c.kind == "odd_pixel" and max(abs(a - b) for a, b in zip(c.old, c.new)) < min_diff:
                continue
            ev = {"old": list(c.old), "new": list(c.new), "same_neighborhood_repeats": c.repeat_count}
            if c.intentional:
                conf, act = INTENTIONAL, "보존 (같은 1픽셀 패턴 반복 → 5쪽 통과 사례와 같은 의도적 반복)"
            elif certain_allowed:
                conf, act = CERTAIN, f"주변 8픽셀과 같은 색 {to_hex(c.new)} 으로 채움"
            else:
                conf, act = UNCERTAIN, "보존 (질감 표현일 수 있음)"
            fix = None
            if conf == CERTAIN:
                fix = PixelFix(np.array([c.y]), np.array([c.x]), np.array([c.new], dtype=np.uint8))
            out.append(self._f(layer, type=c.kind, confidence=conf, proposed_action=act, evidence=ev,
                               bounds=self._bounds(layer, c.x, c.y, c.x + 1, c.y + 1),
                               pdf_ref="3~4쪽 1픽셀 잘못 칠함 / 5쪽 의도적 1픽셀", fix=fix))
        return out

    def hygiene(self, layer: LayerInfo, rgba: np.ndarray | None) -> list[Finding]:
        from ..psd_io import is_default_name
        notes = []
        if not layer.visible:
            notes.append("숨김 레이어")
        if is_default_name(layer.name):
            notes.append("기본 이름(정리 필요 여부 확인)")
        if layer.kind == "pixel" and rgba is not None and not (rgba[..., 3] > 0).any():
            notes.append("완전히 투명한 빈 레이어")
        if layer.kind == "pixel" and layer.width * layer.height == 0:
            notes.append("픽셀 없는 빈 레이어")
        if layer.clipping:
            notes.append("클리핑 마스크 레이어")
        if layer.has_effects:
            notes.append("레이어 효과 있음 (픽셀 수정 시 효과도 따라 변함)")
        if not notes:
            return []
        return [self._f(layer, type="layer_hygiene", confidence=INFO,
                        proposed_action="자동 삭제/변경하지 않음 (보고만)", evidence={"notes": notes},
                        bounds=tuple(layer.bbox))]

    # ---- PDF 대조 후 추가
    PIXEL_TYPES = ("isolated_speck", "odd_pixel", "pinhole", "edge_spur", "edge_notch")

    def density_guard(self, layer, findings: list[Finding], shape) -> None:
        """6쪽 오리 사례: 점묘처럼 1픽셀 후보가 한곳에 몰린 영역은 의도 표현일 수 있음 → 자동 수정 보류."""
        cands = [f for f in findings if f.type in self.PIXEL_TYPES and f.bounds and f.confidence != INFO]
        if len(cands) < 5:
            return
        ox, oy = layer.bbox[0], layer.bbox[1]
        pts = [(min(max(f.bounds[1] - oy, 0), shape[0] - 1), min(max(f.bounds[0] - ox, 0), shape[1] - 1))
               for f in cands]
        for f, d in zip(cands, A.dense_mask(pts, shape)):
            if d and f.confidence == CERTAIN:
                f.confidence, f.fix = UNCERTAIN, None
                f.proposed_action = "보고만 (1픽셀 후보가 한곳에 밀집 → 점묘 등 의도 표현일 수 있음, 6쪽)"
                f.evidence["dense_cluster"] = True

    def internal_seam_findings(self, layer, rgba) -> list[Finding]:
        out = []
        for d, pos, a0, a1, n in A.internal_seams(rgba):
            h, w = rgba.shape[:2]     # 이음매는 보통 선 전체로 이어지므로 위치 표시는 선 전체(근거에 실제 구간 기록)
            if d == "vertical":
                b = self._bounds(layer, pos - 1, 0, pos + 1, h)
            else:
                b = self._bounds(layer, 0, pos - 1, w, pos + 1)
            out.append(self._f(layer, type="internal_seam", confidence=UNCERTAIN,
                               proposed_action="보고만 (캔버스 안쪽 타일 이음매가 어긋남 의심 — 자동 수정 안 함)",
                               evidence={"direction": d, "position": pos + (layer.bbox[0] if d == "vertical" else layer.bbox[1]),
                                         "straight_cut_segments": n, "segment_span": [a0, a1]},
                               bounds=b, pdf_ref="10쪽 지무늬 리핏 / 8쪽 모티프 리핏"))
        return out

