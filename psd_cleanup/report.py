"""보고서/비교 이미지 생성 (로컬 파일, 외부 리소스 없음)."""
from __future__ import annotations

import html
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from .config import REPEAT_LABELS, STYLE_LABELS, TYPE_LABELS
from .findings import CERTAIN, CONF_LABELS, INFO, INTENTIONAL, TYPE_LABELS as FT_LABELS, UNCERTAIN, UNSUPPORTED


def diff_image(merged: np.ndarray, changed: np.ndarray, max_side: int = 4000) -> Image.Image:
    """원본 합성 이미지를 옅게, 변경 픽셀은 빨강, 주변은 원형 표시."""
    base = merged.astype(np.float32)
    gray = base.mean(axis=2, keepdims=True)
    img = (gray * 0.35 + 255 * 0.65).repeat(3, axis=2).astype(np.uint8)
    img[changed] = (255, 0, 0)
    im = Image.fromarray(img)
    dr = ImageDraw.Draw(im)
    lab, n = ndimage.label(ndimage.binary_dilation(changed, iterations=4))
    for sl in ndimage.find_objects(lab)[:2000]:
        cy, cx = (sl[0].start + sl[0].stop) / 2, (sl[1].start + sl[1].stop) / 2
        r = max(10, (sl[0].stop - sl[0].start), (sl[1].stop - sl[1].start))
        dr.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 0, 0), width=2)
    s = min(1.0, max_side / max(im.size))
    if s < 1:
        im = im.resize((int(im.width * s), int(im.height * s)), Image.LANCZOS)
    return im


def fixes_sheet(before: np.ndarray, after: np.ndarray, changed: np.ndarray, max_items: int = 30,
                crop: int = 12, zoom: int = 10) -> Image.Image:
    """수정 지점별 [수정 전 | 수정 후 | 변경 표시] 확대 비교 (최근접 확대, 보간 없음)."""
    lab, n = ndimage.label(ndimage.binary_dilation(changed, iterations=2))
    objs = ndimage.find_objects(lab)[:max_items]
    cell = (2 * crop + 1) * zoom
    if not objs:
        im = Image.new("RGB", (cell * 3 + 40, 60), (255, 255, 255))
        ImageDraw.Draw(im).text((10, 20), "no pixel changes", fill=(0, 0, 0))
        return im
    H, W = changed.shape
    sheet = Image.new("RGB", (cell * 3 + 40, len(objs) * (cell + 24)), (255, 255, 255))
    dr = ImageDraw.Draw(sheet)
    for i, sl in enumerate(objs):
        cy, cx = (sl[0].start + sl[0].stop) // 2, (sl[1].start + sl[1].stop) // 2
        y0, x0 = max(0, cy - crop), max(0, cx - crop)
        y1, x1 = min(H, cy + crop + 1), min(W, cx + crop + 1)
        b = before[y0:y1, x0:x1]
        a = after[y0:y1, x0:x1]
        m = changed[y0:y1, x0:x1]
        mark = a.copy()
        mark[m] = (255, 0, 0)
        ty = i * (cell + 24)
        dr.text((4, ty + 4), f"#{i + 1}  x={x0}..{x1 - 1}  y={y0}..{y1 - 1}   before | after | changed", fill=(0, 0, 0))
        for j, arr in enumerate((b, a, mark)):
            tile = Image.fromarray(np.ascontiguousarray(arr)).resize(
                (arr.shape[1] * zoom, arr.shape[0] * zoom), Image.NEAREST)
            sheet.paste(tile, (j * (cell + 20), ty + 20))
    return sheet


