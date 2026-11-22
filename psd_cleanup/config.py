"""프로젝트 설정 (JSON 저장/불러오기).

주의: 여기 있는 기본 임계값은 '공식 졸업 검수 규정'이 아니라 이 도구의 보수적 기본값이다.
도수·허용 색상·교수 협의 사항은 사용자가 입력한 값만 사용한다.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

WORK_TYPES = ("apparel", "interior")
REPEAT_MODES = ("none", "vertical", "horizontal", "both")  # 해당없음/상하/좌우/상하좌우
INTERIOR_STYLES = ("digital", "watercolor", "pencil", "crayon")  # 디지털 색면/수채화/색연필/크레파스

REPEAT_LABELS = {"none": "해당 없음", "vertical": "상하", "horizontal": "좌우", "both": "상하좌우"}
TYPE_LABELS = {"apparel": "어패럴", "interior": "인테리어"}
STYLE_LABELS = {"digital": "디지털 색면", "watercolor": "수채화", "pencil": "색연필", "crayon": "크레파스"}


@dataclass
class Region:
    """보호(또는 승인) 영역. 캔버스 좌표(px)."""
    x: int
    y: int
    w: int
    h: int
    layers: list[str] = field(default_factory=list)  # 비어 있으면 모든 레이어
    note: str = ""

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h


@dataclass
class ProjectConfig:
    work_type: str = "apparel"
    repeat: str = "none"

    # --- 어패럴 (사용자 입력이 있을 때만 사용) ---
    expected_color_count: int | None = None      # 도수
    allowed_colors: list[str] = field(default_factory=list)  # "#RRGGBB"
    one_color_per_layer: bool = False             # 레이어=분판 구조일 때만 True
    separation_mode: str = "auto"                 # auto: 분판 구조(레이어당 1색·겹침 없음)를 감지하면 분판 단위로 검사/수정, off: 끔

    # --- 인테리어 ---
    interior_style: str = "digital"
    paper_color: str | None = None                # 스캔 종이색 "#RRGGBB" (선택)
    remove_paper: bool = False                    # 인테리어: 스캔 종이 배경 지우기 (사용자가 켰을 때만, 모티브만 남김)

    # --- 공통 보호/예외 ---
    protected_layers: list[str] = field(default_factory=list)       # 이름 glob. 완전 보호(수정 금지)
    approved_texture_layers: list[str] = field(default_factory=list)  # 11쪽: 승인 질감. 내부 검사 제외, 외곽은 검사
    protected_regions: list[Region] = field(default_factory=list)

    # --- 동작 ---
    auto_fix: bool = True          # False 이면 검수만 (Phase 1 모드)
    fix_types: list[str] | None = None  # None = 엔진 기본값. 특정 유형만 수정하고 싶을 때
    fix_repeats: bool = False      # 어패럴: 복제본마다 똑같이 반복된 1픽셀도 '복사된 실수'로 보고 수정 (사람이 확인 후 켬)
    repeat_min: int = 3            # 같은 형태가 이 횟수 이상 반복되면 '의도적 반복'으로 간주 (5쪽)
    max_layer_change_ratio: float = 0.005  # 한 레이어에서 이 비율 이상 바뀌면 해당 레이어 수정 전체 취소

    def validate(self) -> None:
        if self.work_type not in WORK_TYPES:
            raise ValueError(f"work_type must be one of {WORK_TYPES}")
        if self.repeat not in REPEAT_MODES:
            raise ValueError(f"repeat must be one of {REPEAT_MODES}")
        if self.separation_mode not in ("auto", "off"):
            raise ValueError("separation_mode must be 'auto' or 'off'")
        if self.interior_style not in INTERIOR_STYLES:
            raise ValueError(f"interior_style must be one of {INTERIOR_STYLES}")
        for c in self.allowed_colors + ([self.paper_color] if self.paper_color else []):
            parse_hex(c)
        if self.expected_color_count is not None and self.expected_color_count < 1:
            raise ValueError("expected_color_count must be >= 1")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProjectConfig":
        d = dict(d)
        regions = [Region(**r) for r in d.pop("protected_regions", []) or []]
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"알 수 없는 설정 키: {sorted(unknown)}")
        cfg = cls(**d, protected_regions=regions)
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: str | Path) -> "ProjectConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def parse_hex(s: str) -> tuple[int, int, int]:
    s = s.strip().lstrip("#")
    if len(s) != 6:
        raise ValueError(f"색상은 #RRGGBB 형식이어야 합니다: {s!r}")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def to_hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(int(v) for v in rgb[:3])
