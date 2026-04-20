# -*- coding: utf-8 -*-
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json, threading, queue, sys

SESS = {}
LOCK = threading.Lock()

def cors(h):
    h.send_header("Access-Control-Allow-Origin", "*")
    h.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    h.send_header("Access-Control-Allow-Headers", "Content-Type")
    h.send_header("Cache-Control", "no-cache")

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        sys.stdout.write("%s - - [%s] %s\n" % (
            self.client_address[0],
            self.log_date_time_string(),
            fmt % a
        ))
        sys.stdout.flush()

    def do_OPTIONS(self):
        self.send_response(200); cors(self); self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/probe":
            self.send_response(200); cors(self)
            self.send_header("Content-Type","text/plain; charset=utf-8")
            self.end_headers(); self.wfile.write(b"ok"); return
        if u.path == "/events":
            sid = (parse_qs(u.query).get("session") or ["default"])[0]
            self.send_response(200); cors(self)
            self.send_header("Content-Type","text/event-stream")
            self.send_header("Connection","keep-alive")
            self.send_header("X-Accel-Buffering","no")
            self.end_headers()
            q = queue.Queue()
            with LOCK: SESS.setdefault(sid, []).append(q)
            self.log_message("[SSE] client registrato -> %s", sid)
            try:
                self.wfile.write(b": connected\n\n"); self.wfile.flush()
                while True:
                    try:
                        evt = q.get(timeout=25)
                        data = json.dumps(evt.get("data", {}), ensure_ascii=False).encode()
                        et = evt.get("type","message").encode()
                        self.wfile.write(b"event: "+et+b"\n")
                        self.wfile.write(b"data: "+data+b"\n\n")
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n"); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with LOCK:
                    if sid in SESS and q in SESS[sid]: SESS[sid].remove(q)
            return
        self.send_response(404); cors(self); self.end_headers()

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/emit":
            self.send_response(404); cors(self); self.end_headers(); return
        ln = int(self.headers.get("Content-Length","0"))
        raw = self.rfile.read(ln) if ln else b"{}"
        try:
            obj = json.loads(raw.decode() or "{}")
        except Exception as e:
            self.send_response(400); cors(self); self.end_headers()
            self.wfile.write(("bad json: %s" % e).encode()); return
        sid = obj.get("session") or "default"
        evt = {"type":obj.get("type","message"), "data":obj.get("data",{})}
        delivered = 0
        with LOCK:
            for q in SESS.get(sid, []):
                try: q.put_nowait(evt); delivered += 1
                except queue.Full: pass
        self.send_response(200); cors(self)
        self.send_header("Content-Type","application/json"); self.end_headers()
        out = {"ok":True,"session":sid,"delivered":delivered}
        self.wfile.write(json.dumps(out).encode())
        self.log_message("[SSE] evento inviato a %s -> %s", sid, delivered)

def main(host="127.0.0.1", port=8888):
    srv = ThreadingHTTPServer((host, port), H)
    print(f"[SSE] listening on http://{host}:{port}")
    try: srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    main()
