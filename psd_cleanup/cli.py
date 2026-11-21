"""명령줄 실행.

예)
  python -m psd_cleanup run 작품.psd --type apparel --repeat both --colors "#FFFFFF,#F2A0B5,#7A5A50"
  python -m psd_cleanup run 폴더/ --type interior --style watercolor --repeat horizontal
  python -m psd_cleanup inspect 작품.psd            # 1단계: 읽기/검수만 (수정 없음, 결과 PSD는 원본 사본)
  python -m psd_cleanup serve                        # 로컬 UI + Photoshop 플러그인 연결 서버 (127.0.0.1)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ProjectConfig
from .pipeline import run_batch


def _cfg_from_args(a) -> ProjectConfig:
    cfg = ProjectConfig.load(a.config) if a.config else ProjectConfig()
    if a.type:
        cfg.work_type = a.type
    if a.repeat:
        cfg.repeat = a.repeat
    if a.colors:
        cfg.allowed_colors = [c.strip() for c in a.colors.split(",") if c.strip()]
    if a.color_count:
        cfg.expected_color_count = a.color_count
    if a.style:
        cfg.interior_style = a.style
    if a.paper_color:
        cfg.paper_color = a.paper_color
    if a.protect_layer:
        cfg.protected_layers += a.protect_layer
    if a.texture_layer:
        cfg.approved_texture_layers += a.texture_layer
    if a.no_fix:
        cfg.auto_fix = False
    cfg.validate()
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="psd_cleanup", description="졸업작품 PSD 자동 클린업 (로컬 실행)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "inspect"):
        p = sub.add_parser(name)
        p.add_argument("inputs", nargs="+", help="PSD/PSB 파일 또는 폴더")
        p.add_argument("--type", choices=["apparel", "interior"])
        p.add_argument("--repeat", choices=["none", "vertical", "horizontal", "both"])
        p.add_argument("--colors", help="허용 색 목록 #RRGGBB,... (어패럴)")
        p.add_argument("--color-count", type=int, help="도수 (어패럴)")
        p.add_argument("--style", choices=["digital", "watercolor", "pencil", "crayon"], help="표현 방식 (인테리어)")
        p.add_argument("--paper-color", help="스캔 종이색 #RRGGBB (인테리어)")
        p.add_argument("--protect-layer", action="append", help="보호 레이어 이름(glob), 여러 번 가능")
        p.add_argument("--texture-layer", action="append", help="승인된 질감 레이어 이름(glob)")
        p.add_argument("--config", help="프로젝트 설정 JSON")
        p.add_argument("--out", help="출력 폴더 (기본: 입력 폴더/_cleanup_output)")
        p.add_argument("--recursive", action="store_true")
        p.add_argument("--overwrite-outputs", action="store_true", help="기존 결과 파일만 덮어쓰기 (원본은 절대 아님)")
        p.add_argument("--no-fix", action="store_true", help="검수만 (자동 수정 없음)")
        p.add_argument("--save-config", help="현재 설정을 JSON으로 저장")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        from .server import serve
        serve(a.port)
        return 0
    if a.cmd == "inspect":
        a.no_fix = True
    cfg = _cfg_from_args(a)
    if a.save_config:
        cfg.save(a.save_config)

    def prog(f, m):
        print(f"\r[{f * 100:5.1f}%] {m[:100]:<100}", end="", file=sys.stderr, flush=True)

    results = run_batch(a.inputs, cfg, a.out, recursive=a.recursive, overwrite_outputs=a.overwrite_outputs,
                        progress=prog)
    print(file=sys.stderr)
    rc = 0
    for r in results:
        print(json.dumps({"input": r.input_path, "status": r.status, "error": r.error,
                          "summary": {k: v for k, v in r.summary.items() if k != "traceback"},
                          "outputs": r.outputs}, ensure_ascii=False, indent=1))
        if r.status != "ok":
            rc = 1
    if not results:
        print("처리할 PSD/PSB 파일이 없습니다.", file=sys.stderr)
        rc = 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
