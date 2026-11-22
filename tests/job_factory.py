"""플러그인(UXP)이 만드는 작업 폴더를 흉내 내는 생성기 (엔진 테스트용)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np


def make_job(job_dir: Path, size, layers: list[dict], config: dict, kind: str = "cleanup",
             mode: str = "RGBColorMode", bits: str = "bitDepth8") -> Path:
    """layers: 위→아래 순서. {name, rgba(캔버스 크기 HxWx4), kind, visible, opacity(0-100), blendMode, locked, components(3/4), group}"""
    job_dir.mkdir(parents=True, exist_ok=True)
    W, H = size
    out = []
    for order, L in enumerate(layers):
        kind_ = L.get("kind", "pixel")
        e = {"order": order, "id": 100 + order, "name": L["name"], "path": L.get("path", L["name"]), "kind": kind_,
             "visible": L.get("visible", True), "effectiveVisible": L.get("visible", True),
             "opacity": L.get("opacity", 100), "fillOpacity": 100, "blendMode": L.get("blendMode", "normal"),
             "isClippingMask": False, "isBackgroundLayer": L.get("background", False),
             "allLocked": L.get("locked", False), "pixelsLocked": False, "positionLocked": False,
             "transparentPixelsLocked": False, "hasMask": L.get("mask", False)}
        if kind_ == "pixel" and "rgba" in L:
            rgba = np.asarray(L["rgba"], np.uint8)
            comps = L.get("components", 4)
            if comps == 4:
                ys, xs = np.nonzero(rgba[..., 3] > 0)
                if len(ys) == 0:
                    out.append(dict(e, bounds=[0, 0, 0, 0], components=4))
                    continue
                y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
                data = rgba[y0:y1, x0:x1]
            else:
                y0, y1, x0, x1 = 0, H, 0, W
                data = rgba[..., :3]
            f = f"layer_{order}.raw"
            np.ascontiguousarray(data).tofile(job_dir / f)
            e.update(bounds=[int(x0), int(y0), int(x1), int(y1)], components=comps, file=f)
        out.append(e)
    req = {"version": 1, "job_id": job_dir.name, "kind": kind,
           "document": {"name": "test.psd", "width": W, "height": H, "resolution": 300, "mode": mode,
                        "bitsPerChannel": bits, "colorProfileName": "sRGB IEC61966-2.1"},
           "config": config, "layers": out}
    (job_dir / "request.json").write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    return job_dir


def apply_patches(job_dir: Path, result: dict, layers: list[dict], size):
    """플러그인이 putPixels 로 하는 일을 numpy 로 재현 → 레이어별 캔버스 RGBA."""
    W, H = size
    canv = {i: np.array(L["rgba"], np.uint8).copy() for i, L in enumerate(layers) if "rgba" in L}
    for p in result["patches"]:
        c = p["components"]
        reg = np.fromfile(job_dir / p["file"], dtype=np.uint8).reshape(p["height"], p["width"], c)
        tgt = canv[p["order"]]
        y, x = p["top"], p["left"]
        if c == 3:
            tgt[y:y + p["height"], x:x + p["width"], :3] = reg
        else:
            tgt[y:y + p["height"], x:x + p["width"]] = reg
    return canv
