"""플러그인 ↔ 엔진 파일 교환 테스트 (플러그인이 만들 작업 폴더를 흉내)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from job_factory import apply_patches, make_job
from psd_factory import blank, rect
from test_cleanup import BROWN, CREAM, PINK, H, W, _bg, _defect_motif, _interior_layer, EXPECTED_DEFECTS

ROOT = Path(__file__).resolve().parents[1]


def _process(job):
    from psd_cleanup.exchange import handle_one
    handle_one(job)
    return json.loads((job / "result.json").read_text(encoding="utf-8"))


def _changed(a, b):
    vis = lambda x: np.where(x[..., 3:4] == 0, 0, x)  # noqa: E731
    return set(map(tuple, np.argwhere(np.any(vis(a) != vis(b), axis=2)).tolist()))


def test_apparel_job_patches_fix_exactly_the_defects(tmp_path):
    layers = [dict(name="motif", rgba=_defect_motif()), dict(name="배경", rgba=_bg(), components=3, background=True)]
    job = make_job(tmp_path / "j1", (W, H), layers, {"work_type": "apparel"})
    r = _process(job)
    assert r["status"] == "ok", r
    out = apply_patches(job, r, layers, (W, H))
    assert _changed(layers[0]["rgba"], out[0]) == EXPECTED_DEFECTS
    assert np.array_equal(out[1], layers[1]["rgba"])                     # 배경은 그대로
    assert r["summary"]["applied_fixes"] == 5 and len(r["patches"]) == 1
    # 지문: 플러그인이 다시 읽은 레이어(트림·투명 RGB 정규화)와 비교할 값
    from psd_cleanup.exchange import _trim_layer, crc32_hex
    t, b = _trim_layer(out[0], 4, 0, 0)
    assert r["patches"][0]["expected_hash"] == crc32_hex(t) and r["patches"][0]["expected_bounds"] == b
    assert (job / "report.html").exists() and (job / "compare.png").exists()


def test_recheck_job_reports_without_patches(tmp_path):
    layers = [dict(name="motif", rgba=_defect_motif())]
    r = _process(make_job(tmp_path / "j2", (W, H), layers, {"work_type": "apparel"}, kind="recheck"))
    assert r["patches"] == [] and r["summary"]["unresolved_count"] >= 5


def test_separation_job_moves_pixels_between_layers(tmp_path):
    base = np.zeros((H, W), np.int8)
    base[10:50, 10:60] = 1
    base[30, 60] = 1
    base[70, 100] = 1
    layers = []
    for k, col, name in ((1, PINK, "Pink"), (0, CREAM, "Ivory")):
        a = blank(H, W)
        a[base == k] = (*col, 255)
        layers.append(dict(name=name, rgba=a))
    job = make_job(tmp_path / "j3", (W, H), layers, {"work_type": "apparel"})
    r = _process(job)
    assert r["summary"]["separation_mode"] is True
    out = apply_patches(job, r, layers, (W, H))
    cov = (out[0][..., 3] > 0).astype(int) + (out[1][..., 3] > 0).astype(int)
    assert (cov == 1).all()                                             # 구멍·겹침 없음
    assert out[0][30, 60, 3] == 0 and out[1][30, 60, 3] == 255


def test_interior_paper_removal_job(tmp_path):
    from test_cleanup import _scan_on_paper
    rgba, paper, d = _scan_on_paper()
    bg = rect(blank(160, 160), 0, 160, 0, 160, (5, 0, 254))
    layers = [dict(name="motif", rgba=rgba), dict(name="bg", rgba=bg, components=3)]
    job = make_job(tmp_path / "j4", (160, 160), layers,
                   {"work_type": "interior", "interior_style": "watercolor", "remove_paper": True})
    r = _process(job)
    out = apply_patches(job, r, layers, (160, 160))
    assert out[0][15, 20, 3] == 0 and out[0][110, 80, 3] == 255
    assert np.array_equal(out[1], bg)


def test_unsupported_layers_are_skipped_and_reported(tmp_path):
    layers = [dict(name="글자", kind="text"), dict(name="잠김", rgba=_defect_motif(), locked=True),
              dict(name="마스크", rgba=_defect_motif(), mask=True), dict(name="ok", rgba=_defect_motif())]
    r = _process(make_job(tmp_path / "j5", (W, H), layers, {"work_type": "apparel"}))
    names = {x["path"] for x in r["skipped_layers"]}
    assert {"글자", "잠김", "마스크"} <= names
    assert {p["path"] for p in r["patches"]} == {"ok"}


def test_16bit_document_not_modified(tmp_path):
    layers = [dict(name="motif", rgba=_defect_motif())]
    r = _process(make_job(tmp_path / "j6", (W, H), layers, {"work_type": "apparel"}, bits="bitDepth16"))
    assert r["patches"] == []


def test_bad_request_gives_failed_result(tmp_path):
    job = tmp_path / "bad"
    job.mkdir()
    (job / "request.json").write_text("{not json", encoding="utf-8")
    r = _process(job)
    assert r["status"] == "failed" and "error" in r


def test_watch_loop_processes_and_cleans(tmp_path, monkeypatch):
    home = tmp_path / "home"
    env = dict(os.environ, PSD_CLEANUP_HOME=str(home), PYTHONPATH=str(ROOT))
    proc = subprocess.Popen([sys.executable, "-m", "psd_cleanup", "watch"], env=env, cwd=str(ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            if (home / "engine.json").exists():
                break
            time.sleep(0.1)
        assert (home / "engine.json").exists()
        layers = [dict(name="motif", rgba=_defect_motif())]
        jobdir = home / "jobs" / "abc"
        tmpdir = home / "jobs" / ".abc_tmp"
        make_job(tmpdir, (W, H), layers, {"work_type": "apparel"})
        tmpdir.rename(jobdir)
        for _ in range(200):
            if (jobdir / "result.json").exists():
                break
            time.sleep(0.1)
        r = json.loads((jobdir / "result.json").read_text(encoding="utf-8"))
        assert r["status"] == "ok" and r["summary"]["applied_fixes"] == 5
        # 두 번째 엔진은 실행되지 않음 (중복 실행 방지)
        out2 = subprocess.run([sys.executable, "-m", "psd_cleanup", "watch"], env=env, cwd=str(ROOT),
                              capture_output=True, text=True, timeout=20).stdout
        assert "이미 다른 엔진" in out2
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_heartbeat_keeps_updating_while_busy(tmp_path, monkeypatch):
    """실제 Photoshop 테스트에서 발견: 큰 PSD 처리 중 생존 신호가 멈춰 플러그인이 '엔진 꺼짐'으로 오판."""
    import psd_cleanup.exchange as X
    monkeypatch.setattr(X, "ROOT", tmp_path)
    (tmp_path / "jobs" / "slow").mkdir(parents=True)
    (tmp_path / "jobs" / "slow" / "request.json").write_text("{}", encoding="utf-8")

    def slow(job):
        time.sleep(4.5)
        return {"status": "ok"}
    monkeypatch.setattr(X, "process_job", slow)
    t = threading.Thread(target=X.watch, kwargs={"once": True}, daemon=True)
    t.start()
    seen = []
    for _ in range(40):
        time.sleep(0.1)
        if (tmp_path / "engine.json").exists():
            d = json.loads((tmp_path / "engine.json").read_text(encoding="utf-8"))
            seen.append((time.time() - d["time"], d["state"]))
    t.join(timeout=10)
    busy_ages = [age for age, st in seen if st == "busy"]
    assert busy_ages and max(busy_ages) < 3.0          # 바쁜 동안에도 2초 간격으로 갱신
    assert json.loads((tmp_path / "jobs" / "slow" / "result.json").read_text())["status"] == "ok"


def test_concurrent_heartbeat_writes_do_not_collide(tmp_path, monkeypatch):
    """실제 Mac 에서 발견: 생존 신호 스레드와 작업 스레드가 같은 임시 파일로 engine.json 을 써서 충돌 → 엔진 꺼짐."""
    import psd_cleanup.exchange as X
    monkeypatch.setattr(X, "ROOT", tmp_path)
    errors = []

    def spam():
        for _ in range(300):
            try:
                X._heartbeat("busy", "j")
            except Exception as ex:  # noqa: BLE001
                errors.append(ex)
    ts = [threading.Thread(target=spam) for _ in range(4)]
    for th in ts:
        th.start()
    for th in ts:
        th.join()
    assert not errors
    assert json.loads((tmp_path / "engine.json").read_text(encoding="utf-8"))["state"] == "busy"
    assert not list(tmp_path.glob(".tmp_*"))


def test_interrupted_job_gets_failed_result(tmp_path, monkeypatch):
    import psd_cleanup.exchange as X
    monkeypatch.setattr(X, "ROOT", tmp_path)
    d = tmp_path / "jobs" / "crashed"
    d.mkdir(parents=True)
    (d / "request.json").write_text("{}", encoding="utf-8")
    (d / ".processing").write_text("123")
    assert X.recover_interrupted_jobs() == 1
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))
    assert r["status"] == "failed" and "중단" in r["error"]
