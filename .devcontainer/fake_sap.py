"""Stand-in for the external systems the app contacts at startup (test env only).

- SAP ByD: byd_service/rest.py fetches a CSRF token at import time.
- Middleware: egrn_service creates a default Store after migrations by calling
  /api/v1/authenticate and /api/v1/store.

Without answers from these, Django cannot start or create a test database.
This serves dummy responses so no real system is ever contacted.
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

STORE = {
    "store_name": "Test Store",
    "store_email": "store@example.com",
    "icg_warehouse_name": "Test Warehouse",
    "icg_warehouse_code": "TW01",
    "byd_cost_center_code": "TEST01",
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload):
        self.send_response(200)
        self.send_header("x-csrf-token", "test-token")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_GET(self):
        if self.path.startswith("/api/v1/store"):
            self._send({"data": [STORE]})
        else:
            self._send({})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path.startswith("/api/v1/authenticate"):
            self._send({"data": {"access": "test-access-token"}})
        else:
            self._send({})

    do_PUT = do_PATCH = do_DELETE = do_POST

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 9999), Handler).serve_forever()
