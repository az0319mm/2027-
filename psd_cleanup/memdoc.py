"""플러그인(UXP)이 Imaging API 로 꺼내 보낸 레이어 픽셀을 PSDDocument 와 같은 모양으로 다루는 메모리 문서.

작업 폴더 구조 (플러그인이 작성):
  request.json      문서 정보 · 설정 · 레이어 목록(위→아래 순서)
  layer_<order>.raw 레이어 픽셀 (8bit, chunky, RGBA=4 또는 RGB=3, sourceBounds 크기)
  composite.raw     문서 합성 이미지 (선택)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .psd_io import DocumentInfo, LayerInfo

_BLEND = {"normal": "NORMAL", "passThrough": "PASS_THROUGH", "dissolve": "DISSOLVE", "darken": "DARKEN",
          "multiply": "MULTIPLY", "colorBurn": "COLOR_BURN", "linearBurn": "LINEAR_BURN", "darkerColor": "DARKER_COLOR",
          "lighten": "LIGHTEN", "screen": "SCREEN", "colorDodge": "COLOR_DODGE", "linearDodge": "LINEAR_DODGE",
          "lighterColor": "LIGHTER_COLOR", "overlay": "OVERLAY", "softLight": "SOFT_LIGHT", "hardLight": "HARD_LIGHT",
          "vividLight": "VIVID_LIGHT", "linearLight": "LINEAR_LIGHT", "pinLight": "PIN_LIGHT", "hardMix": "HARD_MIX",
          "difference": "DIFFERENCE", "exclusion": "EXCLUSION", "subtract": "SUBTRACT", "divide": "DIVIDE",
          "hue": "HUE", "saturation": "SATURATION", "color": "COLOR", "luminosity": "LUMINOSITY"}


class MemoryDocument:
    def __init__(self, job_dir: str | Path):
        self.dir = Path(job_dir)
        self.req: dict[str, Any] = json.loads((self.dir / "request.json").read_text(encoding="utf-8"))
        d = self.req["document"]
        self.width, self.height = int(d["width"]), int(d["height"])
        mode, bits = str(d.get("mode", "")), str(d.get("bitsPerChannel", ""))
        reasons = []
        if "RGB" not in mode.upper():
            reasons.append(f"색상 모드 {mode} (자동 수정은 RGB 만 지원)")
        if bits and "8" not in bits:
            reasons.append(f"{bits} 문서 (자동 수정은 8비트만 지원 — 16/32비트 정밀도 손실 방지)")
        n = len(self.req["layers"])
        self.doc_info = DocumentInfo(
            path=str(d.get("path", "")), sha256="", version=1, width=self.width, height=self.height, channels=3,
            depth=8 if (not bits or "8" in bits) else 16,
            color_mode=3 if "RGB" in mode.upper() else 0, color_mode_name=mode or "RGB",
            has_icc=bool(d.get("colorProfileName")), icc_bytes_sha256=None,
            resolution={"ppi": d.get("resolution")}, has_merged_image=bool(self.req.get("composite")),
            layer_count=n, pixel_edit_supported=not reasons, unsupported_reasons=reasons)
        self.layers: list[LayerInfo] = []
        self._files: dict[int, dict] = {}
        parents: dict[int, bool] = {}
        for L in self.req["layers"]:
            order = int(L["order"])
            idx = n - 1 - order                     # psd-tools 처럼 0 = 맨 아래
            kind = str(L.get("kind", "pixel"))
            b = L.get("bounds") or [0, 0, 0, 0]
            comps = int(L.get("components") or 0)
            locks = {"transparency": bool(L.get("transparentPixelsLocked")), "composite": bool(L.get("pixelsLocked")),
                     "position": bool(L.get("positionLocked")), "all": bool(L.get("allLocked"))}
            info = LayerInfo(
                index=idx, layer_id=L.get("id"), name=L.get("name", ""), path=L.get("path", ""), kind=kind,
                visible=bool(L.get("visible", True)), effective_visible=bool(L.get("effectiveVisible", L.get("visible", True))),
                opacity=int(round(float(L.get("opacity", 100)) * 2.55)),
                fill_opacity=int(round(float(L.get("fillOpacity", 100)) * 2.55)),
                blend_mode=_BLEND.get(str(L.get("blendMode", "normal")), str(L.get("blendMode", "")).upper()),
                bbox=(int(b[0]), int(b[1]), int(b[2]), int(b[3])), clipping=bool(L.get("isClippingMask")),
                has_mask=bool(L.get("hasMask")), has_vector_mask=False, has_effects=False, locks=locks,
                has_alpha_channel=comps == 4, channel_ids=[-1, 0, 1, 2] if comps == 4 else [0, 1, 2])
            r = info.unsupported_reasons
            if reasons:
                r.extend(reasons)
            if kind != "pixel":
                r.append(f"비픽셀 레이어({kind})")
            if not L.get("file"):
                r.append("픽셀 데이터 없음(빈 레이어 또는 읽기 실패)")
            if locks["all"] or locks["composite"] or locks["transparency"]:
                r.append("잠긴 레이어")
            if info.has_mask:
                r.append("레이어 마스크 있음 (마스크 결합 결과 수정 미지원)")
            info.editable = not r
            self.layers.append(info)
            if L.get("file"):
                self._files[idx] = L
        self.layers.sort(key=lambda l: l.index)
        self.psd = None

    def layer_by_index(self, index: int) -> LayerInfo:
        for l in self.layers:
            if l.index == index:
                return l
        raise KeyError(index)

    def read_rgba(self, index: int) -> np.ndarray | None:
        L = self._files.get(index)
        if L is None or not self.doc_info.pixel_edit_supported:
            return None
        b = L["bounds"]
        w, h, c = b[2] - b[0], b[3] - b[1], int(L["components"])
        raw = np.fromfile(self.dir / L["file"], dtype=np.uint8)
        if raw.size != w * h * c:
            raise ValueError(f"레이어 데이터 크기 불일치: {L.get('path')}")
        a = raw.reshape(h, w, c)
        if c == 3:
            a = np.dstack([a, np.full((h, w), 255, np.uint8)])
        return a.copy()

    def original_layer_bytes(self, index: int) -> np.ndarray:
        L = self._files[index]
        b = L["bounds"]
        return np.fromfile(self.dir / L["file"], dtype=np.uint8).reshape(b[3] - b[1], b[2] - b[0], int(L["components"]))

    def merged_rgb(self) -> np.ndarray | None:
        C = self.req.get("composite")
        if C and (self.dir / C["file"]).exists():
            c = int(C["components"])
            a = np.fromfile(self.dir / C["file"], dtype=np.uint8)
            b = C.get("bounds") or [0, 0, self.width, self.height]
            w, h = b[2] - b[0], b[3] - b[1]
            if a.size == w * h * c:
                a = a.reshape(h, w, c).astype(np.float32)
                if c == 4:                                  # 투명 부분은 흰색 위로 (Photoshop 표시와 같게)
                    al = a[..., 3:4] / 255.0
                    a = a[..., :3] * al + 255 * (1 - al)
                out = np.full((self.height, self.width, 3), 255, np.uint8)
                out[b[1]:b[3], b[0]:b[2]] = np.clip(np.round(a[..., :3]), 0, 255).astype(np.uint8)
                return out
        return self.simple_composite()

    def simple_composite(self, override: dict[int, np.ndarray] | None = None) -> np.ndarray:
        """보이는 픽셀 레이어를 Normal 블렌드로 근사 합성 (미리보기·비교용. Photoshop 합성과 다를 수 있음)."""
        acc = np.full((self.height, self.width, 3), 255.0, np.float32)
        for l in sorted(self.layers, key=lambda x: x.index):
            if l.kind != "pixel" or not l.effective_visible or l.index not in self._files:
                continue
            rgba = (override or {}).get(l.index)
            if rgba is None:
                rgba = self.read_rgba(l.index)
            if rgba is None:
                continue
            x0, y0, x1, y1 = l.bbox
            cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(self.width, x1), min(self.height, y1)
            if cx1 <= cx0 or cy1 <= cy0:
                continue
            src = rgba[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0].astype(np.float32)
            al = src[..., 3:4] / 255.0 * (l.opacity / 255.0)
            acc[cy0:cy1, cx0:cx1] = src[..., :3] * al + acc[cy0:cy1, cx0:cx1] * (1 - al)
        return np.clip(np.round(acc), 0, 255).astype(np.uint8)
