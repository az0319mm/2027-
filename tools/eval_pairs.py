"""골든 테스트셋 평가: '불통 원본 PSD' + '통과 클린업본 PSD' 쌍으로 자동 수정의 정밀도/누락/오탐을 측정.

사용:
  python tools/eval_pairs.py --pairs pairs.json --out 결과폴더
  pairs.json = [{"fail": "A_불통.psd", "pass": "A_통과.psd", "config": {"work_type": "apparel", ...}}, ...]

레이어는 '경로(그룹/이름)'로 대응시키며, 캔버스 크기·레이어 구성이 다르면 해당 레이어는 평가에서 제외하고 보고한다.
결과는 표본 수와 함께만 해석할 것 (표본이 적으면 정확도 % 를 주장하지 않는다).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from psd_cleanup.config import ProjectConfig  # noqa: E402
from psd_cleanup.pipeline import run_file  # noqa: E402
from psd_cleanup.psd_io import PSDDocument  # noqa: E402


def canvas(doc: PSDDocument, l) -> np.ndarray:
    W, H = doc.doc_info.width, doc.doc_info.height
    out = np.zeros((H, W, 4), np.uint8)
    rgba = doc.read_rgba(l.index)
    if rgba is None:
        return out
    x0, y0, x1, y1 = l.bbox
    cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if cx1 > cx0 and cy1 > cy0:
        out[cy0:cy1, cx0:cx1] = rgba[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
    return out


def vis(x):
    return np.where(x[..., 3:4] == 0, 0, x)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    pairs = json.loads(Path(a.pairs).read_text(encoding="utf-8"))
    total = {"tool_changed": 0, "correct": 0, "wrong_value": 0, "should_not_change": 0, "needed": 0, "missed": 0}
    rows = []
    for p in pairs:
        cfg = ProjectConfig.from_dict(p.get("config", {}))
        r = run_file(p["fail"], cfg, Path(a.out) / Path(p["fail"]).stem, overwrite_outputs=True)
        fail, good, tool = PSDDocument(p["fail"]), PSDDocument(p["pass"]), PSDDocument(r.outputs["clean_psd"])
        gl = {l.path: l for l in good.layers if l.kind == "pixel"}
        tl = {l.path: l for l in tool.layers if l.kind == "pixel"}
        skipped = []
        for l in fail.layers:
            if l.kind != "pixel" or l.path not in gl or l.path not in tl:
                skipped.append(l.path)
                continue
            if (fail.doc_info.width, fail.doc_info.height) != (good.doc_info.width, good.doc_info.height):
                skipped.append(l.path + " (캔버스 크기 다름)")
                continue
            f, g, t = vis(canvas(fail, l)), vis(canvas(good, gl[l.path])), vis(canvas(tool, tl[l.path]))
            need = np.any(f != g, axis=2)
            did = np.any(f != t, axis=2)
            ok = did & np.all(t == g, axis=2)
            st = {"tool_changed": int(did.sum()), "correct": int(ok.sum()),
                  "wrong_value": int((did & need & ~ok).sum()), "should_not_change": int((did & ~need).sum()),
                  "needed": int(need.sum()), "missed": int((need & ~did).sum())}
            for k in total:
                total[k] += st[k]
            rows.append({"pair": p["fail"], "layer": l.path, **st})
        rows.append({"pair": p["fail"], "skipped_layers": skipped})
    prec = total["correct"] / total["tool_changed"] if total["tool_changed"] else None
    rec = total["correct"] / total["needed"] if total["needed"] else None
    res = {"pairs": len(pairs), "totals": total, "precision_pixels": prec, "recall_pixels": rec,
           "note": "표본이 적으면 비율을 일반화하지 말 것. 'needed' 에는 사람이 한 큰 수정(재드로잉 등)도 포함됨.",
           "rows": rows}
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "pairs_report.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
