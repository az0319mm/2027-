"""결함 주입 평가: 깨끗한 디자인 이미지 → 레이어 PSD 생성 → 위치를 아는 결함 주입 → 클린업 → 재현율/오탐 측정.

사용:
  python tools/eval_injected.py 디자인.png --out 평가폴더 [--colors 12] [--n 40] [--seed 0]

결과는 '합성 결함'에 대한 측정일 뿐이며 실제 졸업 검수 정확도를 의미하지 않는다.
실제 '불통 원본 PSD + 통과 클린업 PSD' 쌍이 생기면 tools/eval_pairs.py 로 평가한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from psd_cleanup.config import ProjectConfig, to_hex  # noqa: E402
from psd_cleanup.pipeline import run_file  # noqa: E402
from psd_cleanup.psd_io import PSDDocument  # noqa: E402
from psd_factory import build_psd  # noqa: E402


def layer_canvas(doc, name):
    l = next(l for l in doc.layers if l.name == name)
    out = np.zeros((doc.doc_info.height, doc.doc_info.width, 4), np.uint8)
    x0, y0, x1, y1 = l.bbox
    out[y0:y1, x0:x1] = doc.read_rgba(l.index)
    return out


def make_apparel(img: np.ndarray, n_colors: int):
    """평면색(도수 n) 디자인: 디더링 없는 양자화. 가장 많은 색 = 바탕(배경 레이어), 나머지 = 모티브 레이어."""
    q = Image.fromarray(img).quantize(colors=n_colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    pal = np.array(q.getpalette()[:n_colors * 3], np.uint8).reshape(-1, 3)
    idx = np.array(q)
    # 양자화 노이즈 정리 (3x3 다수결 2회) → 실제 어패럴 원본처럼 깨끗한 평면색 기준 디자인
    for _ in range(2):
        votes = np.stack([ndimage.uniform_filter((idx == k).astype(np.float32), size=3) for k in range(n_colors)])
        idx = votes.argmax(axis=0).astype(np.uint8)
    rgb = pal[idx]
    bg_i = np.bincount(idx.ravel()).argmax()
    motif = np.dstack([rgb, np.where(idx == bg_i, 0, 255).astype(np.uint8)])
    motif[motif[..., 3] == 0, :3] = 0
    bg = np.dstack([np.broadcast_to(pal[bg_i], rgb.shape), np.full(idx.shape, 255, np.uint8)]).copy()
    return bg, motif, [to_hex(c) for c in pal]


def make_interior(img: np.ndarray):
    """부드러운 외곽 모티브: 바탕색과의 거리로 alpha 생성 (수채화/스캔과 유사한 반투명 외곽)."""
    flat = img.reshape(-1, 3)
    vals, cnt = np.unique(flat // 4, axis=0, return_counts=True)
    paper = (vals[cnt.argmax()] * 4 + 2).astype(np.int32)
    d = np.abs(img.astype(np.int32) - paper).max(axis=2)
    alpha = np.clip((d - 6) * 255 / 30, 0, 255).astype(np.uint8)
    motif = np.dstack([img, alpha])
    bg = np.dstack([np.broadcast_to(paper.astype(np.uint8), img.shape), np.full(img.shape[:2], 255, np.uint8)]).copy()
    return bg, motif


def inject(motif: np.ndarray, kind: str, n: int, rng, mode: str):
    """주입 위치 (y, x) 와 정답(원래 픽셀) 기록. 판정이 애매하지 않은 위치만 고른다."""
    m = motif.copy()
    a = m[..., 3]
    H, W = a.shape
    truth = []
    keys = (m[..., 0].astype(np.int64) << 24) | (m[..., 1].astype(np.int64) << 16) | (m[..., 2].astype(np.int64) << 8) | a
    if kind == "speck":
        empty = ndimage.binary_erosion(a == 0, iterations=6)
    else:
        same = np.ones_like(a, bool)
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                same &= np.roll(np.roll(keys, dy, 0), dx, 1) == keys
        empty = same & (a == 255)
    empty[:8], empty[-8:], empty[:, :8], empty[:, -8:] = False, False, False, False
    ys, xs = np.nonzero(empty)
    used = np.zeros_like(a, bool)
    for i in rng.permutation(len(ys)):
        if len(truth) >= n:
            break
        y, x = int(ys[i]), int(xs[i])
        if used[y - 12:y + 13, x - 12:x + 13].any():
            continue
        used[y, x] = True
        orig = tuple(int(v) for v in m[y, x])
        if kind == "speck":
            src = m[a == 255]
            m[y, x] = src[rng.integers(len(src))] if len(src) else (200, 50, 80, 255)
            m[y, x, 3] = 255
        elif kind == "odd":
            c = m[y, x, :3].astype(int)
            m[y, x, :3] = np.clip(c + rng.choice([-1, 1], 3) * rng.integers(40, 120, 3), 0, 255)
        elif kind == "pinhole":
            m[y, x] = 0
        truth.append({"y": y, "x": x, "kind": kind, "orig": orig})
    return m, truth


def evaluate(src_psd, cfg, out_dir, before, truth, name="motif"):
    r = run_file(src_psd, cfg, out_dir, overwrite_outputs=True)
    after = layer_canvas(PSDDocument(r.outputs["clean_psd"]), name)
    vis = lambda x: np.where(x[..., 3:4] == 0, 0, x)  # noqa: E731
    changed = np.any(vis(after) != vis(before), axis=2)
    restored = sum(1 for t in truth if tuple(vis(after[t["y"]:t["y"] + 1, t["x"]:t["x"] + 1])[0, 0]) ==
                   tuple(vis(np.array([[t["orig"]]], np.uint8))[0, 0]))
    inj = np.zeros(changed.shape, bool)
    for t in truth:
        y0, y1, x0, x1 = t.get("box", (t["y"], t["y"] + 1, t["x"], t["x"] + 1))
        inj[y0:y1, x0:x1] = True
    touched = sum(1 for t in truth if changed[t["y"], t["x"]])
    other = int((changed & ~inj).sum())
    per_kind = {}
    for t in truth:
        k = per_kind.setdefault(t["kind"], {"injected": 0, "restored": 0})
        k["injected"] += 1
        a = vis(after[t["y"]:t["y"] + 1, t["x"]:t["x"] + 1])[0, 0]
        k["restored"] += int(tuple(a) == tuple(vis(np.array([[t["orig"]]], np.uint8))[0, 0]))
    return r, {
        "injected": len(truth), "restored_exactly": restored, "touched_injected": touched,
        "changed_pixels_not_injected": other, "layer_pixels": int(before.shape[0] * before.shape[1]),
        "false_change_ratio": round(other / (before.shape[0] * before.shape[1]), 8),
        "per_kind": per_kind, "verification_passed": r.summary["verification_passed"],
        "uncertain_findings": r.summary["findings_by_confidence"].get("uncertain", 0),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--out", required=True)
    ap.add_argument("--colors", type=int, default=12)
    ap.add_argument("--n", type=int, default=10, help="결함 유형별 주입 개수 (현실적인 소수)")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(a.seed)
    img = np.array(Image.open(a.image).convert("RGB"))
    H, W = img.shape[:2]
    report = {"image": Path(a.image).name, "size": [W, H], "seed": a.seed}

    # --- 어패럴
    bg, motif, pal = make_apparel(img, a.colors)
    clean = build_psd(out / "apparel_clean.psd", (W, H), [dict(name="Background", rgba=bg, trim=False),
                                                          dict(name="motif", rgba=motif)])
    cfg = ProjectConfig(work_type="apparel", allowed_colors=pal, expected_color_count=a.colors)
    r0, m0 = evaluate(clean, cfg, out / "apparel_clean_out", motif, [])
    report["apparel_clean_design"] = m0
    truth = []
    m = motif
    for kind in ("speck", "odd", "pinhole"):
        m, t = inject(m, kind, a.n, rng, "apparel")
        truth += t
    dirty = build_psd(out / "apparel_defects.psd", (W, H), [dict(name="Background", rgba=bg, trim=False),
                                                            dict(name="motif", rgba=m)])
    # 비교 기준은 '결함 주입 전 깨끗한 레이어' 가 아니라 '주입된 레이어'에서 바뀐 것
    _, m1 = evaluate(dirty, cfg, out / "apparel_defects_out", m, truth)
    report["apparel_injected"] = m1

    # --- 인테리어
    bg, motif = make_interior(img)
    for style in ("digital", "watercolor"):
        clean = build_psd(out / f"interior_clean.psd", (W, H), [dict(name="Background", rgba=bg, trim=False),
                                                               dict(name="motif", rgba=motif)])
        cfg = ProjectConfig(work_type="interior", interior_style=style)
        _, mc = evaluate(clean, cfg, out / f"interior_clean_{style}_out", motif, [])
        report[f"interior_clean_design_{style}"] = mc
    m, truth = inject(motif, "speck", a.n, rng, "interior")
    # 옅은 지우개 잔여 덩어리
    ys, xs = np.nonzero(ndimage.binary_erosion(motif[..., 3] == 0, iterations=14))
    resid = []
    for i in rng.permutation(len(ys))[: a.n * 4]:
        if len(resid) >= a.n // 2:
            break
        y, x = int(ys[i]), int(xs[i])
        if (m[y - 6:y + 7, x - 6:x + 7, 3] > 0).any():
            continue
        m[y - 2:y + 3, x - 3:x + 4] = (200, 120, 140, 12)
        resid.append({"y": y, "x": x, "kind": "faint_residue", "orig": (0, 0, 0, 0),
                      "box": (y - 2, y + 3, x - 3, x + 4)})
    dirty = build_psd(out / "interior_defects.psd", (W, H), [dict(name="Background", rgba=bg, trim=False),
                                                             dict(name="motif", rgba=m)])
    for style in ("digital", "watercolor"):
        _, mi = evaluate(dirty, ProjectConfig(work_type="interior", interior_style=style),
                         out / f"interior_defects_{style}_out", m, truth + resid)
        report[f"interior_injected_{style}"] = mi
    (out / "eval_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
