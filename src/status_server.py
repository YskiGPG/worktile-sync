"""轻量 HTTP 状态接口，供远程查看同步状态"""

import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class _StatusHandler(BaseHTTPRequestHandler):
    """处理 /health, /status, /audit 请求"""

    local_dir: Path  # 由 StatusServer 注入

    def do_GET(self) -> None:
        path = self.path.rstrip("/")

        if path == "" or path == "/":
            self._serve_dashboard()
        elif path == "/health":
            self._serve_json("sync_health.json")
        elif path == "/status":
            self._serve_status()
        elif path == "/progress":
            self._serve_json("sync_progress.json")
        elif path == "/audit":
            self._serve_audit()
        elif path == "/log":
            self._serve_log()
        else:
            self._send(404, "text/plain", "Not Found")

    def _serve_json(self, filename: str) -> None:
        fpath = self.local_dir / filename
        if not fpath.exists():
            self._send(404, "application/json", '{"error": "file not found"}')
            return
        data = fpath.read_text(encoding="utf-8")
        self._send(200, "application/json; charset=utf-8", data)

    def _serve_status(self) -> None:
        """综合状态：health + progress 合并"""
        result: dict[str, Any] = {}
        for name, key in [("sync_health.json", "health"), ("sync_progress.json", "progress")]:
            fpath = self.local_dir / name
            if fpath.exists():
                try:
                    result[key] = json.loads(fpath.read_text(encoding="utf-8"))
                except Exception:
                    result[key] = {"error": "parse failed"}
        self._send(200, "application/json; charset=utf-8",
                   json.dumps(result, ensure_ascii=False, indent=2))

    def _serve_audit(self) -> None:
        """最近 50 条审计记录"""
        fpath = self.local_dir / "sync_audit.csv"
        if not fpath.exists():
            self._send(404, "text/plain", "No audit data")
            return
        lines = fpath.read_text(encoding="utf-8").strip().split("\n")
        header = lines[0] if lines else ""
        recent = lines[-50:] if len(lines) > 51 else lines[1:]
        self._send(200, "text/plain; charset=utf-8",
                   header + "\n" + "\n".join(recent))

    def _serve_log(self) -> None:
        """最近 100 行日志"""
        log_path = self.local_dir / "sync.log"
        if not log_path.exists():
            # 尝试工作目录
            log_path = Path("sync.log")
        if not log_path.exists():
            self._send(404, "text/plain", "No log file")
            return
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        self._send(200, "text/plain; charset=utf-8", "\n".join(lines[-100:]))

    def _serve_dashboard(self) -> None:
        """简易 HTML 仪表盘"""
        html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Worktile Sync</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{font-family:system-ui,sans-serif;max-width:800px;margin:20px auto;padding:0 15px;background:#f5f5f5}
  h1{color:#333;border-bottom:2px solid #4a90d9;padding-bottom:8px}
  .card{background:#fff;border-radius:8px;padding:16px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.1)}
  .ok{color:#27ae60} .error{color:#e74c3c} .label{color:#666;font-size:.9em}
  pre{background:#2d2d2d;color:#f8f8f2;padding:12px;border-radius:6px;overflow-x:auto;font-size:.85em}
  a{color:#4a90d9;text-decoration:none} a:hover{text-decoration:underline}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px}
  .stat{text-align:center;padding:8px}
  .stat .num{font-size:1.5em;font-weight:bold}
  .refresh{cursor:pointer;padding:4px 12px;border:1px solid #ddd;border-radius:4px;background:#fff}
</style></head>
<body>
<h1>Worktile Sync Dashboard</h1>
<div id="content"><p>Loading...</p></div>
<p style="margin-top:20px;font-size:.85em;color:#999">
  API: <a href="/health">/health</a> | <a href="/status">/status</a> |
  <a href="/progress">/progress</a> | <a href="/audit">/audit</a> |
  <a href="/log">/log</a>
  &nbsp; <button class="refresh" onclick="load()">Refresh</button>
</p>
<script>
async function load(){
  const el=document.getElementById('content');
  try{
    const r=await fetch('/status');
    const d=await r.json();
    const h=d.health||{};
    const s=h.stats||{};
    const p=d.progress||{};
    const st=h.status==='ok'?'<span class="ok">OK</span>':'<span class="error">ERROR</span>';
    let html=`
      <div class="card">
        <h3>Status: ${st} &nbsp; Last sync: ${h.last_sync||'N/A'} (${h.duration_sec||0}s)</h3>
        <div class="stats">
          <div class="stat"><div class="num">${s.downloaded||0}</div><div class="label">Downloaded</div></div>
          <div class="stat"><div class="num">${s.uploaded||0}</div><div class="label">Uploaded</div></div>
          <div class="stat"><div class="num">${s.errors||0}</div><div class="label">Errors</div></div>
          <div class="stat"><div class="num">${s.skipped_folders||0}</div><div class="label">Skipped</div></div>
        </div>
      </div>`;
    if(p.phase){
      html+=`<div class="card"><h3>Progress</h3><p>Phase: <b>${p.phase}</b> | ${p.detail||''}</p></div>`;
    }
    const changes=h.recent_changes||[];
    if(changes.length){
      html+='<div class="card"><h3>Recent Changes</h3><ul>';
      changes.slice(0,15).forEach(c=>{
        html+=`<li>${c.direction}: ${c.file} (${(c.size/1024).toFixed(1)} KB)</li>`;
      });
      html+='</ul></div>';
    }
    el.innerHTML=html;
  }catch(e){el.innerHTML='<p class="error">Failed to load: '+e+'</p>';}
}
load(); setInterval(load,10000);
</script></body></html>"""
        self._send(200, "text/html; charset=utf-8", html)

    def _send(self, code: int, content_type: str, body: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format: str, *args: Any) -> None:
        # 不打印每个请求到 stderr，用 logger
        logger.debug("HTTP %s", args[0] if args else "")


class StatusServer:
    """后台线程运行 HTTP 状态服务"""

    def __init__(self, local_dir: Path, port: int = 9090) -> None:
        self.port = port
        handler = type("Handler", (_StatusHandler,), {"local_dir": local_dir})
        self._server = HTTPServer(("0.0.0.0", port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()
        logger.info("状态接口已启动: http://0.0.0.0:%d", self.port)

    def stop(self) -> None:
        self._server.shutdown()
        logger.info("状态接口已停止")