def background_check(doc, layer_rgba: dict, W: int, H: int, max_side: int = 1600) -> Image.Image | None:
    """1쪽: '다른 배경색을 깔면 지저분한 지우개 흔적' 확인용.
    알파 채널이 있는 '보이는' 픽셀 레이어만 단순 Normal 합성(근사)해 검정/흰색/채도 높은 배경 위에 표시."""
    acc_rgb = np.zeros((H, W, 3), np.float32)
    acc_a = np.zeros((H, W, 1), np.float32)
    used = 0
    for idx in sorted(layer_rgba):
        l = doc.layer_by_index(idx)
        if not l.has_alpha_channel or not l.effective_visible:
            continue
        raw = layer_rgba[idx]
        if (l.bbox[0] <= 0 and l.bbox[1] <= 0 and l.bbox[2] >= W and l.bbox[3] >= H
                and (raw[..., 3] == 255).all()):
            continue   # 캔버스 전체를 덮는 불투명 바탕(배경) 레이어는 제외해야 다른 배경색 위 검사가 됨
        rgba = raw.astype(np.float32) / 255.0
        x0, y0 = l.bbox[0], l.bbox[1]
        cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(W, l.bbox[2]), min(H, l.bbox[3])
        if cx1 <= cx0 or cy1 <= cy0:
            continue
        src = rgba[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
        a = src[..., 3:4] * (l.opacity / 255.0)
        acc_rgb[cy0:cy1, cx0:cx1] = src[..., :3] * a + acc_rgb[cy0:cy1, cx0:cx1] * (1 - a)
        acc_a[cy0:cy1, cx0:cx1] = a + acc_a[cy0:cy1, cx0:cx1] * (1 - a)
        used += 1
    if used == 0:
        return None
    panels = []
    for bg in ((0, 0, 0), (255, 255, 255), (0, 160, 255)):
        bgc = np.array(bg, np.float32) / 255.0
        out = acc_rgb + bgc * (1 - acc_a)
        panels.append((np.clip(out, 0, 1) * 255).astype(np.uint8))
    im = Image.fromarray(np.concatenate(panels, axis=1))
    s = min(1.0, max_side * 3 / im.width)
    if s < 1:
        im = im.resize((int(im.width * s), int(im.height * s)), Image.LANCZOS)
    return im


_CONF_CLASS = {CERTAIN: "c", UNCERTAIN: "u", INTENTIONAL: "i", UNSUPPORTED: "x", INFO: "n"}


def write_html(qa: dict, paths: dict[str, Path], outputs: dict[str, str]) -> None:
    e = html.escape
    cfg = qa["config"]
    s = qa["summary"]
    v = qa["verification"]
    doc = qa["document"]

    def rel(key):
        p = outputs.get(key)
        return Path(p).name if p else None

    rows = []
    order = {CERTAIN: 0, UNCERTAIN: 1, UNSUPPORTED: 2, INTENTIONAL: 3, INFO: 4}
    fs = sorted(qa["findings"], key=lambda f: (order[f["confidence"]], f["layer_index"] or -1, f["type"]))
    for f in fs[:3000]:
        status = "수정됨" if f["applied"] else ("수정 안 함: " + f["skip_reason"] if f["skip_reason"] else "수정 안 함")
        ev = ", ".join(f"{k}={v}" for k, v in f["evidence"].items())
        rows.append(
            f"<tr class='{_CONF_CLASS[f['confidence']]}'><td>{e(f['id'])}</td><td>{e(f['layer_name'] or '(합성 결과)')}</td>"
            f"<td>{e(f['type_label'])}</td><td>{e(f['confidence_label'])}</td><td>{e(str(f['bounds']))}</td>"
            f"<td>{e(f['proposed_action'])}</td><td>{e(status)}</td><td>{f['changed_pixel_count']}</td>"
            f"<td>{e(f['pdf_ref'])}</td><td class='ev'>{e(ev[:300])}</td></tr>")
    more = f"<p>※ 항목이 많아 3000개만 표시 (전체는 QA.json)</p>" if len(fs) > 3000 else ""
    lrows = []
    for l in qa["layers"]:
        lrows.append(f"<tr><td>{l['index']}</td><td>{e(l['path'])}</td><td>{e(l['kind'])}</td>"
                     f"<td>{'보임' if l['visible'] else '숨김'}</td><td>{l['opacity']}</td><td>{e(l['blend_mode'])}</td>"
                     f"<td>{e(str(l['bbox']))}</td><td>{'가능' if l['editable'] else '불가: ' + e('; '.join(l['unsupported_reasons']))}</td></tr>")
    imgs = []
    for key, title in (("diff_png", "수정 위치 (DIFF)"), ("fixes_png", "수정 전/후 확대"),
                       ("repeat_png", "리핏 연결 미리보기 (빨간 테두리 = 불연속 후보)"),
                       ("bgcheck_png", "배경색 대비 검사 (검정 / 흰색 / 파랑) — 위: 원본, 아래: 클린업 결과")):
        if rel(key):
            imgs.append(f"<h3>{title}</h3><a href='{e(rel(key))}'><img src='{e(rel(key))}'></a>")
    bt = "".join(f"<tr><td>{e(FT_LABELS.get(t, t))}</td>" + "".join(
        f"<td>{d.get(c, 0)}</td>" for c in (CERTAIN, UNCERTAIN, INTENTIONAL, UNSUPPORTED, INFO)) + "</tr>"
        for t, d in sorted(s["findings_by_type"].items()))
    vrows = "".join(f"<li>{e(k)}: <b>{e(str(val))}</b></li>" for k, val in v["checks"].items())
    vfail = "".join(f"<li class='bad'>{e(x)}</li>" for x in v["failures"])
    vwarn = "".join(f"<li>{e(x)}</li>" for x in v["warnings"])
    style_line = (f" / 표현 방식: {STYLE_LABELS[cfg['interior_style']]}" if cfg["work_type"] == "interior" else "")
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>PSD 클린업 QA</title>
<style>
body{{font-family:system-ui,-apple-system,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;margin:16px;color:#222;background:#fff}}
table{{border-collapse:collapse;font-size:12px;width:100%}} td,th{{border:1px solid #ddd;padding:3px 5px;vertical-align:top}}
th{{background:#f4f4f4;position:sticky;top:0}} tr.c td{{background:#eef9ee}} tr.u td{{background:#fff7e0}}
tr.i td{{background:#eef3ff}} tr.x td{{background:#f3f3f3}} .ev{{color:#666;max-width:380px;word-break:break-all}}
img{{max-width:100%;border:1px solid #ccc}} .bad{{color:#b00}} .box{{border:1px solid #ccc;padding:10px;border-radius:6px;margin:10px 0}}
.warn{{background:#fff3f3;border-color:#e99}}
</style></head><body>
<h1>PSD 클린업 QA — {e(Path(qa['input']).name)}</h1>
<div class="box warn">{e(qa['disclaimer'])}</div>
<div class="box"><b>유형</b>: {TYPE_LABELS[cfg['work_type']]} 엔진 / 리핏: {REPEAT_LABELS[cfg['repeat']]}{e(style_line)}
<br><b>문서</b>: {doc['width']}×{doc['height']}px, {e(doc['color_mode_name'])} {doc['depth']}bit, {'PSB' if doc['version'] == 2 else 'PSD'},
ICC {'있음' if doc['has_icc'] else '없음'}, 레이어 {doc['layer_count']}개
<br><b>자동 수정</b>: {s['applied_fixes']}건, 변경 픽셀 {s['changed_pixels_total']}px, 수정된 레이어: {e(', '.join(s['patched_layers']) or '없음')}
<br><b>저장 후 검증</b>: {'<b style="color:green">통과</b>' if v['passed'] else '<b class="bad">실패</b>'}
<br><b>결과 PSD</b>: {e(Path(qa['outputs']['clean_psd']).name)} (원본 SHA-256 {qa['input_sha256_before'][:16]}… 변경 없음 확인)</div>
<h2>유형별 집계</h2><table><tr><th>유형</th><th>확실</th><th>불확실</th><th>의도적</th><th>지원 불가</th><th>정보</th></tr>{bt}</table>
<h2>검증</h2><ul>{vrows}</ul><ul>{vfail}</ul><ul>{vwarn}</ul>
<p>미검증: {e('; '.join(v.get('not_verified_here', [])))}</p>
{''.join(imgs)}
<h2>레이어 목록</h2><table><tr><th>#</th><th>경로</th><th>종류</th><th>가시성</th><th>불투명도</th><th>블렌드</th><th>경계</th><th>자동 수정</th></tr>{''.join(lrows)}</table>
<h2>검출 항목</h2>{more}
<table><tr><th>ID</th><th>레이어</th><th>유형</th><th>신뢰도</th><th>위치 (x0,y0,x1,y1)</th><th>제안</th><th>결과</th><th>변경 px</th><th>기준</th><th>근거</th></tr>
{''.join(rows)}</table></body></html>"""
    paths["qa_html"].write_text(page, encoding="utf-8")
