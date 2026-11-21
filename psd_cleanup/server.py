"""로컬 전용 HTTP 서버 (127.0.0.1). 브라우저 UI와 Photoshop UXP 플러그인이 이 서버에 작업을 요청한다.

보안:
- 127.0.0.1 에만 바인딩 (외부 네트워크 노출 없음, 클라우드 전송 없음)
- /api/* 는 사용자 정의 헤더 `X-PSD-Cleanup: 1` 필수 → 다른 웹사이트가 브라우저를 통해 몰래 호출하는 것(CSRF) 차단
- Host 헤더가 localhost/127.0.0.1 이 아니면 거부 (DNS rebinding 방지)
- /files/ 는 이 서버가 만든 결과 폴더의 파일만 제공
"""
from __future__ import annotations

import json
import mimetypes
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .config import ProjectConfig
from .pipeline import run_batch
from .psd_io import PSDDocument

UI_FILE = Path(__file__).with_name("ui.html")
JOBS: dict[str, dict] = {}
ALLOWED_OUTPUT_DIRS: set[Path] = set()
LOCK = threading.Lock()


def _start_job(inputs, cfg, out_dir, overwrite) -> str:
    jid = uuid.uuid4().hex[:12]
    cancel = threading.Event()
    job = {"id": jid, "status": "running", "progress": 0.0, "message": "대기", "results": [], "cancel": cancel}
    with LOCK:
        JOBS[jid] = job

    def prog(f, m):
        job["progress"], job["message"] = round(float(f), 4), m

    def work():
        try:
            res = run_batch(inputs, cfg, out_dir, overwrite_outputs=overwrite, progress=prog, cancel=cancel)
            job["results"] = [{"input": r.input_path, "status": r.status, "error": r.error,
                               "summary": r.summary, "outputs": r.outputs,
                               "layers": r.qa.get("layers", [])} for r in res]
            for r in res:
                for p in r.outputs.values():
                    ALLOWED_OUTPUT_DIRS.add(Path(p).resolve().parent)
            job["status"] = "cancelled" if cancel.is_set() else "done"
        except Exception as e:
            job["status"], job["message"] = "failed", f"{type(e).__name__}: {e}"

    threading.Thread(target=work, daemon=True).start()
    return jid


class Handler(BaseHTTPRequestHandler):
    server_version = "psd-cleanup/" + __version__

    def log_message(self, fmt, *args):  # 조용히
        pass

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _api_guard(self) -> bool:
        if not self._host_ok():
            self._json(403, {"error": "host not allowed"})
            return False
        if self.headers.get("X-PSD-Cleanup") != "1":
            self._json(403, {"error": "missing X-PSD-Cleanup header"})
            return False
        return True

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ValueError("요청이 너무 큽니다")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        if not self._host_ok():
            return self._json(403, {"error": "host not allowed"})
        if u.path in ("/", "/index.html"):
            body = UI_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/files":
            p = Path(unquote(parse_qs(u.query).get("path", [""])[0])).resolve()
            if p.parent not in ALLOWED_OUTPUT_DIRS or not p.is_file() or p.suffix.lower() not in (
                    ".png", ".html", ".json"):
                return self._json(404, {"error": "not found"})
            body = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(p.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self._api_guard():
            return
        if u.path == "/api/health":
            return self._json(200, {"ok": True, "version": __version__})
        if u.path.startswith("/api/jobs/"):
            job = JOBS.get(u.path.rsplit("/", 1)[-1])
            if not job:
                return self._json(404, {"error": "no such job"})
            return self._json(200, {k: v for k, v in job.items() if k != "cancel"})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._api_guard():
            return
        u = urlparse(self.path)
        try:
            body = self._body()
            if u.path == "/api/run":
                inputs = body.get("inputs") or []
                if not inputs or not all(isinstance(x, str) for x in inputs):
                    return self._json(400, {"error": "inputs 에 PSD 파일/폴더 경로를 넣으세요"})
                for x in inputs:
                    if not Path(x).exists():
                        return self._json(400, {"error": f"경로가 없습니다: {x}"})
                cfg = ProjectConfig.from_dict(body.get("config") or {})
                jid = _start_job(inputs, cfg, body.get("out_dir") or None, bool(body.get("overwrite_outputs")))
                return self._json(200, {"job_id": jid})
            if u.path.startswith("/api/jobs/") and u.path.endswith("/cancel"):
                job = JOBS.get(u.path.split("/")[3])
                if not job:
                    return self._json(404, {"error": "no such job"})
                job["cancel"].set()
                return self._json(200, {"ok": True})
            if u.path == "/api/inspect":
                doc = PSDDocument(body["path"])
                return self._json(200, {"document": doc.doc_info.to_dict(),
                                        "layers": [l.to_dict() for l in doc.layers]})
        except Exception as e:
            return self._json(400, {"error": f"{type(e).__name__}: {e}"})
        self._json(404, {"error": "not found"})


def make_server(port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(port: int = 8765) -> None:
    srv = make_server(port)
    print(f"PSD 클린업 로컬 서버: http://127.0.0.1:{port}/  (Ctrl+C 로 종료, 외부 접속 불가)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
