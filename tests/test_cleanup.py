"""명세서 5장 필수 테스트 + 기준 PDF 쪽별 규칙/예외 테스트.

PDF 원본 사진은 저장소에 없으므로, 각 쪽의 '통과/불통' 사례를 같은 성질의 합성 픽셀 패턴으로 재현한다.
"""
from __future__ import annotations

import io
import json
import shutil
import threading
from pathlib import Path

import numpy as np
import pytest
from psd_tools import PSDImage

from psd_cleanup.config import ProjectConfig, Region
from psd_cleanup.pipeline import Cancelled, run_batch, run_file
from psd_cleanup.psd_io import PSDDocument, sha256_file
from psd_factory import blank, build_psd, disk, rect

PINK, CREAM, BROWN, GREEN = (220, 80, 120), (250, 245, 235), (90, 60, 50), (10, 200, 10)
W, H = 120, 90


def _bg():
    return rect(blank(H, W), 0, H, 0, W, CREAM)


def _defect_motif():
    """2~4쪽 불통 사례를 모은 레이어 + 5쪽 통과 사례(반복 1px 점)."""
    m = blank(H, W)
    rect(m, 10, 50, 10, 60, PINK)
    m[30, 60] = (*PINK, 255)        # 2쪽: 곧은 경계 1px 돌출
    m[10, 30] = 0                   # 2쪽: 곧은 경계 1px 패임
    m[25, 30] = (*GREEN, 255)       # 3~4쪽: 1px 잘못 칠함
    m[35, 40] = 0                   # 1px 구멍
    m[70, 100] = (*PINK, 255)       # 1쪽: 고립 원픽셀 점
    for x in range(70, 118, 8):     # 5쪽: 의도적으로 반복 사용한 1px 점 → 보존
        m[85, x] = (*BROWN, 255)
    return m


EXPECTED_DEFECTS = {(30, 60), (10, 30), (25, 30), (35, 40), (70, 100)}  # (y, x) 캔버스 좌표


@pytest.fixture
def defect_psd(tmp_path):
    return build_psd(tmp_path / "defect.psd", (W, H), [
        dict(name="Background", rgba=_bg()),
        dict(name="motif", rgba=_defect_motif(), opacity=230, blend="MULTIPLY"),
        dict(name="hidden note", rgba=rect(blank(H, W), 60, 70, 5, 20, BROWN), visible=False),
    ])


def _layer_canvas(doc: PSDDocument, name: str) -> np.ndarray:
    l = next(l for l in doc.layers if l.name == name)
    out = np.zeros((doc.doc_info.height, doc.doc_info.width, 4), np.uint8)
    rgba = doc.read_rgba(l.index)
    x0, y0, x1, y1 = l.bbox
    out[y0:y1, x0:x1] = rgba
    return out


def _changed(a, b):
    vis = lambda x: np.where(x[..., 3:4] == 0, 0, x)  # noqa: E731
    return set(map(tuple, np.argwhere(np.any(vis(a) != vis(b), axis=2)).tolist()))


def _run(src, tmp_path, **cfg):
    cfg.setdefault("write_reports", True)
    return run_file(src, ProjectConfig(**cfg), tmp_path / "out", overwrite_outputs=True)


# ---------------------------------------------------------------- 1. 원본 보존
def test_original_hash_unchanged_and_outputs_exist(defect_psd, tmp_path):
    before = sha256_file(defect_psd)
    r = _run(defect_psd, tmp_path, work_type="apparel")
    assert r.status == "ok", r.error
    assert sha256_file(defect_psd) == before
    for k in ("clean_psd", "qa_html", "qa_json", "diff_png", "fixes_png"):
        assert Path(r.outputs[k]).exists(), k
    assert Path(r.outputs["clean_psd"]).name == "defect_CLEAN.psd"


def test_refuses_to_overwrite_existing_output_and_never_input(defect_psd, tmp_path):
    run_file(defect_psd, ProjectConfig(), tmp_path / "out")
    with pytest.raises(FileExistsError):
        run_file(defect_psd, ProjectConfig(), tmp_path / "out")
    from psd_cleanup.psd_io import write_patched_psd
    with pytest.raises(ValueError):
        write_patched_psd(defect_psd, defect_psd, {})


# ---------------------------------------------------- 2. 유효성 / 바이트 보존
def test_unmodified_roundtrip_is_byte_identical(defect_psd):
    psd = PSDImage.open(defect_psd)
    buf = io.BytesIO()
    psd._record.write(buf)
    assert buf.getvalue() == Path(defect_psd).read_bytes()


def test_inspect_mode_outputs_byte_identical_copy(defect_psd, tmp_path):
    r = _run(defect_psd, tmp_path, auto_fix=False)
    assert r.summary["applied_fixes"] == 0
    assert Path(r.outputs["clean_psd"]).read_bytes() == Path(defect_psd).read_bytes()
    assert r.summary["findings_by_confidence"].get("certain", 0) >= 5  # 검출은 그대로


# ---------------------------------------------- 3. 레이어 불변성 + 어패럴 수정
def test_apparel_fixes_exactly_the_defects(defect_psd, tmp_path):
    r = _run(defect_psd, tmp_path, work_type="apparel")
    assert r.status == "ok" and r.summary["verification_passed"]
    a, b = PSDDocument(defect_psd), PSDDocument(r.outputs["clean_psd"])
    assert _changed(_layer_canvas(a, "motif"), _layer_canvas(b, "motif")) == EXPECTED_DEFECTS
    # 비수정 레이어: 채널 바이트 동일
    for l in a.layers:
        if l.name != "motif":
            assert a.raw_channel_bytes(l.index) == b.raw_channel_bytes(l.index)
    # 레이어 속성
    for la, lb in zip(a.layers, b.layers):
        for k in ("name", "visible", "opacity", "blend_mode", "bbox", "index"):
            assert getattr(la, k) == getattr(lb, k)
    # 수정 결과 값
    c = _layer_canvas(b, "motif")
    assert tuple(c[25, 30]) == (*PINK, 255) and tuple(c[35, 40]) == (*PINK, 255)
    assert c[30, 60, 3] == 0 and c[70, 100, 3] == 0 and tuple(c[10, 30]) == (*PINK, 255)
    # 5쪽: 반복 점은 그대로
    assert all(c[85, x, 3] == 255 for x in range(70, 118, 8))


