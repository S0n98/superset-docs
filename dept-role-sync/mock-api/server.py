"""Fake department API. Serves departments.json, re-read on every request
so editing the file simulates the upstream system renaming a department."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA_FILE = os.environ.get("DATA_FILE", "/data/departments.json")
API_TOKEN = os.environ.get("API_TOKEN", "")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            return self._send(200, {"status": "ok"})
        if self.path.split("?")[0].rstrip("/") != "/api/departments":
            return self._send(404, {"success": False, "message": "not found"})
        if API_TOKEN and self.headers.get("Authorization") != f"Bearer {API_TOKEN}":
            return self._send(401, {"success": False, "message": "unauthorized"})
        with open(DATA_FILE, encoding="utf-8") as f:
            return self._send(200, json.load(f))

    def _send(self, status, body):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8000"))), Handler).serve_forever()
