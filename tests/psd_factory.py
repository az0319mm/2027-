"""테스트용 레이어 PSD 생성기 (psd-tools 사용).

주의: 실제 Photoshop이 저장한 PSD가 아니라 psd-tools가 만든 PSD다.
Photoshop 저장 PSD 고유의 태그 블록(lmfx, lsct 등)은 일부만 포함된다 → 실제 PSD 테스트는 별도로 필요.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image
from psd_tools import PSDImage
from psd_tools.api.layers import PixelLayer
from psd_tools.constants import BlendMode


def _set_alpha(layer, psd, alpha: np.ndarray):
    """psd-tools frompil 은 RGB 문서에서 alpha 를 마스크로 만들므로, 투명 채널(-1)에 직접 기록."""
    rec = layer._record
    h = psd._record.header
    depth = int(h.depth)
    data = alpha.tobytes() if depth == 8 else (alpha.astype(">u2") * 257).tobytes()
    for ci, cd in zip(rec.channel_info, layer._channels):
        if int(ci.id) == -1:
            cd.set_data(data, alpha.shape[1], alpha.shape[0], depth, int(h.version))
            ci.length = 2 + len(cd.data)
            return
    raise RuntimeError("transparency channel not found")


def _make_layer(psd, parent, spec):
    arr = np.asarray(spec["rgba"], dtype=np.uint8)
    left, top = spec.get("left", 0), spec.get("top", 0)
    if spec.get("trim", True) and arr[..., 3].any():   # Photoshop처럼 내용 경계로 레이어 bounds 축소
        ys, xs = np.nonzero(arr[..., 3])
        arr = arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        left, top = left + int(xs.min()), top + int(ys.min())
    if psd._record.header.color_mode == 1:
        im = Image.fromarray(np.ascontiguousarray(arr[..., 0]), "L")
    else:
        im = Image.fromarray(np.ascontiguousarray(arr[..., :3]), "RGB")
    lay = PixelLayer.frompil(im, parent, "layer", top=top, left=left)
    lay.name = spec["name"]   # 한글 이름: Photoshop처럼 유니코드 이름 블록(luni)에 저장
    _set_alpha(lay, psd, np.ascontiguousarray(arr[..., 3]))
    if not spec.get("visible", True):
        lay.visible = False
    if "opacity" in spec:
        lay.opacity = spec["opacity"]
    if "blend" in spec:
        lay.blend_mode = BlendMode[spec["blend"]]
    if spec.get("lock"):
        from psd_tools.constants import ProtectedFlags
        lay.lock()                                  # psd-tools: 첫 호출은 블록만 생성
        lay.lock(ProtectedFlags.COMPLETE)
    return lay


def build_psd(path: str | Path, size: tuple[int, int], layers: list[dict], bg=(255, 255, 255),
              mode: str = "RGB", depth: int = 8) -> Path:
    """layers: 아래→위 순서. 각 항목:
      {name, rgba(HxWx4), left, top, visible, opacity(0~255), blend, lock}
      또는 그룹 {group: "이름", layers: [...]}"""
    W, H = size
    psd = PSDImage.new(mode, (W, H), color=bg if mode == "RGB" else bg[0], depth=depth)
    for spec in layers:
        if "group" in spec:
            kids = [_make_layer(psd, psd, s) for s in spec["layers"]]
            g = psd.create_group(kids, name="group")
            g.name = spec["group"]
        else:
            _make_layer(psd, psd, spec)
    path = Path(path)
    psd.save(path)
    return path


def blank(h, w):
    return np.zeros((h, w, 4), np.uint8)


def rect(a, y0, y1, x0, x1, color, alpha=255):
    a[y0:y1, x0:x1] = (*color, alpha)
    return a


def disk(a, cy, cx, r, color, alpha=255):
    yy, xx = np.mgrid[:a.shape[0], :a.shape[1]]
    m = (yy - cy) ** 2 + (xx - cx) ** 2 <= r * r
    a[m] = (*color, alpha)
    return a
