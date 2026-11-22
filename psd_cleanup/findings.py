"""오류 후보(Finding) 자료 구조."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# 신뢰도 등급 (명세서 4장)
CERTAIN = "certain"          # 확실: 자동 수정 대상
UNCERTAIN = "uncertain"      # 불확실: 수정하지 않고 보고
INTENTIONAL = "intentional"  # 의도적: 보호 목록/통과 사례 → 변경 금지
UNSUPPORTED = "unsupported"  # 지원 불가 → 변경 금지
INFO = "info"                # 참고 정보 (오류 아님)

CONF_LABELS = {CERTAIN: "확실", UNCERTAIN: "불확실(검토 필요)", INTENTIONAL: "의도적(보존)",
               UNSUPPORTED: "지원 불가", INFO: "정보"}

TYPE_LABELS = {
    "isolated_speck": "고립 점(원픽셀/잔여 점)",
    "odd_pixel": "1픽셀 잘못 칠한 부분",
    "pinhole": "1픽셀 구멍",
    "small_hole": "작은 구멍/단절",
    "edge_spur": "경계 1픽셀 돌출",
    "edge_notch": "경계 1픽셀 패임",
    "off_palette_edge": "경계 혼합색(허용 색 외)",
    "off_palette": "허용 색 목록 밖 색",
    "color_count": "도수(색 수)",
    "rare_color": "희소 색(경계 혼색 의심)",
    "semi_alpha_edge": "반투명 경계 픽셀",
    "layer_multi_color": "분판 레이어에 복수 색",
    "faint_residue": "덜 지워진 반투명 잔여",
    "paper_fringe": "스캔 종이색 외곽 잔여",
    "scan_background": "스캔 배경 미제거 의심",
    "bg_contrast_residue": "배경색 대비 시 보이는 잔여",
    "repeat_discontinuity": "리핏 연결부 불연속",
    "internal_seam": "캔버스 내부 타일 이음매 어긋남",
    "separation_mismatch": "분판 구성 불일치",
    "separation_info": "분판 모드",
    "paper_removed": "스캔 종이 배경 제거(사용자 요청)",
    "edge_foreign_speck": "외곽 이질 점(도장툴 흔적 의심)",
    "hard_edge": "외곽 계단(매끄럽지 않음)",
    "line_artifact": "1px 직선 자국",
    "layer_hygiene": "레이어 구조 점검",
    "unsupported_layer": "지원하지 않는 레이어",
    "document": "문서 정보",
}

_counter = itertools.count(1)


@dataclass
class PixelFix:
    """레이어 로컬 좌표의 픽셀 변경. new 는 RGBA (len 4)."""
    ys: np.ndarray
    xs: np.ndarray
    new: np.ndarray  # (N,4) uint8

    @property
    def count(self) -> int:
        return int(len(self.ys))


@dataclass
class Finding:
    type: str
    engine: str                     # common / apparel / interior
    scope: str                      # layer / composite / document
    confidence: str
    proposed_action: str
    evidence: dict[str, Any] = field(default_factory=dict)
    bounds: tuple[int, int, int, int] | None = None   # 캔버스 좌표 x0,y0,x1,y1 (x1,y1 미포함)
    layer_index: int | None = None
    layer_name: str | None = None
    layer_id: int | None = None
    pdf_ref: str = ""
    fix: PixelFix | None = None
    applied: bool = False
    changed_pixel_count: int = 0
    skip_reason: str = ""
    id: str = field(default_factory=lambda: f"F{next(_counter):05d}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "type": self.type, "type_label": TYPE_LABELS.get(self.type, self.type),
            "engine": self.engine, "scope": self.scope,
            "layer_index": self.layer_index, "layer_name": self.layer_name, "layer_id": self.layer_id,
            "bounds": list(self.bounds) if self.bounds else None,
            "confidence": self.confidence, "confidence_label": CONF_LABELS[self.confidence],
            "evidence": _jsonable(self.evidence), "proposed_action": self.proposed_action,
            "pdf_ref": self.pdf_ref, "applied": self.applied,
            "changed_pixel_count": self.changed_pixel_count, "skip_reason": self.skip_reason,
        }


def _jsonable(v):
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray):
        return v.tolist()
    return v
