"""临时假后端：返回非标准字段形状的 JSON，用于验证 P4 字段映射。生产不需要。"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = {"orderState": "已签收", "logistics": {"company": "京东", "arriveDate": "2026-08-30"}}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *a):  # 静默
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8899), H).serve_forever()
