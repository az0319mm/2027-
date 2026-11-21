"""저장소에 포함된 합성 샘플 PSD 생성 (어패럴 결함 / 인테리어 잔여 / 리핏 깨짐).
python tools/make_sample.py examples/  → *.psd 생성"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from psd_factory import build_psd  # noqa: E402
import test_cleanup as T  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else "examples")
out.mkdir(parents=True, exist_ok=True)
build_psd(out / "sample_apparel_defects.psd", (T.W, T.H), [
    dict(name="Background", rgba=T._bg()),
    dict(group="모티브", layers=[dict(name="딸기 모티브", rgba=T._defect_motif()),
                              dict(name="정상 형태", rgba=T._shapes_layer())]),
])
build_psd(out / "sample_interior_residue.psd", (T.W, T.H), [
    dict(name="Background", rgba=T._bg()), dict(name="수채 모티브", rgba=T._interior_layer())])
build_psd(out / "sample_repeat_broken.psd", (64, 64), [dict(name="tile", rgba=T._tile(True), trim=False)])
print("ok")
