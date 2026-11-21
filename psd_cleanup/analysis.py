"""픽셀 단위 탐지기 (규칙 기반, 설명 가능).

모든 함수는 레이어 로컬 좌표 RGBA(uint8, HxWx4)를 받아 '후보'를 돌려준다.
신뢰도 판정과 수정 여부는 엔진(apparel/interior)이 결정한다.
어떤 탐지기도 블러·리샘플링·전역 침식/팽창으로 이미지를 '변경'하지 않는다
(형태학 연산은 판정용 마스크 계산에만 사용).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np
from scipy import ndimage

EIGHT = np.ones((3, 3), dtype=bool)
NB8 = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
OOB = -1  # 범위 밖(알 수 없음)


# --------------------------------------------------------------------- utils
def pixel_keys(rgba: np.ndarray) -> np.ndarray:
    """RGBA → int64 키. 완전 투명은 색과 무관하게 0."""
    r, g, b, a = (rgba[..., i].astype(np.int64) for i in range(4))
    k = (r << 24) | (g << 16) | (b << 8) | a
    return np.where(a == 0, 0, k)


def key_to_rgba(k: int) -> tuple[int, int, int, int]:
    if k <= 0:
        return (0, 0, 0, 0)
    return ((k >> 24) & 255, (k >> 16) & 255, (k >> 8) & 255, k & 255)


def padded(arr: np.ndarray, pad: int, fill) -> np.ndarray:
    return np.pad(arr, pad, mode="constant", constant_values=fill)


def shifted(P: np.ndarray, pad: int, dy: int, dx: int, H: int, W: int) -> np.ndarray:
    """P 는 pad 된 배열. out[y,x] = 원본[y+dy, x+dx]."""
    return P[pad + dy: pad + dy + H, pad + dx: pad + dx + W]


def patch_signature(K: np.ndarray, y: int, x: int, r: int, oob_fill: int) -> bytes:
    H, W = K.shape
    y0, y1, x0, x1 = y - r, y + r + 1, x - r, x + r + 1
    if y0 >= 0 and x0 >= 0 and y1 <= H and x1 <= W:
        return K[y0:y1, x0:x1].tobytes()
    P = np.full((2 * r + 1, 2 * r + 1), oob_fill, dtype=np.int64)
    sy0, sx0 = max(y0, 0), max(x0, 0)
    sy1, sx1 = min(y1, H), min(x1, W)
    P[sy0 - y0: sy1 - y0, sx0 - x0: sx1 - x0] = K[sy0:sy1, sx0:sx1]
    return P.tobytes()


def lattice_members(points: list[tuple[int, int]], max_n: int = 1500) -> list[bool]:
    """같은 서명의 점들 중 '정확히 등간격으로 3개 이상 나란한' (a, a+v, a+2v) 점 표시.
    스텝 앤 리피트로 배치한 반복 모티브/점선/도트 격자는 이 조건을 만족하고, 무작위로 흩어진 실수는 거의 만족하지 않는다."""
    n = len(points)
    if n > max_n:
        return [True] * n
    S = set(points)
    mem = [False] * n
    idx = {p: i for i, p in enumerate(points)}
    for i in range(n):
        yi, xi = points[i]
        for j in range(i + 1, n):
            yj, xj = points[j]
            vy, vx = yj - yi, xj - xi
            for third in ((yj + vy, xj + vx), (yi - vy, xi - vx)):
                if third in S:
                    mem[i] = mem[j] = True
                    mem[idx[third]] = True
    return mem


def repetition_intentional(points: list[tuple[int, int]], sigs: list, structured: list[bool],
                           repeat_min: int, texture_min: int = 20, reasons: list | None = None
                           ) -> tuple[list[bool], list[int]]:
    """5쪽 '의도적으로 반복 사용한 1픽셀' 판정.
    - 같은 서명이 repeat_min 회 미만 → 의도적 아님
    - 주변(패치)에 모티브 구조가 있고 똑같이 반복 → 모티브 복제본의 일부 → 의도적
    - 주변이 단순(단색 면/빈 공간)하면 '서명이 같다'는 것만으로는 근거가 약함
        → 정확한 등간격 배열(격자/점선)이거나, texture_min 회 이상(질감)일 때만 의도적"""
    groups: dict = {}
    for i, sg in enumerate(sigs):
        groups.setdefault(sg, []).append(i)
    out = [False] * len(sigs)
    counts = [0] * len(sigs)
    why = [None] * len(sigs)
    for sg, ids in groups.items():
        for i in ids:
            counts[i] = len(ids)
        if len(ids) < repeat_min:
            continue
        if len(ids) >= texture_min:
            for i in ids:
                out[i], why[i] = True, "texture"
            continue
        lat = lattice_members([points[i] for i in ids])
        for k, i in enumerate(ids):
            out[i] = structured[i] or lat[k]
            why[i] = "lattice" if lat[k] else ("structured" if structured[i] else None)
    if reasons is not None:
        reasons[:] = why
    return out, counts


def repeated_mask(K: np.ndarray, ys: np.ndarray, xs: np.ndarray, r: int, min_count: int,
                  oob_fill: int) -> tuple[np.ndarray, list[int]]:
    """같은 (2r+1)^2 주변 패턴의 반복 여부 (repetition_intentional 규칙 적용)."""
    sigs, structured = [], []
    for y, x in zip(ys, xs):
        sg = patch_signature(K, int(y), int(x), r, oob_fill)
        sigs.append(sg)
        patch = np.frombuffer(sg, dtype=np.int64).copy()
        patch[len(patch) // 2] = patch[0]
        structured.append(len(np.unique(patch)) >= 2)   # 주변에 경계/모티브가 걸치면 '구조 있음'
    flags, counts = repetition_intentional([(int(y), int(x)) for y, x in zip(ys, xs)], sigs, structured,
                                           min_count)
    return np.array(flags, dtype=bool), counts


# ------------------------------------------------------------ 1. 고립 점
@dataclass
class Component:
    label: int
    ys: np.ndarray
    xs: np.ndarray
    bbox: tuple[int, int, int, int]   # x0,y0,x1,y1 local
    area: int
    max_alpha: int
    mean_rgb: tuple[int, int, int]
    gap: int                          # 다른 픽셀까지 사이의 투명 픽셀 수 (>= gap_cap 이면 gap_cap)
    similar_count: int = 1            # 거친 유사(크기·모양·색 대역) 개수 — 인테리어용 보수적 기준
    exact_repeat_intentional: bool = False  # 정확히 같은 점의 등간격 배열/질감 — 어패럴용 기준
    repeat_reason: str | None = None        # lattice / texture


def small_components(rgba: np.ndarray, max_area: int, gap_cap: int = 8,
                     alpha_max: int | None = None) -> list[Component]:
    """alpha>0 연결 성분(8연결) 중 면적 <= max_area 인 것.
    alpha_max 가 주어지면 '최대 alpha <= alpha_max' 인 성분만 (반투명 잔여 탐지용)."""
    a = rgba[..., 3]
    mask = a > 0
    lab, n = ndimage.label(mask, structure=EIGHT)
    if n == 0:
        return []
    areas = np.bincount(lab.ravel(), minlength=n + 1)
    if alpha_max is not None:
        maxa = ndimage.maximum(a, lab, index=np.arange(n + 1))
        maxa = np.asarray(maxa)
        cand = np.where((areas <= max_area) & (maxa <= alpha_max))[0]
    else:
        cand = np.where(areas <= max_area)[0]
    cand = cand[cand > 0]
    if len(cand) == 0:
        return []
    slices = ndimage.find_objects(lab)
    H, W = a.shape
    out = []
    for L in cand:
        sl = slices[L - 1]
        y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
        sub = lab[sl] == L
        ys, xs = np.nonzero(sub)
        ys, xs = ys + y0, xs + x0
        # 간격: 창 내 다른 성분까지의 체비셰프 거리 - 1
        wy0, wy1 = max(0, y0 - gap_cap - 1), min(H, y1 + gap_cap + 1)
        wx0, wx1 = max(0, x0 - gap_cap - 1), min(W, x1 + gap_cap + 1)
        win_lab = lab[wy0:wy1, wx0:wx1]
        other = (win_lab > 0) & (win_lab != L)
        if other.any():
            oy, ox = np.nonzero(other)
            oy, ox = oy + wy0, ox + wx0
            d = np.min(np.maximum(np.abs(oy[:, None] - ys[None, :]), np.abs(ox[:, None] - xs[None, :])))
            gap = int(d) - 1
        else:
            gap = gap_cap
        px = rgba[ys, xs]
        out.append(Component(
            label=int(L), ys=ys, xs=xs, bbox=(int(x0), int(y0), int(x1), int(y1)), area=int(len(ys)),
            max_alpha=int(px[:, 3].max()), mean_rgb=tuple(int(v) for v in px[:, :3].mean(axis=0)),
            gap=min(gap, gap_cap)))
    # 유사 성분 개수 (거친 서명: 크기·모양·색 대역·alpha 대역)
    def sig(c: Component):
        return (c.area, c.bbox[2] - c.bbox[0], c.bbox[3] - c.bbox[1],
                tuple(v // 32 for v in c.mean_rgb), c.max_alpha // 64)
    cnt = Counter(sig(c) for c in out)
    for c in out:
        c.similar_count = cnt[sig(c)]

    def exact(c: Component):
        sub = rgba[c.bbox[1]:c.bbox[3], c.bbox[0]:c.bbox[2]].copy()
        sub[sub[..., 3] == 0] = 0
        return (c.bbox[2] - c.bbox[0], c.bbox[3] - c.bbox[1], sub.tobytes())
    why: list = []
    flags, _ = repetition_intentional([(c.bbox[1], c.bbox[0]) for c in out], [exact(c) for c in out],
                                      [False] * len(out), repeat_min=3, reasons=why)
    for c, f, w in zip(out, flags, why):
        c.exact_repeat_intentional = f
        c.repeat_reason = w
    return out


# --------------------------------------------- 2. 1픽셀 잘못 칠함 / 1픽셀 구멍
@dataclass
class PixelCandidate:
    y: int
    x: int
    kind: str                 # odd_pixel / pinhole
    old: tuple[int, int, int, int]
    new: tuple[int, int, int, int]
    repeat_count: int
    intentional: bool


def single_pixel_anomalies(rgba: np.ndarray, repeat_min: int, has_alpha: bool,
                           patch_r: int = 3) -> list[PixelCandidate]:
    """8이웃이 모두 같은 '불투명' 색인데 가운데 1픽셀만 다른 경우.
    - 가운데가 다른 색(또는 반투명) → odd_pixel
    - 가운데가 완전 투명 → pinhole
    선(1px 라인)은 같은 색 이웃이 있으므로 해당하지 않는다."""
    K = pixel_keys(rgba)
    H, W = K.shape
    if H < 3 or W < 3:
        return []
    oob = 0 if has_alpha else OOB
    P = padded(K, 1, oob)
    n0 = shifted(P, 1, *NB8[0], H, W)
    same = np.ones((H, W), dtype=bool)
    for dy, dx in NB8[1:]:
        same &= shifted(P, 1, dy, dx, H, W) == n0
    a0 = (n0 & 255)
    cand = same & (n0 > 0) & (a0 == 255) & (K != n0)
    ys, xs = np.nonzero(cand)
    if len(ys) == 0:
        return []
    rep, counts = repeated_mask(K, ys, xs, patch_r, repeat_min, oob)
    out = []
    for i, (y, x) in enumerate(zip(ys, xs)):
        k = int(K[y, x])
        out.append(PixelCandidate(int(y), int(x), "pinhole" if k == 0 else "odd_pixel",
                                  key_to_rgba(k), key_to_rgba(int(n0[y, x])), counts[i], bool(rep[i])))
    return out


# ------------------------------------------------ 3. 경계 1픽셀 돌출/패임 (2쪽)
@dataclass
class EdgeCandidate:
    y: int
    x: int
    kind: str                 # edge_spur / edge_notch
    direction: str
    old: tuple[int, int, int, int]
    new: tuple[int, int, int, int]
    repeat_count: int
    intentional: bool
    apex: bool = False        # 볼록 곡선(원·타원 등)의 정점 가운데 1px → 정상 계단일 수 있음
    plateau: tuple[int, int] = (0, 0)


def _at(K, y, x, oob):
    H, W = K.shape
    return int(K[y, x]) if 0 <= y < H and 0 <= x < W else oob


def _plateau(K, y, x, dy, dx, ey, ex, I, O, oob, maxlen=64):
    """P 에서 경계 방향(±e)으로 '안쪽줄=I, 바깥줄=O' 가 이어지는 길이와,
    끝에서 몸체가 곡선처럼 넓어지는지(안쪽줄이 끝나고 그 한 줄 더 안쪽은 계속) 여부."""
    res = []
    for sg in (1, -1):
        k = 1
        while k <= maxlen:
            py, px = y + sg * k * ey, x + sg * k * ex
            if _at(K, py + dy, px + dx, oob) == I and _at(K, py, px, oob) == O:
                k += 1
                continue
            break
        py, px = y + sg * k * ey, x + sg * k * ex
        shoulder = _at(K, py + dy, px + dx, oob) == O and _at(K, py + 2 * dy, px + 2 * dx, oob) == I
        res.append((k - 1, shoulder and k <= maxlen))
    return res


_DIRS = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1)}


def edge_bumps(rgba: np.ndarray, repeat_min: int, has_alpha: bool, patch_r: int = 3,
               straight_half: int = 2) -> list[EdgeCandidate]:
    """'곧은 경계(폭 >= 2*straight_half+1)' 위에 1픽셀만 튀어나온/패인 경우.

    d = 몸체(안쪽) 방향, e = 경계에 평행한 방향. P=후보 픽셀, I=안쪽 색, O=바깥 색.
      P == I, I != O
      P 의 바깥줄(P±e, ..., P±straight_half*e) 전부 O, P 바깥쪽(P-d, P-d±e) 전부 O
      안쪽줄(P+d ±0..straight_half*e) 전부 I, 그 안쪽(P+2d, P+2d±e) 전부 I
    → 선 끝(1~3px 폭 라인의 끝), 뾰족한 모서리, 원형 끝은 '곧은 경계' 조건을 만족하지 않아 제외.
    수정: P := O (바깥 색, 이미 이웃에 존재하는 색 → 새 색 생성 없음)."""
    K = pixel_keys(rgba)
    H, W = K.shape
    oob = 0 if has_alpha else OOB
    pad = straight_half + 3
    if H < 3 or W < 3:
        return []
    Pd = padded(K, pad, oob)
    out: list[EdgeCandidate] = []
    for name, (dy, dx) in _DIRS.items():
        ey, ex = dx, dy  # 평행 방향
        S = lambda oy, ox: shifted(Pd, pad, oy, ox, H, W)  # noqa: E731
        I = S(dy, dx)
        O = S(-dy, -dx)
        cond = (K == I) & (I != O) & (I != OOB) & (O != OOB)
        for s in range(1, straight_half + 1):
            for sg in (1, -1):
                cond &= S(sg * s * ey, sg * s * ex) == O                       # 바깥 줄
                cond &= S(dy + sg * s * ey, dx + sg * s * ex) == I             # 안쪽 줄
        for sg in (1, -1):
            cond &= S(-dy + sg * ey, -dx + sg * ex) == O                       # 한 칸 더 바깥
            cond &= S(2 * dy + sg * ey, 2 * dx + sg * ex) == I                 # 한 칸 더 안쪽
        cond &= S(2 * dy, 2 * dx) == I
        ys, xs = np.nonzero(cond)
        if len(ys) == 0:
            continue
        rep, counts = repeated_mask(K, ys, xs, patch_r, repeat_min, oob)
        for i, (y, x) in enumerate(zip(ys, xs)):
            ik, ok = int(I[y, x]), int(O[y, x])
            kind = "edge_notch" if ik == 0 else "edge_spur"
            (a, sa), (b, sb) = _plateau(K, int(y), int(x), dy, dx, ey, ex, ik, ok, oob)
            apex = sa and sb and abs(a - b) <= 1
            out.append(EdgeCandidate(int(y), int(x), kind, name, key_to_rgba(ik), key_to_rgba(ok),
                                     counts[i], bool(rep[i]), apex, (a, b)))
    return out


# ------------------------------------------------------ 4. 허용 색 / 도수
def color_stats(rgb_or_rgba: np.ndarray, opaque_only: bool = True, top: int = 64):
    arr = rgb_or_rgba
    if arr.shape[2] == 4:
        m = arr[..., 3] == 255 if opaque_only else arr[..., 3] > 0
        px = arr[..., :3][m]
    else:
        px = arr.reshape(-1, 3)
    if len(px) == 0:
        return 0, [], 0
    keys = (px[:, 0].astype(np.int64) << 16) | (px[:, 1].astype(np.int64) << 8) | px[:, 2]
    uniq, cnt = np.unique(keys, return_counts=True)
    order = np.argsort(-cnt)
    tops = [((int(uniq[i]) >> 16) & 255, (int(uniq[i]) >> 8) & 255, int(uniq[i]) & 255, int(cnt[i]))
            for i in order[:top]]
    return int(len(uniq)), tops, int(len(px))


@dataclass
class OffPaletteGroup:
    ys: np.ndarray
    xs: np.ndarray
    new_rgb: np.ndarray | None   # (N,3) 모두 확실할 때만
    certain: bool
    colors: list[tuple[int, int, int]]
    bbox: tuple[int, int, int, int]


def off_palette_groups(rgba: np.ndarray, palette: np.ndarray, tol: int = 0) -> list[OffPaletteGroup]:
    """불투명 픽셀 중 허용 색 목록 밖 픽셀을 연결 성분으로 묶음.
    성분의 모든 픽셀이 '서로 다른 2개 이상 허용색 이웃의 RGB 범위 안'(=경계 혼합색)일 때만 certain.
    수정값 = 이웃 허용색 중 가장 가까운 색 (새 색 생성 없음)."""
    H, W = rgba.shape[:2]
    rgb = rgba[..., :3].astype(np.int32)
    opaque = rgba[..., 3] == 255
    pal = palette.astype(np.int32)
    d = np.abs(rgb[..., None, :] - pal[None, None, :, :]).max(axis=3)  # HxWxP
    in_pal = (d <= tol).any(axis=2)
    pal_idx = np.where(in_pal, d.argmin(axis=2), -1)
    off = opaque & ~in_pal
    if not off.any():
        return []
    lab, n = ndimage.label(off, structure=EIGHT)
    PI = padded(pal_idx, 1, -1)
    PO = padded(opaque, 1, False)
    nb_idx = np.stack([shifted(PI, 1, dy, dx, H, W) for dy, dx in NB8], axis=-1)  # HxWx8
    nb_op = np.stack([shifted(PO, 1, dy, dx, H, W) for dy, dx in NB8], axis=-1)
    out = []
    for sl_i, sl in enumerate(ndimage.find_objects(lab)):
        L = sl_i + 1
        sub = lab[sl] == L
        ys, xs = np.nonzero(sub)
        ys, xs = ys + sl[0].start, xs + sl[1].start
        certain = True
        news = np.zeros((len(ys), 3), dtype=np.uint8)
        for j, (y, x) in enumerate(zip(ys, xs)):
            idxs = nb_idx[y, x][(nb_idx[y, x] >= 0) & nb_op[y, x]]
            uniq = np.unique(idxs)
            if len(uniq) < 2:
                certain = False
                break
            cols = pal[uniq]
            c = rgb[y, x]
            lo, hi = cols.min(axis=0) - 2, cols.max(axis=0) + 2
            if not (np.all(c >= lo) and np.all(c <= hi)):
                certain = False
                break
            news[j] = cols[np.argmin(np.abs(cols - c).sum(axis=1))]
        colors = sorted({tuple(int(v) for v in rgb[y, x]) for y, x in zip(ys[:200], xs[:200])})
        out.append(OffPaletteGroup(ys, xs, news if certain else None, certain, colors[:20],
                                   (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)))
    return out


# ---------------------------------------------------------- 5. 구멍
def enclosed_holes(rgba: np.ndarray, min_area: int, max_area: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """레이어 가장자리에 닿지 않는 완전 투명 성분 (4연결) 중 min~max 면적."""
    hole = rgba[..., 3] == 0
    lab, n = ndimage.label(hole)
    if n == 0:
        return []
    H, W = hole.shape
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])).tolist())
    areas = np.bincount(lab.ravel(), minlength=n + 1)
    out = []
    for L in range(1, n + 1):
        if L in border or not (min_area <= areas[L] <= max_area):
            continue
        ys, xs = np.nonzero(lab == L)
        out.append((ys, xs))
    return out


# ---------------------------------------------------- 6. 종이색 외곽 잔여
def paper_fringe(rgba: np.ndarray, paper_rgb: tuple[int, int, int] | None, ring: int = 2,
                 min_area: int = 6):
    """외곽 ring 픽셀 중 '종이색(밝고 저채도 또는 지정 종이색)'인데 안쪽은 더 어두운 부분."""
    a = rgba[..., 3]
    mask = a > 0
    if not mask.any() or mask.all():
        return []
    dist = ndimage.distance_transform_cdt(mask, metric="chessboard")
    rgb = rgba[..., :3].astype(np.int32)
    lum = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2])
    chroma = rgb.max(axis=2) - rgb.min(axis=2)
    if paper_rgb is not None:
        paper_like = np.abs(rgb - np.array(paper_rgb)).max(axis=2) <= 24
    else:
        paper_like = (lum >= 215) & (chroma <= 25)
    edge = mask & (dist <= ring) & paper_like & (a >= 128)
    if not edge.any():
        return []
    inner = (dist >= ring + 2) & (dist <= ring + 6)
    s = ndimage.uniform_filter(np.where(inner, lum, 0.0), size=13)
    c = ndimage.uniform_filter(inner.astype(float), size=13)
    inner_mean = np.where(c > 0.02, s / np.maximum(c, 1e-9), np.nan)
    fringe = edge & (inner_mean <= lum - 35)
    lab, n = ndimage.label(fringe, structure=EIGHT)
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab)):
        sub = lab[sl] == i + 1
        if sub.sum() < min_area:
            continue
        ys, xs = np.nonzero(sub)
        out.append((ys + sl[0].start, xs + sl[1].start))
    return out


# ---------------------------------------------------- 7. 리핏 연결부
def canvas_lines(rgba: np.ndarray, left: int, top: int, W: int, H: int, axis: str,
                 idxs: list[int]) -> np.ndarray:
    """캔버스 좌표의 열(axis='col') 또는 행(axis='row')을 레이어에서 잘라낸다. 범위 밖은 투명."""
    h, w = rgba.shape[:2]
    if axis == "col":
        out = np.zeros((H, len(idxs), 4), dtype=np.uint8)
        for j, cx in enumerate(idxs):
            lx = cx - left
            if 0 <= lx < w:
                y0, y1 = max(0, top), min(H, top + h)
                if y1 > y0:
                    out[y0:y1, j] = rgba[y0 - top:y1 - top, lx]
        return out
    out = np.zeros((W, len(idxs), 4), dtype=np.uint8)
    for j, cy in enumerate(idxs):
        ly = cy - top
        if 0 <= ly < h:
            x0, x1 = max(0, left), min(W, left + w)
            if x1 > x0:
                out[x0:x1, j] = rgba[ly, x0 - left:x1 - left]
    return out


def _premul(px: np.ndarray) -> np.ndarray:
    f = px.astype(np.float64)
    a = f[..., 3:4] / 255.0
    return np.concatenate([f[..., :3] * a, f[..., 3:4]], axis=-1)


def seam_discontinuities(lines: np.ndarray, min_len: int = 2, abs_thr: float = 60.0,
                         ratio: float = 2.5) -> tuple[list[tuple[int, int, float]], dict]:
    """lines: (N, 4, 4) = [끝-2, 끝-1, 시작0, 시작1] 순서의 4줄.
    이음매 차이가 크고(abs_thr) 양쪽 내부 변화보다 훨씬 클 때(ratio) 불연속으로 본다.
    (단순히 양끝 픽셀이 같은지만 보는 것이 아니라 '연결 방향의 국소 연속성'을 비교)"""
    p = _premul(lines)
    d_seam = np.abs(p[:, 2] - p[:, 1]).sum(axis=1)
    d_in = np.maximum(np.abs(p[:, 1] - p[:, 0]).sum(axis=1), np.abs(p[:, 3] - p[:, 2]).sum(axis=1))
    flag = (d_seam > abs_thr) & (d_seam > ratio * d_in + 8)
    segs = []
    i, N = 0, len(flag)
    while i < N:
        if flag[i]:
            j = i
            while j + 1 < N and (flag[j + 1] or (j + 2 < N and flag[j + 2])):
                j += 1
            if j - i + 1 >= min_len:
                segs.append((i, j + 1, float(d_seam[i:j + 1].max())))
            i = j + 1
        else:
            i += 1
    return segs, {"flagged_lines": int(flag.sum()), "total_lines": int(N)}


# ================================================================ PDF 대조 후 추가 탐지기
def dense_mask(points: list[tuple[int, int]], shape: tuple[int, int], radius: int = 12,
               min_count: int = 5) -> list[bool]:
    """후보가 반경 안에 min_count 개 이상 모여 있으면 True (6쪽 오리: 점묘 같은 의도 표현 영역 보호)."""
    if not points:
        return []
    cnt = np.zeros(shape, np.float32)
    for y, x in points:
        cnt[y, x] += 1
    s = ndimage.uniform_filter(cnt, size=2 * radius + 1, mode="constant") * (2 * radius + 1) ** 2
    return [bool(s[y, x] >= min_count - 0.5) for y, x in points]


@dataclass
class SpikeCandidate:
    y: int
    x: int
    kind: str                 # edge_spur / edge_notch / odd_pixel(boundary)
    old: tuple[int, int, int, int]
    new: tuple[int, int, int, int]
    same_neighbors: int
    repeat_count: int
    intentional: bool


def diagonal_spikes_and_boundary_odd(rgba: np.ndarray, repeat_min: int, has_alpha: bool,
                                     patch_r: int = 3) -> list[SpikeCandidate]:
    """2쪽 '모티프 회전으로 깨진 경계'(대각선 경계의 톱니)와 4쪽 '경계 위 1픽셀 이물 색'.

    A) 톱니(spike/notch): 픽셀 P 의 같은 색 이웃이 1~2개(서로 붙어 있음)뿐이고 나머지 이웃이 모두 한 색 O.
       붙어 있는 같은 색 이웃은 두꺼운 몸체여야 함(그 이웃의 같은 색 이웃 >= 5) → 선 끝·뾰족한 모서리는 제외.
       수정: P := O.
    B) 경계 위 이물 픽셀: P 의 색이 8이웃 어디에도 없고, 이웃 중 한 색 M 이 5개 이상 → P := M.
       (8이웃이 모두 같은 경우는 single_pixel_anomalies 가 담당)"""
    K = pixel_keys(rgba)
    H, W = K.shape
    if H < 3 or W < 3:
        return []
    oob = 0 if has_alpha else OOB
    P = padded(K, 2, oob)
    nb = np.stack([shifted(P, 2, dy, dx, H, W) for dy, dx in NB8], axis=-1)   # HxWx8
    same = (nb == K[..., None])
    nsame = same.sum(axis=2)
    out_idx = []
    # 후보 1차 필터 (벡터) → 세부 판정은 파이썬 루프 (후보 수가 적음)
    cand = (nsame <= 2) & (K != OOB)
    ys, xs = np.nonzero(cand)
    order = [(-1, -1), (-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1)]  # 원형 순서
    ring_idx = [NB8.index(o) for o in order]
    for y, x in zip(ys, xs):
        k = int(K[y, x])
        n = nb[y, x]
        s = same[y, x]
        ns = int(nsame[y, x])
        others = [int(v) for v, sm in zip(n, s) if not sm]
        if any(v == OOB for v in others):
            continue
        if ns == 0:
            if len(set(others)) == 1:
                continue                      # 8이웃 동일 → 기존 탐지기
            vals, cnts = np.unique(np.array(others), return_counts=True)
            m = int(vals[cnts.argmax()])
            if cnts.max() < 5 or (m & 255) != 255:
                continue
            out_idx.append((y, x, "odd_pixel", m, 0))
            continue
        if len(set(others)) != 1:
            continue
        o = others[0]
        if ns == 2:                            # 두 같은 색 이웃은 원형으로 인접해야 함
            pos = [i for i, ri in enumerate(ring_idx) if s[ri]]
            if (pos[1] - pos[0]) % 8 not in (1, 7):
                continue
        thick = False
        for (dy, dx), sm in zip(NB8, s):
            if sm:
                yy, xx = y + dy, x + dx
                if 0 <= yy < H and 0 <= xx < W and int(nsame[yy, xx]) >= 5:
                    thick = True
        if not thick:
            continue
        kind = "edge_notch" if k == 0 or (has_alpha and (k & 255) == 0) else "edge_spur"
        out_idx.append((y, x, kind, o, ns))
    if not out_idx:
        return []
    cy = np.array([t[0] for t in out_idx])
    cx = np.array([t[1] for t in out_idx])
    rep, counts = repeated_mask(K, cy, cx, patch_r, repeat_min, oob)
    return [SpikeCandidate(int(y), int(x), kind, key_to_rgba(int(K[y, x])), key_to_rgba(o), ns, counts[i], bool(rep[i]))
            for i, (y, x, kind, o, ns) in enumerate(out_idx)]


def edge_foreign_specks(rgba: np.ndarray, band: int = 2, delta: float = 60, max_area: int = 16):
    """8쪽 '도장툴/스팟툴 사용 후 정리 안 한 부분': 외곽 띠 안의 작은 이질 점(주변 몸체보다 확연히 어둡거나 밝음)."""
    a = rgba[..., 3]
    mask = a >= 128
    if mask.sum() < 50 or mask.all():
        return []
    dist = ndimage.distance_transform_cdt(mask, metric="chessboard")
    rgb = rgba[..., :3].astype(np.float32)
    lum = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    w = mask.astype(np.float32)
    s = ndimage.uniform_filter(lum * w, size=15)
    c = ndimage.uniform_filter(w, size=15)
    local = s / np.maximum(c, 1e-6)
    odd = mask & (dist <= band) & (np.abs(lum - local) >= delta)
    lab, n = ndimage.label(odd, structure=EIGHT)
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab)):
        sub = lab[sl] == i + 1
        if sub.sum() > max_area:
            continue
        yy, xx = np.nonzero(sub)
        out.append((yy + sl[0].start, xx + sl[1].start))
    return out


def hard_edge_segments(rgba: np.ndarray, soft_ratio: float = 0.4, min_px: int = 30):
    """8쪽 '외곽 매끄럽게 정리 안 된 부분': 대부분 부드러운(반투명) 외곽인 레이어에서
    계단처럼 딱딱한(alpha 0→255) 외곽이 몰려 있는 구간."""
    a = rgba[..., 3]
    m = a > 0
    if not m.any() or m.all():
        return []
    Pm = padded(~m, 1, True)
    H, W = a.shape
    touch = np.zeros_like(m)
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        touch |= shifted(Pm, 1, dy, dx, H, W)
    boundary = m & touch
    nb = int(boundary.sum())
    if nb < min_px:
        return []
    soft = boundary & (a < 255)
    if soft.sum() / nb < soft_ratio:
        return []                    # 원래 하드 엣지 레이어
    hard = boundary & (a == 255)
    lab, n = ndimage.label(ndimage.binary_dilation(hard, iterations=2), structure=EIGHT)
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab)):
        sub = (lab[sl] == i + 1) & hard[sl]
        if sub.sum() >= min_px:
            yy, xx = np.nonzero(sub)
            out.append((yy + sl[0].start, xx + sl[1].start))
    return out


def straight_line_artifacts(rgba: np.ndarray, min_len: int = 24):
    """8쪽 사례의 세로/가로 1px 직선 자국 (양옆과 다른 1px 폭 직선이 길게 이어짐)."""
    rgba = np.pad(rgba, ((1, 1), (1, 1), (0, 0)))   # 레이어 경계에 붙은 선도 잡도록 투명 1px 여백
    f = _premul(rgba)
    out = []
    for axis in ("v", "h"):
        g = f if axis == "v" else f.transpose(1, 0, 2)
        if g.shape[1] < 3:
            continue
        c, l, r = g[:, 1:-1], g[:, :-2], g[:, 2:]
        dl = np.abs(c - l).sum(axis=2)
        dr = np.abs(c - r).sum(axis=2)
        dlr = np.abs(l - r).sum(axis=2)
        line = (dl > 30) & (dr > 30) & (dlr < 0.35 * np.minimum(dl, dr))
        for j in range(line.shape[1]):
            col = line[:, j]
            if col.sum() < min_len:
                continue
            # 가장 긴 연속 구간
            d = np.diff(np.concatenate([[0], col.astype(int), [0]]))
            st, en = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
            for s0, e0 in zip(st, en):
                if e0 - s0 >= min_len:
                    if axis == "v":
                        out.append(("vertical", j, int(s0) - 1, int(e0) - 1))
                    else:
                        out.append(("horizontal", j, int(s0) - 1, int(e0) - 1))
    return out


def internal_seams(rgba: np.ndarray, run_len: int = 3, min_runs: int = 3, factor: float = 2.5, thr: float = 30):
    """10쪽 '지무늬 리핏 안 맞는 경우': 캔버스 안쪽에서 타일을 이어 붙인 경계가 어긋난 세로/가로 직선.
    잘린 이음매에서는 무늬(점 등)가 한 직선에서 일제히 잘려 '곧은 세로(가로) 경계 구간(>= run_len 줄)'이 많이 생긴다.
    열마다 그런 구간 수를 세고, 다른 열들(상위 1%)보다 factor 배 이상 튀는 열을 보고.
    디자인된 직선 경계(스트라이프·체크: 같은 색 쌍이 대부분)는 제외."""
    f = _premul(rgba)
    K = pixel_keys(rgba)
    out = []
    for axis in ("v", "h"):
        g = f if axis == "v" else f.transpose(1, 0, 2)
        kk = K if axis == "v" else K.T
        N, M = g.shape[:2]
        if M < 16 or N < 16:
            continue
        strong = np.abs(np.diff(g, axis=1)).sum(axis=2) > thr      # strong[:, j] : 이음매 j|j+1
        # 세로 방향 연속 구간: 시작/끝 찾기
        st = strong.astype(np.int8)
        d = np.diff(np.vstack([np.zeros((1, st.shape[1]), np.int8), st, np.zeros((1, st.shape[1]), np.int8)]), axis=0)
        nruns = np.zeros(st.shape[1], int)
        medlen = np.zeros(st.shape[1])
        spans: dict = {}
        for j in range(st.shape[1]):
            s0 = np.nonzero(d[:, j] == 1)[0]
            e0 = np.nonzero(d[:, j] == -1)[0]
            L = e0 - s0
            ok = L >= run_len
            nruns[j] = int(ok.sum())
            if ok.any():
                medlen[j] = float(np.median(L[ok]))
                spans[j] = (int(s0[ok].min()), int(e0[ok].max()))
        if nruns.max() < min_runs:
            continue
        base = max(1.0, float(np.percentile(nruns, 99)))     # 열 전체 상위 1% (한 번만 계산)
        for j in np.nonzero(nruns >= min_runs)[0]:
            if nruns[j] < factor * base:
                continue
            loc = np.r_[nruns[max(0, j - 4):j], nruns[j + 1:j + 5]]
            if len(loc) and loc.max() * 2 > nruns[j]:
                continue      # 바로 옆 줄도 높음 = 점선·테두리 같은 얇은 디자인 요소(위/아래 가장자리 쌍)
            if medlen[j] >= 16:
                continue      # 긴 직선 경계(스트라이프·체크) = 디자인. 잘린 이음매는 짧은 구간이 여러 개
            r0, r1 = spans[j]
            out.append(("vertical" if axis == "v" else "horizontal", int(j + 1), r0, r1, int(nruns[j])))
    return out


def _chains(K: np.ndarray, max_jump: int = 2):
    """열마다 세로 방향 색 전환(y|y+1, 위색 a, 아래색 b)을 찾아 같은 (a,b) 전환을 옆 열과 이어 경계선 체인을 만든다.
    반환: [(a, b, [(x, y), ...])]  (y = 전환 위쪽 픽셀의 행)"""
    H, W = K.shape
    tr = K[:-1] != K[1:]
    open_chains: dict = {}          # (a,b) -> list of chains (각 chain: [ (x,y), ... ])
    done = []
    prev_ends: list = []
    for x in range(W):
        ys = np.nonzero(tr[:, x])[0]
        cur = []
        used = set()
        for y in ys:
            a, b = int(K[y, x]), int(K[y + 1, x])
            best = None
            for ci, (pa, pb, ch) in enumerate(prev_ends):
                if ci in used or pa != a or pb != b:
                    continue
                if abs(ch[-1][1] - y) <= max_jump and ch[-1][0] == x - 1:
                    if best is None or abs(ch[-1][1] - y) < abs(prev_ends[best][2][-1][1] - y):
                        best = ci
            if best is not None:
                used.add(best)
                ch = prev_ends[best][2]
                ch.append((x, int(y)))
                cur.append((a, b, ch))
            else:
                cur.append((a, b, [(x, int(y))]))
        for ci, item in enumerate(prev_ends):
            if ci not in used:
                done.append(item)
        prev_ends = cur
    done += prev_ends
    return [c for c in done if len(c[2]) >= 9]


def _spread(xs: np.ndarray, hs: np.ndarray) -> float:
    """점들이 '디지털 직선'인지: 어떤 기울기 s 에 대해 (h - s*x) 의 최대-최소 < 1 이면 디지털 직선."""
    if len(xs) < 2:
        return 0.0
    s0 = np.polyfit(xs, hs, 1)[0]
    best = 9e9
    for s in s0 + np.linspace(-0.12, 0.12, 25):
        r = hs - s * xs
        best = min(best, float(r.max() - r.min()))
    return best


def line_profile_bumps(rgba: np.ndarray, has_alpha: bool, w: int = 7):
    """2쪽 '모티프 회전으로 깨진 경계': 경계선(가로형·세로형)을 따라가며
    양옆 w칸이 깨끗한 디지털 직선인데 가운데 한 칸만 1px 벗어나, 그 칸을 ±1 고치면 전체가 디지털 직선이 되는 곳.
    (곡선·원 꼭짓점은 ±w 범위가 직선이 아니므로 제외)
    반환: [(y, x, new_key, old_key, kind)] — 수정은 1픽셀, 이웃의 기존 색으로."""
    K0 = pixel_keys(rgba)
    out = []
    for transpose in (False, True):
        K = K0.T if transpose else K0
        H, W = K.shape
        for a, b, ch in _chains(K):
            if len(ch) < 2 * w + 1:
                continue
            xs = np.array([p[0] for p in ch], float)
            hs = np.array([p[1] for p in ch], float)
            n = len(ch)
            for i in range(w, n - w):
                if xs[i + w] - xs[i - w] != 2 * w:
                    continue
                sl = slice(i - w, i + w + 1)
                wx, wh = xs[sl], hs[sl].copy()
                if _spread(wx, wh) < 0.999:
                    continue                       # 이미 깨끗한 직선
                side = np.r_[0:w, w + 1:2 * w + 1]
                if _spread(wx[side], wh[side]) >= 0.999:
                    continue                       # 양옆이 직선이 아님 (곡선/복잡한 형태)
                if abs(np.polyfit(wx[side], wh[side], 1)[0]) > 1.0:
                    continue
                ok = []
                for dd in (-1, 1):
                    t = wh.copy()
                    t[w] += dd
                    if _spread(wx, t) < 0.999:
                        ok.append(dd)
                if len(ok) != 1:
                    continue
                x, y = int(xs[i]), int(hs[i])
                if ok[0] == 1:   # 전환이 1칸 위에 있음 → 아래색 b 가 위로 튀어나옴: (y+1) 픽셀을 a 로
                    py, new, old = y + 1, a, b
                else:            # 전환이 1칸 아래 → 위색 a 가 아래로 파고듦: y 픽셀을 b 로
                    py, new, old = y, b, a
                if not (0 <= py < H) or int(K[py, x]) != old:
                    continue
                win = K[max(0, py - 1):py + 2, max(0, x - 1):x + 2]
                if not set(np.unique(win).tolist()) <= {a, b}:
                    continue
                kind = "edge_notch" if old == 0 else "edge_spur"
                yy, xx = (x, py) if transpose else (py, x)
                out.append((yy, xx, new, old, kind))
    seen, uniq = set(), []
    for t in out:
        if (t[0], t[1]) not in seen:
            seen.add((t[0], t[1]))
            uniq.append(t)
    return uniq
