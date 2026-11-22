"""플러그인 JS 엔진 ↔ Python 엔진 결과 일치 검증 (같은 작업 폴더 → 같은 패치·같은 검출 집계).
플러그인에는 이 테스트를 통과한 JS 엔진만 들어간다."""
from __future__ import annotations

import json
import shutil
import subprocess
import zlib
from pathlib import Path

import numpy as np
import pytest

import test_cleanup as T
from job_factory import make_job
from psd_factory import blank, rect

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")


def _scenarios():
    W, H = T.W, T.H
    bg = dict(name="배경", rgba=T._bg(), components=3, background=True)
    sc = {}
    sc["apparel_defects"] = ((W, H), [dict(name="motif", rgba=T._defect_motif()), bg], {"work_type": "apparel"})
    sc["apparel_shapes"] = ((W, H), [dict(name="shapes", rgba=T._shapes_layer()), bg], {"work_type": "apparel"})
    sc["apparel_palette"] = ((W, H), [dict(name="sep", rgba=T._aa_layer())],
                             {"work_type": "apparel", "allowed_colors": ["#DC5078", "#5A3C32", "#FAF5EB"]})
    m = blank(H, W)
    for cx in (15, 45, 75, 105):
        T.disk(m, 40, cx, 10, T.PINK)
        m[36, cx - 3] = (255, 255, 255, 255)
    for x in range(10, 110, 6):
        m[80, x] = (*T.BROWN, 255)
    sc["apparel_repeat"] = ((W, H), [dict(name="dots", rgba=m)], {"work_type": "apparel"})
    sc["apparel_fix_repeats"] = ((W, H), [dict(name="dots", rgba=m)], {"work_type": "apparel", "fix_repeats": True})
    d = T._diagonal_two_color()
    for x in (17, 44, 78, 101):
        y = 0
        while not (d[y, x, 0] == T.ORANGE[0] and d[y + 1, x, 0] == T.MAGENTA[0]):
            y += 1
        d[y, x] = (*T.MAGENTA, 255)
    d[40, 33] = (200, 200, 190, 255)
    sc["apparel_diagonal"] = ((120, 90), [dict(name="L", rgba=d, components=3)], {"work_type": "apparel"})
    rng = np.random.default_rng(3)
    duck = rect(blank(H, W), 5, 85, 5, 115, (235, 235, 235))
    for _ in range(25):
        duck[int(rng.integers(30, 50)), int(rng.integers(60, 80))] = (30, 30, 230, 255)
    duck[70, 20] = (30, 30, 230, 255)
    sc["apparel_dense"] = ((W, H), [dict(name="duck", rgba=duck)], {"work_type": "apparel"})
    base = np.zeros((H, W), np.int8)
    base[10:50, 10:60] = 1
    base[30, 60] = 1
    base[70, 100] = 1
    layers = []
    for k, col, name in ((1, T.PINK, "Pink"), (0, T.CREAM, "Ivory")):
        a = blank(H, W)
        a[base == k] = (*col, 255)
        layers.append(dict(name=name, rgba=a))
    sc["apparel_separation"] = ((W, H), layers, {"work_type": "apparel", "repeat": "both"})
    for style in ("digital", "watercolor"):
        sc[f"interior_{style}"] = ((W, H), [dict(name="paint", rgba=T._interior_layer()), bg],
                                   {"work_type": "interior", "interior_style": style})
    rgba, paper, _ = T._scan_on_paper()
    sc["interior_paper"] = ((160, 160), [dict(name="motif", rgba=rgba),
                                         dict(name="bg", rgba=rect(blank(160, 160), 0, 160, 0, 160, (5, 0, 254)), components=3)],
                            {"work_type": "interior", "interior_style": "watercolor", "remove_paper": True})
    t = T._spots_tile()
    big = np.tile(t, (3, 3, 1))
    big[48:, 96:] = np.roll(np.roll(big[48:, 96:], 17, axis=0), 11, axis=1)
    sc["interior_seams"] = ((144, 144), [dict(name="지무늬", rgba=big, components=3)], {"work_type": "interior", "repeat": "both"})
    sc["tile_broken"] = ((64, 64), [dict(name="tile", rgba=T._tile(True), components=3)], {"work_type": "interior", "repeat": "both"})
    sc["unsupported"] = ((W, H), [dict(name="글자", kind="text"), dict(name="잠김", rgba=T._defect_motif(), locked=True),
                                   dict(name="ok", rgba=T._defect_motif())], {"work_type": "apparel",
                                   "protected_regions": [{"x": 95, "y": 65, "w": 10, "h": 10}]})
    return sc


SCEN = _scenarios()


def _python(job: Path) -> dict:
    from psd_cleanup.exchange import process_job
    r = process_job(job)
    out = []
    for p in r["patches"]:
        data = (job / p["file"]).read_bytes()
        q = {k: p[k] for k in ("order", "path", "left", "top", "width", "height", "components", "changed_pixels",
                               "expected_hash", "expected_bounds")}
        q["data_crc"] = "%08x" % (zlib.crc32(data) & 0xFFFFFFFF)
        out.append(q)
    return {"summary": r["summary"], "patches": out, "skipped": [x["path"] for x in r["skipped_layers"]]}


def _js(job: Path) -> dict:
    out = subprocess.run(["node", str(ROOT / "tests" / "uxp_mock" / "run_js_engine.js"), str(job)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.parametrize("name", sorted(SCEN))
def test_js_engine_matches_python(tmp_path, name):
    size, layers, cfg = SCEN[name]
    job_py = make_job(tmp_path / "py", size, layers, cfg)
    job_js = make_job(tmp_path / "js", size, layers, cfg)
    py, js = _python(job_py), _js(job_js)
    assert js["patches"] == py["patches"], (name, js["patches"], py["patches"])
    assert js["skipped"] == py["skipped"]
    ps, jsum = py["summary"], js["summary"]
    for k in ("applied_fixes", "changed_pixels_total", "patched_layers", "separation_mode", "findings_by_type"):
        assert jsum[k] == ps[k], (name, k, jsum[k], ps[k])
