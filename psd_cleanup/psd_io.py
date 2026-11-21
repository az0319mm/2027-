"""PSD/PSB 읽기·쓰기.

쓰기 전략 (레이어 보존의 핵심):
  원본 파일을 새로 파싱한 뒤, **수정 대상 레이어의 픽셀 채널 데이터만** 같은 압축 방식으로
  다시 인코딩해 교체한다. 레이어 레코드(이름·순서·가시성·불투명도·블렌드 모드·마스크·
  효과·태그 블록), 이미지 리소스(ICC·해상도 등), 헤더(색상 모드·비트 심도·캔버스 크기)는
  원본 바이트를 그대로 다시 기록한다. 레이어 경계(bounds)는 바꾸지 않는다.

  병합(composite) 미리보기 이미지는 '변경된 픽셀이 있는 곳만' 갱신한다.
  (psd-tools 합성 결과의 원본/수정본 차이(delta)만 원래 병합 이미지에 더함 → 렌더러 차이 상쇄)
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from psd_tools import PSDImage

COLOR_MODE_NAMES = {0: "Bitmap", 1: "Grayscale", 2: "Indexed", 3: "RGB", 4: "CMYK",
                    7: "Multichannel", 8: "Duotone", 9: "Lab"}
COLOR_CHANNELS = {1: 1, 3: 3, 4: 4, 9: 3}
DEFAULT_NAME_RE = re.compile(r"^(Layer|레이어|图层|レイヤー|Ebene|Calque)\s*\d+(\s*(copy|복사|拷贝|のコピー).*)?$", re.I)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class LayerInfo:
    index: int               # 파일 내 레이어 레코드 인덱스 (0 = 맨 아래)
    layer_id: int | None
    name: str
    path: str                # "그룹/하위그룹/레이어"
    kind: str                # pixel, group, type, shape, smartobject, adjustment ...
    visible: bool
    effective_visible: bool  # 상위 그룹 가시성 포함
    opacity: int
    fill_opacity: int | None
    blend_mode: str
    bbox: tuple[int, int, int, int]  # left, top, right, bottom (캔버스 좌표)
    clipping: bool
    has_mask: bool
    has_vector_mask: bool
    has_effects: bool
    locks: dict[str, bool]
    has_alpha_channel: bool
    channel_ids: list[int]
    editable: bool = False
    unsupported_reasons: list[str] = field(default_factory=list)

    @property
    def width(self) -> int:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> int:
        return self.bbox[3] - self.bbox[1]

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["bbox"] = list(self.bbox)
        return d


@dataclass
class DocumentInfo:
    path: str
    sha256: str
    version: int            # 1=PSD, 2=PSB
    width: int
    height: int
    channels: int
    depth: int
    color_mode: int
    color_mode_name: str
    has_icc: bool
    icc_bytes_sha256: str | None
    resolution: dict[str, Any] | None
    has_merged_image: bool
    layer_count: int
    pixel_edit_supported: bool
    unsupported_reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class PSDDocument:
    """읽기 전용 래퍼. 원본 파일은 열어서 읽기만 한다."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.sha256 = sha256_file(self.path)
        self.psd = PSDImage.open(self.path)
        self.header = self.psd._record.header
        li = self.psd._record.layer_and_mask_information.layer_info
        self._records = list(li.layer_records) if (li is not None and li.layer_records) else []
        self._chandata = list(li.channel_image_data) if (li is not None and li.channel_image_data) else []
        self._rec_index = {id(r): i for i, r in enumerate(self._records)}
        self.doc_info = self._build_doc_info()
        self.layers = self._build_layers()

    # ------------------------------------------------------------------ info
    def _build_doc_info(self) -> DocumentInfo:
        h = self.header
        mode = int(h.color_mode)
        reasons = []
        if int(h.depth) != 8:
            reasons.append(f"{int(h.depth)}비트 문서 (자동 수정은 8비트만 지원)")
        if mode not in (1, 3):
            reasons.append(f"색상 모드 {COLOR_MODE_NAMES.get(mode, mode)} (자동 수정은 RGB/Grayscale만 지원)")
        res = self.psd.image_resources
        icc = None
        resolution = None
        try:
            from psd_tools.constants import Resource
            icc_block = res.get_data(Resource.ICC_PROFILE)
            if icc_block is not None:
                icc = bytes(icc_block)
            ri = res.get_data(Resource.RESOLUTION_INFO)
            if ri is not None:
                resolution = {"h_res": float(ri.horizontal), "v_res": float(ri.vertical),
                              "h_unit": int(ri.horizontal_unit), "v_unit": int(ri.vertical_unit)}
        except Exception:  # pragma: no cover - 리소스 파싱 실패는 보고만
            pass
        img = self.psd._record.image_data
        return DocumentInfo(
            path=str(self.path), sha256=self.sha256, version=int(h.version),
            width=int(h.width), height=int(h.height), channels=int(h.channels),
            depth=int(h.depth), color_mode=mode, color_mode_name=COLOR_MODE_NAMES.get(mode, str(mode)),
            has_icc=icc is not None,
            icc_bytes_sha256=hashlib.sha256(icc).hexdigest() if icc else None,
            resolution=resolution,
            has_merged_image=bool(img is not None and img.data),
            layer_count=len(self._records),
            pixel_edit_supported=not reasons, unsupported_reasons=reasons,
        )

    def _build_layers(self) -> list[LayerInfo]:
        out: list[LayerInfo] = []
        doc_ok = self.doc_info.pixel_edit_supported

        def walk(group, prefix: str, parent_visible: bool):
            for layer in group:
                rec = layer._record
                idx = self._rec_index.get(id(rec), -1)
                path = f"{prefix}{layer.name}"
                eff_vis = parent_visible and layer.visible
                locks = {"transparency": False, "composite": False, "position": False, "all": False}
                try:
                    lk = layer.locks
                    if lk is not None:   # lspf 비트: 0x1 투명, 0x2 픽셀, 0x4 위치, 0x80000000 모두
                        v = int(lk.value)
                        locks = {"transparency": bool(v & 0x1), "composite": bool(v & 0x2),
                                 "position": bool(v & 0x4), "all": bool(v & 0x80000000)}
                except Exception:
                    pass
                ids = [int(c.id) for c in rec.channel_info]
                try:
                    lid = int(layer.layer_id)
                    lid = None if lid < 0 else lid
                except Exception:
                    lid = None
                try:
                    fill = int(layer.fill_opacity)
                except Exception:
                    fill = None
                has_mask = bool(layer.has_mask())
                info = LayerInfo(
                    index=idx, layer_id=lid, name=layer.name, path=path, kind=str(layer.kind),
                    visible=bool(layer.visible), effective_visible=bool(eff_vis),
                    opacity=int(layer.opacity), fill_opacity=fill,
                    blend_mode=getattr(layer.blend_mode, "name", str(layer.blend_mode)),
                    bbox=(int(rec.left), int(rec.top), int(rec.right), int(rec.bottom)),
                    clipping=bool(getattr(layer, "clipping", False)),
                    has_mask=has_mask, has_vector_mask=bool(layer.has_vector_mask()),
                    has_effects=bool(layer.has_effects()), locks=locks,
                    has_alpha_channel=-1 in ids, channel_ids=ids,
                )
                r = info.unsupported_reasons
                if not doc_ok:
                    r.extend(self.doc_info.unsupported_reasons)
                if info.kind != "pixel":
                    r.append(f"비픽셀 레이어({info.kind})")
                if info.width <= 0 or info.height <= 0:
                    r.append("빈 레이어(픽셀 없음)")
                if locks["all"] or locks["transparency"] or locks["composite"]:
                    r.append("잠긴 레이어")
                if has_mask or info.has_vector_mask:
                    r.append("레이어 마스크/벡터 마스크 있음 (마스크 결합 결과 수정 미지원)")
                if idx < 0:
                    r.append("레이어 레코드 매핑 실패")
                info.editable = not r
                out.append(info)
                if layer.is_group():
                    walk(layer, path + "/", eff_vis)

        walk(self.psd, "", True)
        out.sort(key=lambda l: l.index)
        return out

    # ---------------------------------------------------------------- pixels
    def read_channels(self, index: int) -> dict[int, np.ndarray]:
        """레이어 로컬 좌표의 원본 채널 (uint8, HxW). 마스크 채널(-2,-3)은 제외."""
        rec = self._records[index]
        w, h = rec.right - rec.left, rec.bottom - rec.top
        if w <= 0 or h <= 0:
            return {}
        depth, ver = int(self.header.depth), int(self.header.version)
        if depth != 8:
            raise NotImplementedError("8비트 문서만 픽셀 분석을 지원합니다")
        out = {}
        for ci, cd in zip(rec.channel_info, self._chandata[index]):
            cid = int(ci.id)
            if cid in (-2, -3):
                continue
            raw = cd.get_data(w, h, depth, ver)
            out[cid] = np.frombuffer(raw, dtype=np.uint8).reshape(h, w).copy()
        return out

    def read_rgba(self, index: int) -> np.ndarray | None:
        """HxWx4 RGBA (Grayscale은 회색을 RGB로 복제). 지원 불가면 None."""
        mode = self.doc_info.color_mode
        if mode not in (1, 3) or self.doc_info.depth != 8:
            return None
        ch = self.read_channels(index)
        if not ch:
            return None
        if mode == 3:
            if not all(k in ch for k in (0, 1, 2)):
                return None
            rgb = [ch[0], ch[1], ch[2]]
        else:
            if 0 not in ch:
                return None
            rgb = [ch[0]] * 3
        a = ch.get(-1)
        if a is None:
            a = np.full_like(rgb[0], 255)
        return np.dstack(rgb + [a])

    def merged_rgb(self) -> np.ndarray | None:
        """파일에 저장된 병합 이미지 (Photoshop이 저장한 그대로). RGB/Gray 8bit만."""
        mode = self.doc_info.color_mode
        if not self.doc_info.has_merged_image or self.doc_info.depth != 8 or mode not in (1, 3):
            return None
        h = self.header
        chans = self.psd._record.image_data.get_data(h, split=True)
        arrs = [np.frombuffer(c, dtype=np.uint8).reshape(h.height, h.width) for c in chans]
        if mode == 3:
            return np.dstack(arrs[:3]).copy()
        return np.dstack([arrs[0]] * 3).copy()

    def layer_by_index(self, index: int) -> LayerInfo:
        for l in self.layers:
            if l.index == index:
                return l
        raise KeyError(index)

    def raw_channel_bytes(self, index: int) -> list[bytes]:
        return [bytes(cd.data) for cd in self._chandata[index]]