def test_merged_preview_updated_only_at_changed_pixels(defect_psd, tmp_path):
    r = _run(defect_psd, tmp_path, work_type="apparel")
    m0 = PSDDocument(defect_psd).merged_rgb()
    m1 = PSDDocument(r.outputs["clean_psd"]).merged_rgb()
    diff = set(map(tuple, np.argwhere(np.any(m0 != m1, axis=2)).tolist()))
    assert diff and diff <= EXPECTED_DEFECTS


def test_group_structure_and_hidden_layers_preserved(tmp_path):
    m = _defect_motif()
    src = build_psd(tmp_path / "g.psd", (W, H), [
        dict(name="Background", rgba=_bg()),
        dict(group="모티브 그룹", layers=[dict(name="딸기 모티브", rgba=m),
                                       dict(name="memo", rgba=rect(blank(H, W), 0, 5, 0, 5, BROWN), visible=False)]),
    ])
    r = _run(src, tmp_path, work_type="apparel")
    assert r.status == "ok", r.error
    a, b = PSDDocument(src), PSDDocument(r.outputs["clean_psd"])
    assert [l.path for l in a.layers] == [l.path for l in b.layers]
    assert "모티브 그룹/딸기 모티브" in [l.path for l in b.layers]
    assert r.summary["patched_layers"] == ["모티브 그룹/딸기 모티브"]
    assert [l.visible for l in a.layers] == [l.visible for l in b.layers]


# ------------------------------------------------ 5. 형태 보호 (가는 선·첨단)
def _shapes_layer():
    """수정되면 안 되는 정상 형태들."""
    m = blank(H, W)
    m[5, 5:40] = (*BROWN, 255)                 # 1px 가로선 (끝점 = 첨단)
    m[10:12, 5:40] = (*BROWN, 255)             # 2px 선
    rect(m, 20, 40, 5, 8, PINK)                # 3px 폭 세로 막대
    m[19, 6] = (*PINK, 255)                    # 3px 막대의 둥근 끝(1px 머리) → 돌출로 오인 금지
    for i in range(15):                        # 계단형 대각선 (픽셀 경계의 정상 계단)
        m[50 + i, 10 + i] = (*BROWN, 255)
        m[50 + i, 11 + i] = (*BROWN, 255)
    for y in range(20, 40):                    # 날카로운 삼각형 모서리
        w = (y - 20) // 2
        m[y, 60 - w:61 + w] = (*PINK, 255)
    disk(m, 70, 90, 10, PINK)                  # 원 (계단형 곡선)
    m[80:85, 110] = (*BROWN, 255)              # 꽃술 같은 가는 줄기
    m[79, 110] = (*GREEN, 255)                 # 줄기 끝의 다른 색 점(꽃밥)
    return m


@pytest.mark.parametrize("work_type", ["apparel", "interior"])
def test_thin_lines_tips_corners_preserved(tmp_path, work_type):
    src = build_psd(tmp_path / "shapes.psd", (W, H), [dict(name="Background", rgba=_bg()),
                                                     dict(name="shapes", rgba=_shapes_layer())])
    r = _run(src, tmp_path, work_type=work_type)
    assert r.status == "ok"
    assert r.summary["changed_pixels_total"] == 0, r.qa["findings"]


