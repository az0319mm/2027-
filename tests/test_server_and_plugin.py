"""로컬 서버 API + UXP 플러그인의 레이어 대조 로직(compare.js, Node로 실행) 테스트.
Photoshop 자체에서의 동작은 이 환경에서 검증 불가 (미검증)."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from psd_cleanup import server as S
from psd_cleanup.psd_io import PSDDocument
from psd_factory import blank, build_psd, rect
from test_cleanup import H, W, _bg, _defect_motif

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def srv():
    s = S.make_server(0)
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown()
    s.server_close()


def _req(url, body=None, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


OK = {"X-PSD-Cleanup": "1"}


def test_api_requires_custom_header(srv):
    assert _req(srv + "/api/health")[0] == 403
    code, body = _req(srv + "/api/health", headers=OK)
    assert code == 200 and json.loads(body)["ok"]


def test_ui_served(srv):
    code, body = _req(srv + "/")
    assert code == 200 and "자동 클린업 실행".encode() in body


def test_host_header_rejected(srv):
    assert _req(srv + "/api/health", headers={**OK, "Host": "evil.example:80"})[0] == 403


def test_run_job_end_to_end(srv, tmp_path):
    src = build_psd(tmp_path / "a.psd", (W, H), [dict(name="Background", rgba=_bg()),
                                                dict(name="motif", rgba=_defect_motif())])
    code, body = _req(srv + "/api/run", {"inputs": [str(src)], "config": {"work_type": "apparel"},
                                         "out_dir": str(tmp_path / "out")}, OK)
    assert code == 200, body
    jid = json.loads(body)["job_id"]
    for _ in range(100):
        j = json.loads(_req(srv + f"/api/jobs/{jid}", headers=OK)[1])
        if j["status"] != "running":
            break
        time.sleep(0.1)
    assert j["status"] == "done", j
    r = j["results"][0]
    assert r["status"] == "ok" and r["summary"]["applied_fixes"] == 5
    code, png = _req(srv + "/files?path=" + urllib.request.quote(r["outputs"]["diff_png"]))
    assert code == 200 and png[:4] == b"\x89PNG"
    # 결과 폴더 밖 파일은 제공하지 않음
    assert _req(srv + "/files?path=" + urllib.request.quote(str(src)))[0] == 404


def test_inspect_and_bad_inputs(srv, tmp_path):
    src = build_psd(tmp_path / "a.psd", (W, H), [dict(name="motif", rgba=_defect_motif())])
    code, body = _req(srv + "/api/inspect", {"path": str(src)}, OK)
    assert code == 200 and json.loads(body)["layers"][0]["name"] == "motif"
    assert _req(srv + "/api/run", {"inputs": [str(tmp_path / "none.psd")]}, OK)[0] == 400
    assert _req(srv + "/api/run", {"inputs": [str(src)], "config": {"work_type": "x"}}, OK)[0] == 400


@pytest.mark.skipif(shutil.which("node") is None, reason="node 없음")
def test_uxp_compare_logic_with_node(tmp_path):
    """Photoshop DOM 형태의 가짜 레이어 트리와 서버 레이어 목록 대조."""
    src = build_psd(tmp_path / "a.psd", (W, H), [
        dict(name="Background", rgba=_bg()),
        dict(group="그룹", layers=[dict(name="motif", rgba=_defect_motif(), opacity=128, blend="MULTIPLY"),
                                  dict(name="memo", rgba=rect(blank(H, W), 0, 5, 0, 5, (1, 2, 3)), visible=False)]),
    ])
    srv_layers = [l.to_dict() for l in PSDDocument(src).layers]
    by = {l["path"]: l for l in srv_layers}

    def ps(path, kind, children=None, **kw):
        l = by[path]
        d = {"name": l["name"], "kind": kind, "visible": l["visible"], "opacity": round(l["opacity"] / 2.55),
             "blendMode": kw.get("blend", "normal"),
             "bounds": {"left": l["bbox"][0], "top": l["bbox"][1], "right": l["bbox"][2], "bottom": l["bbox"][3]}}
        if children is not None:
            d["layers"] = children
        return d

    good = [ps("그룹", "group", [ps("그룹/memo", "pixel"), ps("그룹/motif", "pixel", blend="multiply")],
               blend="passThrough"), ps("Background", "pixel")]
    bad = json.loads(json.dumps(good))
    bad[0]["layers"][1]["opacity"] = 100
    script = f"""
const c = require({json.dumps(str(ROOT / 'uxp-plugin' / 'compare.js'))});
const srv = {json.dumps(srv_layers, ensure_ascii=False)};
const g = c.compareLayerTrees(c.flattenPsLayers({json.dumps(good, ensure_ascii=False)}), srv);
const b = c.compareLayerTrees(c.flattenPsLayers({json.dumps(bad, ensure_ascii=False)}), srv);
console.log(JSON.stringify({{g, b}}));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    res = json.loads(out)
    assert res["g"]["ok"], res["g"]
    assert not res["b"]["ok"] and any("불투명도" in p for p in res["b"]["problems"])