def is_default_name(name: str) -> bool:
    return bool(DEFAULT_NAME_RE.match(name.strip()))


# ====================================================================== write
def _composite_rgb8(psd: PSDImage) -> np.ndarray:
    from psd_tools.composite import composite
    color, _shape, _alpha = composite(psd)
    arr = np.asarray(color, dtype=np.float64)
    if arr.ndim == 2:
        arr = arr[..., None]
    arr = np.clip(np.round(arr * 255.0), 0, 255).astype(np.int16)
    if arr.shape[2] == 1:
        arr = np.repeat(arr, 3, axis=2)
    return arr[..., :3]


def write_patched_psd(src: str | Path, dst: str | Path,
                      patches: dict[int, dict[int, np.ndarray]]) -> dict[str, Any]:
    """원본 src를 읽어, patches[레이어인덱스][채널ID] = 새 채널 배열(HxW uint8)로 교체한 PSD를 dst에 기록.

    - src 와 dst 가 같으면 거부.
    - 임시 파일에 쓴 뒤 원자적으로 교체 → 실패 시 반쪽 파일이 남지 않음.
    """
    src, dst = Path(src).resolve(), Path(dst).resolve()
    if src == dst:
        raise ValueError("출력 경로가 원본과 같습니다. 원본 덮어쓰기는 금지되어 있습니다.")
    psd = PSDImage.open(src)
    header = psd._record.header
    depth, ver = int(header.depth), int(header.version)
    li = psd._record.layer_and_mask_information.layer_info
    info: dict[str, Any] = {"patched_layers": sorted(patches), "merged_image_updated": False,
                            "merged_changed_pixels": 0}
    for idx, chmap in patches.items():
        rec = li.layer_records[idx]
        w, h = rec.right - rec.left, rec.bottom - rec.top
        for ci, cd in zip(rec.channel_info, li.channel_image_data[idx]):
            cid = int(ci.id)
            if cid in chmap:
                arr = np.ascontiguousarray(chmap[cid], dtype=np.uint8)
                if arr.shape != (h, w):
                    raise ValueError(f"채널 크기 불일치: layer {idx} ch {cid}")
                cd.set_data(arr.tobytes(), w, h, depth, ver)

    buf = io.BytesIO()
    psd._record.write(buf)

    # 병합 이미지 delta 갱신 (변경이 있을 때만, RGB/Gray 8bit)
    mode = int(header.color_mode)
    img = psd._record.image_data
    if patches and img is not None and img.data and depth == 8 and mode in (1, 3):
        before = _composite_rgb8(PSDImage.open(src))
        after = _composite_rgb8(PSDImage.open(io.BytesIO(buf.getvalue())))
        delta = after - before
        changed = np.any(delta != 0, axis=2)
        if changed.any():
            chans = img.get_data(header, split=True)
            arrs = [np.frombuffer(c, dtype=np.uint8).reshape(header.height, header.width).astype(np.int16)
                    for c in chans]
            ncol = 3 if mode == 3 else 1
            for c in range(ncol):
                d = delta[..., c] if mode == 3 else np.round(delta.mean(axis=2)).astype(np.int16)
                arrs[c] = np.where(changed, np.clip(arrs[c] + d, 0, 255), arrs[c])
            img.set_data([a.astype(np.uint8).tobytes() for a in arrs], header)
            info["merged_image_updated"] = True
            info["merged_changed_pixels"] = int(changed.sum())
            buf = io.BytesIO()
            psd._record.write(buf)

    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=dst.suffix, dir=dst.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(buf.getvalue())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return info