# ------------------------------------------------------- 4. 색상 테스트
def _aa_layer():
    """6쪽/1쪽: 두 허용 색 경계에 안티앨리어싱 중간색 (도수는 맞지만 경계에 여러 색)."""
    m = rect(blank(H, W), 10, 60, 10, 50, PINK)
    rect(m, 10, 60, 50, 90, BROWN)
    mid = tuple((np.array(PINK) + np.array(BROWN)) // 2)
    m[10:60, 50] = (*mid, 255)                 # 1px 혼합색 띠
    m[30, 20] = (40, 160, 230, 255)            # 혼합으로 설명 안 되는 색 1px (주변 PINK 단색)
    return m


def test_palette_mixed_edge_fixed_without_new_colors(tmp_path):
    src = build_psd(tmp_path / "aa.psd", (W, H), [dict(name="Background", rgba=_bg()),
                                                 dict(name="sep", rgba=_aa_layer())])
    pal = ["#DC5078", "#5A3C32", "#FAF5EB"]
    r = _run(src, tmp_path, work_type="apparel", allowed_colors=pal)
    assert r.status == "ok"
    out = _layer_canvas(PSDDocument(r.outputs["clean_psd"]), "sep")
    opaque = out[out[..., 3] == 255][:, :3]
    cols = {tuple(c) for c in opaque.tolist()}
    assert cols <= {PINK, BROWN}, cols        # 새 색 없음 + 혼합색 제거
    types = {f["type"]: f for f in r.qa["findings"] if f["applied"]}
    assert "off_palette_edge" in types
    # 1px 이물질 색은 odd_pixel 규칙으로 주변색이 됨 (새 색 아님)
    assert tuple(out[30, 20, :3]) == PINK


def test_unexplained_off_palette_block_is_reported_not_changed(tmp_path):
    m = rect(blank(H, W), 10, 60, 10, 50, PINK)
    rect(m, 20, 30, 20, 30, (40, 160, 230))    # 허용 색 밖 블록 (혼합색 아님)
    src = build_psd(tmp_path / "blk.psd", (W, H), [dict(name="L", rgba=m)])
    r = _run(src, tmp_path, work_type="apparel", allowed_colors=["#DC5078"])
    assert r.summary["changed_pixels_total"] == 0
    assert any(f["type"] == "off_palette" and f["confidence"] == "uncertain" for f in r.qa["findings"])


def test_apparel_continuous_tone_layer_not_auto_fixed(tmp_path):
    rng = np.random.default_rng(0)
    m = blank(H, W)
    m[..., :3] = rng.integers(0, 255, (H, W, 3))
    m[..., 3] = 255
    m[40, 40] = (5, 5, 5, 255)
    m[39:42, 39:42][m[39:42, 39:42, 3] > 0] = (200, 200, 200, 255)
    m[40, 40] = (5, 5, 5, 255)
    src = build_psd(tmp_path / "photo.psd", (W, H), [dict(name="photo", rgba=m)])
    r = _run(src, tmp_path, work_type="apparel")
    assert r.summary["changed_pixels_total"] == 0


# ------------------------------------------------ 인테리어: 잔여/번짐 보호
def _interior_layer():
    m = blank(H, W)
    disk(m, 40, 40, 18, PINK)
    # 12쪽: 몸체에 붙은 반투명 번짐(그라데이션 알파) → 보존
    yy, xx = np.mgrid[:H, :W]
    d = np.sqrt((yy - 40) ** 2 + (xx - 40) ** 2)
    ring = (d > 18) & (d <= 26)
    m[ring] = (*PINK, 0)
    m[..., 3] = np.where(ring, (255 * (26 - d) / 8).clip(1, 254).astype(np.uint8), m[..., 3])
    # 7·9쪽: 몸체와 떨어진 옅은 지우개 잔여
    rect(m, 75, 80, 90, 98, PINK, alpha=10)    # 매우 옅음 (수채화 프로필도 자동 제거)
    rect(m, 10, 14, 95, 100, PINK, alpha=30)   # 옅음 (디지털만 자동 제거)
    rect(m, 60, 63, 100, 103, PINK, alpha=55)  # 반투명 (보고만)
    m[85, 10] = (*PINK, 255)                   # 불투명 1px 고립점
    return m


def test_interior_digital_removes_detached_residue_keeps_bleed(tmp_path):
    src = build_psd(tmp_path / "int.psd", (W, H), [dict(name="Background", rgba=_bg()),
                                                  dict(name="paint", rgba=_interior_layer())])
    r = _run(src, tmp_path, work_type="interior", interior_style="digital")
    a, b = _layer_canvas(PSDDocument(src), "paint"), _layer_canvas(PSDDocument(r.outputs["clean_psd"]), "paint")
    assert b[77, 92, 3] == 0 and b[12, 97, 3] == 0 and b[85, 10, 3] == 0     # 제거
    assert b[61, 101, 3] == 55                                                # 보고만
    ring = (a[..., 3] > 0) & (a[..., 3] < 255)
    ring[50:, 60:] = False
    ring[:20, 80:] = False
    assert np.array_equal(a[ring], b[ring])                                   # 번짐 보존
    assert r.outputs.get("bgcheck_png")


def test_interior_watercolor_profile_is_more_conservative(tmp_path):
    src = build_psd(tmp_path / "wc.psd", (W, H), [dict(name="paint", rgba=_interior_layer())])
    r = _run(src, tmp_path, work_type="interior", interior_style="watercolor")
    b = _layer_canvas(PSDDocument(r.outputs["clean_psd"]), "paint")
    assert b[77, 92, 3] == 0            # 매우 옅고 멀리 떨어진 잔여만 제거
    assert b[12, 97, 3] == 30           # 수채화에서는 보존(보고)
    assert b[85, 10, 3] == 255          # 불투명 점도 보존(보고) — 물감 튐일 수 있음
    unc = [f for f in r.qa["findings"] if f["confidence"] == "uncertain"]
    assert len(unc) >= 2


def test_paper_fringe_reported_not_fixed(tmp_path):
    m = blank(H, W)
    disk(m, 45, 60, 25, (150, 60, 90))
    yy, xx = np.mgrid[:H, :W]
    d = np.sqrt((yy - 45) ** 2 + (xx - 60) ** 2)
    m[(d > 23) & (d <= 25)] = (248, 244, 236, 255)    # 스캔 종이색 테두리
    src = build_psd(tmp_path / "paper.psd", (W, H), [dict(name="scan", rgba=m)])
    r = _run(src, tmp_path, work_type="interior")
    assert any(f["type"] == "paper_fringe" for f in r.qa["findings"])
    assert r.summary["changed_pixels_total"] == 0


# -------------------------------------------------------- 6. 리핏
def _tile(broken: bool):
    m = blank(64, 64)
    rect(m, 0, 64, 0, 64, CREAM)
    disk(m, 32, 0, 10, PINK)        # 좌우 경계를 가로지르는 원 (양쪽 반원)
    disk(m, 32, 64, 10, PINK)
    disk(m, 0, 20, 6, BROWN)        # 상하 경계를 가로지르는 원
    disk(m, 64, 20, 6, BROWN)
    if broken:
        m[:, 56:] = (*CREAM, 255)   # 오른쪽 반원 삭제 → 좌우 리핏 깨짐
        disk(m, 32, 64, 10, CREAM)
    return m


@pytest.mark.parametrize("broken,expect", [(False, 0), (True, 1)])
def test_repeat_seam_detection(tmp_path, broken, expect):
    src = build_psd(tmp_path / "tile.psd", (64, 64), [dict(name="tile", rgba=_tile(broken), trim=False)])
    r = _run(src, tmp_path, work_type="interior", repeat="both")
    seams = [f for f in r.qa["findings"] if f["type"] == "repeat_discontinuity"]
    comp = [f for f in seams if f["scope"] == "composite"]
    lay = [f for f in seams if f["scope"] == "layer"]
    assert (len(comp) >= 1) == bool(expect) and (len(lay) >= 1) == bool(expect)
    assert all(f["evidence"]["direction"] == "horizontal" for f in seams)
    assert Path(r.outputs["repeat_png"]).exists()
    assert r.summary["changed_pixels_total"] == 0     # 리핏은 자동 수정하지 않음


def test_repeat_mode_respects_direction(tmp_path):
    src = build_psd(tmp_path / "tile.psd", (64, 64), [dict(name="tile", rgba=_tile(True), trim=False)])
    r = _run(src, tmp_path, work_type="interior", repeat="vertical")
    assert not [f for f in r.qa["findings"] if f["type"] == "repeat_discontinuity"]


# --------------------------------------------------- 7. 유형 분리
def test_apparel_and_interior_behave_differently(tmp_path):
    m = _defect_motif()
    rect(m, 60, 64, 20, 26, PINK, alpha=20)        # 옅은 잔여
    src = build_psd(tmp_path / "both.psd", (W, H), [dict(name="L", rgba=m)])
    ra = run_file(src, ProjectConfig(work_type="apparel"), tmp_path / "a")
    ri = run_file(src, ProjectConfig(work_type="interior"), tmp_path / "i")
    ta = {f["type"] for f in ra.qa["findings"] if f["applied"]}
    ti = {f["type"] for f in ri.qa["findings"] if f["applied"]}
    assert {"edge_spur", "edge_notch"} <= ta and not ({"edge_spur", "edge_notch"} & ti)
    assert "faint_residue" in ti and "faint_residue" not in ta
    assert ra.qa["summary"]["engine"] == "apparel" and ri.qa["summary"]["engine"] == "interior"


# --------------------------------------------------- 8. 예외(통과 사례)
def test_repeated_single_pixel_pattern_is_intentional(tmp_path):
    """5쪽: 동일 디자인에서 반복 사용된 1px 하이라이트는 통과."""
    m = blank(H, W)
    for cx in (15, 45, 75, 105):
        disk(m, 40, cx, 10, PINK)
        m[36, cx - 3] = (255, 255, 255, 255)    # 모든 복제본에 같은 1px 하이라이트
    src = build_psd(tmp_path / "rep.psd", (W, H), [dict(name="dots", rgba=m)])
    r = _run(src, tmp_path, work_type="apparel")
    assert r.summary["changed_pixels_total"] == 0
    assert any(f["confidence"] == "intentional" and f["type"] == "odd_pixel" for f in r.qa["findings"])


def test_approved_texture_and_protected_layers(tmp_path):
    """11쪽: 승인 질감 레이어는 내부 검사 제외(외곽 잔여는 검사). 보호 레이어는 절대 수정 안 함."""
    m = _defect_motif()
    src = build_psd(tmp_path / "p.psd", (W, H), [dict(name="texture_A", rgba=m.copy()),
                                                dict(name="logo", rgba=m.copy())])
    r = _run(src, tmp_path, work_type="apparel", approved_texture_layers=["texture_*"], protected_layers=["logo"])
    a, b = PSDDocument(src), PSDDocument(r.outputs["clean_psd"])
    assert _changed(_layer_canvas(a, "logo"), _layer_canvas(b, "logo")) == set()
    assert _changed(_layer_canvas(a, "texture_A"), _layer_canvas(b, "texture_A")) == {(70, 100)}  # 외곽 고립점만


def test_protected_region(tmp_path, defect_psd):
    r = _run(defect_psd, tmp_path, work_type="apparel",
             protected_regions=[Region(x=95, y=65, w=10, h=10, note="교수 협의 점")])
    b = _layer_canvas(PSDDocument(r.outputs["clean_psd"]), "motif")
    assert b[70, 100, 3] == 255


# --------------------------------------------------- 지원 불가 유형
def test_locked_layer_not_modified(tmp_path):
    src = build_psd(tmp_path / "lock.psd", (W, H), [dict(name="L", rgba=_defect_motif(), lock=True)])
    r = _run(src, tmp_path, work_type="apparel")
    assert r.summary["changed_pixels_total"] == 0
    assert any(f["confidence"] == "unsupported" and "잠긴" in f["skip_reason"] for f in r.qa["findings"])


def test_16bit_document_is_not_modified(tmp_path):
    src = build_psd(tmp_path / "d16.psd", (W, H), [dict(name="L", rgba=_defect_motif())], depth=16)
    r = _run(src, tmp_path, work_type="apparel")
    assert r.status == "ok"
    assert Path(r.outputs["clean_psd"]).read_bytes() == Path(src).read_bytes()
    assert any(f["confidence"] == "unsupported" for f in r.qa["findings"])


def test_grayscale_document_supported(tmp_path):
    m = _defect_motif()
    m[..., :3] = m[..., :3].mean(axis=2, keepdims=True).astype(np.uint8)
    src = build_psd(tmp_path / "g.psd", (W, H), [dict(name="L", rgba=m)], mode="L", bg=(255,))
    r = _run(src, tmp_path, work_type="apparel")
    assert r.status == "ok", r.error
    assert r.summary["applied_fixes"] >= 4 and r.summary["verification_passed"]


def test_change_limit_aborts_layer(tmp_path, defect_psd):
    r = _run(defect_psd, tmp_path, work_type="apparel", max_layer_change_ratio=0.0)
    # 한도 = max(64, 0) → 5px 변경은 허용됨. 대신 많은 변경을 만들어 확인
    S = 300                                          # 밀집하지 않게(14px 간격) 많은 1px 결함
    m = rect(blank(S, S), 0, S, 0, S, PINK)
    pts = [(y, x) for y in range(7, S - 7, 14) for x in range(7, S - 7, 14)][:200]
    for i, (y, x) in enumerate(pts):                 # 결함마다 고유 색 (전체 색 수 <= 256 유지)
        m[y, x] = (i, 255 - i, 30, 255)
    src = build_psd(tmp_path / "many.psd", (S, S), [dict(name="L", rgba=m)])
    r2 = _run(src, tmp_path, work_type="apparel", max_layer_change_ratio=0.0001)
    assert r2.summary["changed_pixels_total"] == 0
    assert any("안전 한도" in f["skip_reason"] for f in r2.qa["findings"])


# --------------------------------------------------- 배치 / 실패 격리 / 취소
def test_batch_isolates_failures(tmp_path, defect_psd):
    d = tmp_path / "batch"
    d.mkdir()
    shutil.copy(defect_psd, d / "ok.psd")
    (d / "broken.psd").write_bytes(b"8BPS" + b"\x00" * 50)
    res = run_batch([d], ProjectConfig(), tmp_path / "out")
    st = {Path(r.input_path).name: r.status for r in res}
    assert st == {"broken.psd": "failed", "ok.psd": "ok"}
    assert not (tmp_path / "out" / "broken_CLEAN.psd").exists()


def test_cancel_leaves_no_clean_file(tmp_path, defect_psd):
    ev = threading.Event()
    ev.set()
    with pytest.raises(Cancelled):
        run_file(defect_psd, ProjectConfig(), tmp_path / "out", cancel=ev)
    assert not (tmp_path / "out" / "defect_CLEAN.psd").exists()


# --------------------------------------------------- 10. 오탐 측정
def test_false_positive_rate_on_clean_designs(tmp_path):
    """깨끗한 합성 디자인(결함 없음)에서 바뀐 픽셀 비율을 측정. 데이터가 적어 정확도 %는 주장하지 않음."""
    m = blank(H, W)
    rect(m, 0, H, 0, W, CREAM)
    for cx in range(12, W, 24):
        disk(m, 20, cx, 8, PINK)
        disk(m, 60, cx, 6, BROWN)
        m[60, cx] = (255, 255, 255, 255) if cx % 48 == 12 else m[60, cx]
    m[80:82, :] = (*PINK, 255)
    src = build_psd(tmp_path / "clean.psd", (W, H), [dict(name="design", rgba=m)])
    changed = 0
    for wt in ("apparel", "interior"):
        r = run_file(src, ProjectConfig(work_type=wt, repeat="horizontal"), tmp_path / wt)
        changed += r.summary["changed_pixels_total"]
    assert changed == 0


def test_config_roundtrip(tmp_path):
    cfg = ProjectConfig(work_type="interior", repeat="both", interior_style="watercolor",
                        protected_regions=[Region(1, 2, 3, 4, ["a*"], "n")], allowed_colors=["#FFFFFF"])
    p = tmp_path / "c.json"
    cfg.save(p)
    assert ProjectConfig.load(p) == cfg
    with pytest.raises(ValueError):
        ProjectConfig.from_dict({"work_type": "furniture"})
    with pytest.raises(ValueError):
        ProjectConfig.from_dict({"allowed_colors": ["pink"]})


def test_qa_json_has_required_fields(defect_psd, tmp_path):
    r = _run(defect_psd, tmp_path)
    q = json.loads(Path(r.outputs["qa_json"]).read_text(encoding="utf-8"))
    f = q["findings"][0]
    for k in ("type", "layer_id", "bounds", "confidence", "evidence", "proposed_action", "changed_pixel_count"):
        assert k in f
    assert q["verification"]["passed"] is True


# --------------------------------------------- 5쪽 반복 판정 세부 (평가에서 발견된 문제의 회귀 테스트)
def test_identical_errors_scattered_in_flat_area_are_fixed(tmp_path):
    """단색 면 위의 똑같은 1px 구멍/점이 불규칙하게 흩어져 있으면 '의도적 반복'이 아니다."""
    m = rect(blank(H, W), 5, 85, 5, 115, PINK)
    holes = [(20, 17), (33, 61), (47, 29), (70, 96)]
    for y, x in holes:
        m[y, x] = 0
    sp = [(1, 1), (88, 50), (1, 118)]   # 몸체와 3px 이상 떨어진 점
    for y, x in sp:
        m[y, x] = (*BROWN, 255)
    src = build_psd(tmp_path / "scat.psd", (W, H), [dict(name="L", rgba=m)])
    r = _run(src, tmp_path, work_type="apparel")
    b = _layer_canvas(PSDDocument(r.outputs["clean_psd"]), "L")
    assert all(b[y, x, 3] == 255 for y, x in holes)
    assert all(b[y, x, 3] == 0 for y, x in sp)


def test_regular_grid_of_identical_pixels_is_intentional(tmp_path):
    """등간격 격자(메시·도트 패턴)의 같은 1px 은 의도적 반복으로 보존."""
    m = rect(blank(H, W), 5, 85, 5, 115, PINK)
    grid = [(y, x) for y in (20, 40, 60) for x in (20, 50, 80)]
    for y, x in grid:
        m[y, x] = 0
    src = build_psd(tmp_path / "grid.psd", (W, H), [dict(name="L", rgba=m)])
    r = _run(src, tmp_path, work_type="apparel")
    assert r.summary["changed_pixels_total"] == 0


def test_many_scattered_specks_treated_as_texture(tmp_path):
    rng = np.random.default_rng(1)
    m = blank(H, W)
    for _ in range(40):
        y, x = rng.integers(2, H - 2), rng.integers(2, W - 2)
        m[y, x] = (*BROWN, 255)
    src = build_psd(tmp_path / "tex.psd", (W, H), [dict(name="L", rgba=m)])
    r = _run(src, tmp_path, work_type="apparel")
    assert r.summary["changed_pixels_total"] == 0


def test_interior_keeps_similar_dots_more_conservatively(tmp_path):
    m = rect(blank(H, W), 30, 60, 30, 90, PINK)
    for y, x in [(5, 7), (12, 100), (80, 15), (85, 110)]:
        m[y, x] = (*BROWN, 255)        # 비슷한 점 4개 → 인테리어는 지무늬 가능성으로 보존
    src = build_psd(tmp_path / "dots.psd", (W, H), [dict(name="L", rgba=m)])
    ri = _run(src, tmp_path / "i", work_type="interior")
    ra = _run(src, tmp_path / "a", work_type="apparel")
    assert ri.summary["changed_pixels_total"] == 0
    assert ra.summary["changed_pixels_total"] == 4


# ============================== 기준 PDF 사진(2·4·6·8·10쪽) 대조 후 추가한 사례 재현 테스트
ORANGE, MAGENTA, NAVY, LBLUE = (205, 135, 90), (210, 70, 200), (40, 50, 70), (150, 185, 200)


def _diagonal_two_color(h=90, w=120, slope=0.6, c=20.0):
    """깨끗한 대각선 경계(디지털 직선): y > slope*x + c 는 MAGENTA, 나머지 ORANGE."""
    yy, xx = np.mgrid[:h, :w]
    m = blank(h, w)
    m[...] = (*ORANGE, 255)
    m[yy > slope * xx + c] = (*MAGENTA, 255)
    return m


def test_p2_clean_diagonal_edges_untouched_and_rotation_spikes_fixed(tmp_path):
    clean = _diagonal_two_color()
    dirty = clean.copy()
    spikes = []
    for x in (17, 44, 78, 101):                # 대각선 경계에 1px 톱니 (불규칙 위치 = 회전으로 깨진 경계)
        y = int(np.floor(0.6 * x + 20))        # 경계 바로 위 ORANGE 쪽에 MAGENTA 1px 가 튀어나옴
        while dirty[y, x, 0] != ORANGE[0]:
            y -= 1
        dirty[y, x] = (*MAGENTA, 255)
        spikes.append((y, x))
    a = build_psd(tmp_path / "clean.psd", (120, 90), [dict(name="L", rgba=clean)])
    b = build_psd(tmp_path / "dirty.psd", (120, 90), [dict(name="L", rgba=dirty)])
    ra = run_file(a, ProjectConfig(work_type="apparel"), tmp_path / "oa")
    assert ra.summary["changed_pixels_total"] == 0
    rb = run_file(b, ProjectConfig(work_type="apparel"), tmp_path / "ob")
    out = _layer_canvas(PSDDocument(rb.outputs["clean_psd"]), "L")
    changed = _changed(_layer_canvas(PSDDocument(b), "L"), out)
    assert changed and changed <= set(spikes) | {(y, x) for y, x in np.argwhere(np.any(dirty != clean, axis=2))}
    # 결과가 원래의 깨끗한 경계와 같아짐 (수정된 픽셀 기준)
    for y, x in changed:
        assert tuple(out[y, x]) == tuple(clean[y, x])


def test_p4_boundary_odd_pixel_on_staircase_fixed_without_new_color(tmp_path):
    m = _diagonal_two_color()
    # 계단 경계 위 1px 회색 (주황·분홍 어느 쪽 색도 아님)
    y, x = 0, 33
    while not (m[y, x, 0] == ORANGE[0] and m[y + 1, x, 0] == MAGENTA[0]):
        y += 1
    m[y, x] = (200, 200, 190, 255)
    src = build_psd(tmp_path / "p4.psd", (120, 90), [dict(name="L", rgba=m)])
    r = _run(src, tmp_path, work_type="apparel")
    out = _layer_canvas(PSDDocument(r.outputs["clean_psd"]), "L")
    assert tuple(out[y, x, :3]) in (ORANGE, MAGENTA)
    assert r.summary["changed_pixels_total"] == 1
    assert {tuple(c) for c in out[..., :3].reshape(-1, 3).tolist()} == {ORANGE, MAGENTA}


def test_p6_dense_stipple_preserved_isolated_error_fixed(tmp_path):
    """6쪽 오리: 몸통 안 점묘(의도)는 보존, 나머지 1픽셀 오류는 정리."""
    rng = np.random.default_rng(3)
    m = rect(blank(H, W), 5, 85, 5, 115, (235, 235, 235))
    stip = set()
    while len(stip) < 25:                       # 한 영역(20x20)에 몰린 점묘
        y, x = int(rng.integers(30, 50)), int(rng.integers(60, 80))
        if all(abs(y - a) > 1 or abs(x - b) > 1 for a, b in stip):
            stip.add((y, x))
    for y, x in stip:
        m[y, x] = (30, 30, 230, 255)
    m[70, 20] = (30, 30, 230, 255)              # 홀로 떨어진 1px 오류
    src = build_psd(tmp_path / "duck.psd", (W, H), [dict(name="duck", rgba=m)])
    r = _run(src, tmp_path, work_type="apparel")
    out = _layer_canvas(PSDDocument(r.outputs["clean_psd"]), "duck")
    assert all(tuple(out[y, x, :3]) == (30, 30, 230) for y, x in stip)
    assert tuple(out[70, 20, :3]) == (235, 235, 235)
    assert any(f["evidence"].get("dense_cluster") for f in r.qa["findings"])


def test_p6_color_separation_mismatch_reported(tmp_path):
    olive, navy = (110, 120, 60), (30, 40, 80)
    l1 = rect(blank(H, W), 10, 30, 10, 30, olive)
    rect(l1, 40, 50, 10, 20, navy)              # Olive 분판에 남색이 섞임
    l2 = rect(blank(H, W), 10, 30, 60, 90, navy)
    src = build_psd(tmp_path / "sep.psd", (W, H), [dict(name="Olive", rgba=l1), dict(name="Navy", rgba=l2)])
    r = _run(src, tmp_path, work_type="apparel", one_color_per_layer=True, expected_color_count=3)
    sm = [f for f in r.qa["findings"] if f["type"] == "separation_mismatch"]
    assert len(sm) == 2
    assert "#1E2850" in sm[0]["evidence"]["colors_in_multiple_layers"]
    assert any(f["type"] == "layer_multi_color" and f["layer_name"] == "Olive" for f in r.qa["findings"])


def _soft_disk(h, w, cy, cx, r, color, feather=4):
    yy, xx = np.mgrid[:h, :w]
    d = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    a = np.clip((r - d) / feather, 0, 1)
    m = blank(h, w)
    m[..., :3] = color
    m[..., 3] = (a * 255).astype(np.uint8)
    return m


def test_p8_outline_problems_reported_not_fixed(tmp_path):
    m = _soft_disk(H, W, 45, 60, 30, (150, 190, 210))
    m[20:70, 60:91][m[20:70, 60:91, 3] > 0] = (150, 190, 210, 255)   # 오른쪽 반은 딱딱한(계단) 외곽
    yy, xx = np.nonzero((m[..., 3] == 255))
    edge = [(y, x) for y, x in zip(yy, xx) if x < 45 and (m[y, x - 2, 3] < 255)][:3]
    for y, x in edge:                                                    # 외곽의 검은 도장툴 점
        m[y, x] = (20, 20, 20, 255)
    m[5:60, 10] = (90, 90, 90, 255)                                      # 세로 1px 직선 자국
    m[5:60, 9] = 0
    m[5:60, 11] = 0
    src = build_psd(tmp_path / "p8.psd", (W, H), [dict(name="leaf", rgba=m)])
    r = _run(src, tmp_path, work_type="interior", interior_style="watercolor")
    types = {f["type"] for f in r.qa["findings"]}
    assert {"hard_edge", "edge_foreign_speck", "line_artifact"} <= types
    assert r.summary["changed_pixels_total"] == 0


def _spots_tile(n=48, seed=0):
    rng = np.random.default_rng(seed)
    t = rect(blank(n, n), 0, n, 0, n, (220, 235, 210))
    for _ in range(40):
        cy, cx, r = rng.integers(0, n), rng.integers(0, n), rng.integers(1, 3)
        for oy in (-n, 0, n):                     # 타일 자체는 이음매 없게(래핑)
            for ox in (-n, 0, n):
                yy, xx = np.mgrid[:n, :n]
                t[(yy - cy - oy) ** 2 + (xx - cx - ox) ** 2 <= r * r] = (160, 180, 150, 255)
    return t


@pytest.mark.parametrize("broken", [False, True])
def test_p10_ground_texture_internal_seam(tmp_path, broken):
    t = _spots_tile()
    big = np.tile(t, (3, 3, 1))
    if broken:                                   # 오른쪽 아래 타일들을 다른 위치에서 잘라 붙임 → 내부 이음매 어긋남
        big[48:, 96:] = np.roll(np.roll(big[48:, 96:], 17, axis=0), 11, axis=1)
    src = build_psd(tmp_path / "ground.psd", (144, 144), [dict(name="지무늬", rgba=big, trim=False)])
    r = _run(src, tmp_path, work_type="interior")
    seams = [f for f in r.qa["findings"] if f["type"] == "internal_seam"]
    if broken:
        assert any(f["evidence"]["direction"] == "vertical" and abs(f["evidence"]["position"] - 96) <= 1 for f in seams)
        # 이음매에서 잘린 무늬 조각은 지우지 않음(리핏을 먼저 맞춰야 함). 검출 못 한 가로 이음매(y=48) 위 조각만 예외
        for f in r.qa["findings"]:
            if f["applied"]:
                assert abs(f["bounds"][1] - 48) <= 1, f
    else:
        assert not seams
        assert r.summary["changed_pixels_total"] == 0


def test_stripes_not_reported_as_internal_seam(tmp_path):
    m = blank(90, 120)
    for x in range(0, 120, 10):
        m[:, x:x + 5] = (*PINK, 255)
        m[:, x + 5:x + 10] = (*CREAM, 255)
    src = build_psd(tmp_path / "st.psd", (120, 90), [dict(name="stripe", rgba=m, trim=False)])
    r = _run(src, tmp_path, work_type="interior")
    assert not [f for f in r.qa["findings"] if f["type"] in ("internal_seam", "line_artifact")]



def test_tapering_tip_is_not_treated_as_spike(tmp_path):
    """실제 분판 PSD 에서 발견: 10→9→7→4→2→1 로 좁아지는 뾰족한 끝 1px 을 톱니로 오인하던 문제 (첨단 보호)."""
    m = rect(blank(H, W), 0, H, 0, W, CREAM)
    widths = [10, 9, 7, 4, 2, 1]
    for i, w in enumerate(widths):
        x0 = 50 - (w - 1) // 2 if w > 2 else 50
        m[30 + i, x0:x0 + w] = (*BROWN, 255)
    src = build_psd(tmp_path / "tip.psd", (W, H), [dict(name="L", rgba=m)])
    r = _run(src, tmp_path, work_type="apparel")
    assert r.summary["changed_pixels_total"] == 0


def test_separation_mode_moves_pixels_between_layers_without_holes(tmp_path):
    """실제 분판 PSD 에서 발견: 레이어를 따로 고치면 구멍/겹침이 생기던 문제 → 분판 모드."""
    base = np.zeros((H, W), np.int8)               # 0 = CREAM 분판, 1 = PINK 분판
    base[10:50, 10:60] = 1
    base[30, 60] = 1                               # 곧은 경계 1px 돌출 (PINK → CREAM 분판으로 옮겨야 함)
    base[70, 100] = 1                              # 고립 점
    layers = []
    for k, col in ((0, CREAM), (1, PINK)):
        a = blank(H, W)
        a[base == k] = (*col, 255)
        layers.append(dict(name=["Ivory", "Pink"][k], rgba=a))
    src = build_psd(tmp_path / "sep.psd", (W, H), layers)
    r = _run(src, tmp_path, work_type="apparel")
    assert r.summary["separation_mode"] is True
    v = r.qa["verification"]["checks"]
    assert v["separation_integrity"] is True and v["separation_holes_before_after"] == "0 -> 0"
    d = PSDDocument(r.outputs["clean_psd"])
    ivory, pink = _layer_canvas(d, "Ivory"), _layer_canvas(d, "Pink")
    for y, x in ((30, 60), (70, 100)):
        assert pink[y, x, 3] == 0 and ivory[y, x, 3] == 255 and tuple(ivory[y, x, :3]) == CREAM
    assert r.summary["changed_pixels_total"] == 2



def test_fix_repeats_option_fixes_copied_pixels_but_keeps_dot_patterns(tmp_path):
    """실제 분판 PSD 에서 발견: 모티브 복제본마다 똑같이 복사된 1px 실수. 사용자가 옵션을 켜면 고침.
    단, 반복 점무늬(점선)와 밀집 영역은 여전히 보존."""
    m = blank(H, W)
    for cx in (15, 45, 75, 105):
        disk(m, 40, cx, 10, PINK)
        m[36, cx - 3] = (255, 255, 255, 255)        # 복제본마다 같은 1px
    for x in range(10, 110, 6):                     # 점선
        m[80, x] = (*BROWN, 255)
    src = build_psd(tmp_path / "rep.psd", (W, H), [dict(name="dots", rgba=m)])
    r0 = run_file(src, ProjectConfig(work_type="apparel"), tmp_path / "a")
    assert r0.summary["changed_pixels_total"] == 0
    r1 = run_file(src, ProjectConfig(work_type="apparel", fix_repeats=True), tmp_path / "b")
    out = _layer_canvas(PSDDocument(r1.outputs["clean_psd"]), "dots")
    assert all(tuple(out[36, cx - 3, :3]) == PINK for cx in (15, 45, 75, 105))
    assert all(out[80, x, 3] == 255 for x in range(10, 110, 6))
    assert r1.summary["changed_pixels_total"] == 4


def _scan_on_paper(seed=0):
    """스캔 수채화 흉내: 종이(질감 노이즈) 위 부드러운 꽃 + 가는 줄기 2개(위에서 만남) + 꽃 안쪽 흰 부분."""
    rng = np.random.default_rng(seed)
    S = 160
    paper = np.array([246, 243, 236], float)
    img = np.tile(paper, (S, S, 1)) + rng.normal(0, 3, (S, S, 3))
    yy, xx = np.mgrid[:S, :S]
    d = np.sqrt((yy - 110) ** 2 + (xx - 80) ** 2)
    petal = np.clip((30 - d) / 4, 0, 1)                         # 부드러운 꽃 외곽 (수채 번짐)
    ring = (d > 8) & (d < 30)                                   # 가운데 흰 부분(꽃잎 안쪽, 지름 16)은 종이색 그대로
    pig = np.array([150, 110, 200], float)
    img = np.where((ring | (d >= 30))[..., None], img * (1 - petal[..., None]) + pig * petal[..., None], img)
    for x0 in (60, 100):                                        # 위에서 만나는 두 줄기 → 사이에 길쭉한 종이 띠 갇힘
        for y in range(20, 85):
            x = int(x0 + (80 - x0) * (85 - y) / 65)
            img[y, x - 1:x + 2] = (70, 110, 60)
    img[18:22, 78:83] = (70, 110, 60)
    a = np.zeros((S, S), np.uint8)
    a[6:154, 6:154] = 255                                       # 종이 조각(레이어 경계 안쪽)
    rgba = np.dstack([np.clip(img, 0, 255), a]).astype(np.uint8)
    return rgba, paper, d


def test_remove_paper_keeps_motif_soft_edges_and_inner_white(tmp_path):
    rgba, paper, d = _scan_on_paper()
    bg = rect(blank(160, 160), 0, 160, 0, 160, (5, 0, 254))
    src = build_psd(tmp_path / "scan.psd", (160, 160), [dict(name="bg", rgba=bg), dict(name="motif", rgba=rgba)])
    r0 = _run(src, tmp_path / "a", work_type="interior", interior_style="watercolor")
    assert r0.summary["changed_pixels_total"] == 0 or not any(
        f["type"] == "paper_removed" for f in r0.qa["findings"])                       # 기본은 지우지 않음
    r = _run(src, tmp_path / "b", work_type="interior", interior_style="watercolor", remove_paper=True)
    assert r.status == "ok" and r.summary["verification_passed"]
    out = _layer_canvas(PSDDocument(r.outputs["clean_psd"]), "motif")
    assert out[15, 20, 3] == 0 and out[140, 145, 3] == 0                             # 바깥 종이 제거
    assert out[50, 80, 3] == 0                                                        # 줄기 사이 갇힌 종이 띠 제거
    assert out[110, 80, 3] == 255                                                     # 꽃잎 안쪽 흰 부분 유지
    assert out[110, 100, 3] == 255                                                    # 꽃 몸체 유지
    stem = (rgba[..., 0] == 70) & (rgba[..., 1] == 110)                                # 떨어진 가는 줄기 픽셀
    assert (out[..., 3][stem] == 255).mean() > 0.95                                   # 가는 줄기 유지
    edge = (d > 27) & (d < 31)
    ea = out[..., 3][edge]
    assert ((ea > 0) & (ea < 255)).mean() > 0.3                                      # 외곽은 부드러운 반투명
    # 파란 바탕 위에서 외곽에 흰 종이 테두리(헤일로)가 없어야 함: 외곽 합성색이 종이보다 꽃/바탕 쪽
    al = out[..., 3:4] / 255.0
    comp = out[..., :3] * al + np.array([5, 0, 254]) * (1 - al)
    assert comp[edge].mean(axis=0)[0] < 200
    assert sha256_file(src) == r.qa["input_sha256_before"]



def test_default_outputs_only_the_psd(tmp_path, defect_psd):
    """사용자 요청: 기본은 결과 PSD 하나만 생성."""
    r = run_file(defect_psd, ProjectConfig(), tmp_path / "only")
    files = sorted(p.name for p in (tmp_path / "only").iterdir())
    assert files == ["defect_CLEAN.psd"], files
    assert r.status == "ok" and r.summary["verification_passed"]
