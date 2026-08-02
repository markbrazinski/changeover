"""Gate 4 (fault 3/3): a real local HTTP server standing in for the upstream
caption-generation service. It is called for real over the network by
caption_gen_with_telemetry.py -- when in "fail" mode it returns real 503s / real
connection behavior, not a synthetic success-rate number written directly to a metric.
"""
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

FAIL_MODE = "--fail" in sys.argv


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if FAIL_MODE and random.random() < 0.95:
            self.send_response(503)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"upstream caption generation unavailable")
            return
        time.sleep(0.05)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"caption": "..."}')

    def log_message(self, format, *args):
        pass  # keep stdout clean; caller logs request outcomes itself


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8090), Handler)
    print(f"caption-gen upstream stub listening on :8090 (fail_mode={FAIL_MODE})")
    server.serve_forever()
