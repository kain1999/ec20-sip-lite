"""Dev helper: minimal webhook receiver for testing SMS forwarding locally.

    python tools/webhook_sink.py 18099
"""
import http.server
import json
import sys


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = body
        print("WEBHOOK %s %s -> %s" % (self.command, self.path, json.dumps(parsed, ensure_ascii=False)), flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        payload = b'{"ok":true}'
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18099
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()