#!/usr/bin/env python3
"""Status page on :8080 for the Workbench proxy: GPU memory/utilisation and each run's state and last step/loss lines.
No training data is shown (progress.log keeps only step/loss/error lines)."""
import html
import http.server
import json
import pathlib
import subprocess


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total,utilization.gpu",
                              "--format=csv,noheader"], capture_output=True, text=True).stdout
        parts = [f"<h2>Post-training Super VL</h2><h3>GPUs</h3><pre>{html.escape(gpu)}</pre>"]
        for s in sorted(pathlib.Path("/data/runs").glob("*/status.json")):
            st = json.loads(s.read_text())
            prog = s.parent / "progress.log"
            tail = "".join(open(prog, errors="replace").readlines()[-15:]) if prog.exists() else ""
            parts.append(f"<h3>{html.escape(st['name'])}: {html.escape(st['state'])}</h3><pre>{html.escape(tail)}</pre>")
        for f, n in (("start.log", 20), ("setup_automodel.log", 60), ("download.log", 10)):  # setup output only, no data
            p = pathlib.Path("/data/logs") / f
            if p.exists():
                parts.append(f"<h3>{f}</h3><pre>{html.escape(''.join(open(p, errors='replace').readlines()[-n:]))}</pre>")
        body = ("<html><body style='font-family:monospace'>" + "".join(parts) + "</body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)


http.server.ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
