#!/usr/bin/env python3
"""End-to-end test of the briefs server, run locally against a FAKE Anthropic API.

  python briefs-server/test_server.py

Starts a fake api.anthropic.com (files + messages) and the real Worker under
`wrangler dev`, then drives it as the app would. Costs nothing and needs no key.
"""
import email.parser
import email.policy
import hashlib
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
NODE = r"C:\Program Files\nodejs"
os.environ["PATH"] = NODE + os.pathsep + os.environ["PATH"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ fake Anthropic API

class Fake:
    files = {}
    deleted = []
    messages = []
    mode = "ok"          # ok | refuse | reject_fallback


BRIEF = {"summary": "Write a market profile.", "steps": ["Read the brief", "Collect data\nfrom sources"],
         "watch_out": ["No sources costs points"], "hand_in": "One PDF in the dropbox."}


class FakeAPI(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(n)
        # the SDK may send the key as a bearer token; the raw upload sends x-api-key
        if "test-key" not in (self.headers.get("x-api-key") or "") + (self.headers.get("authorization") or ""):
            print("401 headers:", {k: (v[:12] + "…") for k, v in self.headers.items()
                                   if k.lower() in ("x-api-key", "authorization", "anthropic-version")}, flush=True)
            return self._send(401, {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}})
        if self.path.startswith("/v1/files"):
            msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(
                b"content-type: " + self.headers["content-type"].encode() + b"\r\n\r\n" + raw)
            part = next(p for p in msg.iter_parts() if p.get_param("name", header="content-disposition") == "file")
            data = part.get_payload(decode=True)
            fid = f"file_{len(Fake.files) + 1}"
            mime = part.get_content_type()
            Fake.files[fid] = {"id": fid, "mime_type": mime, "size_bytes": len(data),
                               "created_at": "2026-01-01T00:00:00Z", "filename": part.get_filename()}
            return self._send(200, dict(Fake.files[fid], type="file", downloadable=False, expires_at=None))
        if self.path.startswith("/v1/messages"):
            req = json.loads(raw)
            Fake.messages.append({"path": self.path, "body": req, "beta": self.headers.get("anthropic-beta")})
            if Fake.mode == "reject_fallback" and "fallbacks" in req:
                return self._send(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                                    "message": "fallbacks: not supported here"}})
            stop = "refusal" if Fake.mode == "refuse" else "end_turn"
            return self._send(200, {"id": "msg_1", "type": "message", "role": "assistant", "model": req["model"],
                                    "content": [{"type": "text", "text": json.dumps(BRIEF)}],
                                    "stop_reason": stop, "stop_sequence": None,
                                    "usage": {"input_tokens": 10, "output_tokens": 10}})
        self._send(404, {})

    def do_DELETE(self):
        fid = self.path.rsplit("/", 1)[-1].split("?")[0]
        Fake.deleted.append(fid)
        Fake.files.pop(fid, None)
        self._send(200, {"id": fid, "type": "file_deleted"})

    def do_GET(self):
        self._send(200, {"data": list(Fake.files.values()), "next_page": None})


# ------------------------------------------------------------------ helpers

def multipart(name, data, mime):
    b = "----test" + hashlib.sha1(data).hexdigest()[:12]
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    return body, f"multipart/form-data; boundary={b}"


def call(base, path, method="GET", body=None, headers=None):
    req = urllib.request.Request(base + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(("ok   " if cond else "FAIL ") + name, extra if not cond else "")


def main():
    api_port, worker_port = free_port(), free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", api_port), FakeAPI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    work = HERE / ".test-run"
    shutil.rmtree(work, ignore_errors=True)
    (HERE / ".dev.vars").write_text(
        f"ANTHROPIC_API_KEY=test-key\nTICKET_SECRET=test-secret\n"
        f"ANTHROPIC_BASE_URL=http://127.0.0.1:{api_port}\nPER_IP_LIMIT=3\nDAILY_LIMIT=50\n", encoding="utf-8")
    log = open(HERE / ".test-wrangler.log", "w", encoding="utf-8")
    wr = subprocess.Popen(["npx.cmd", "wrangler", "dev", "--port", str(worker_port), "--ip", "127.0.0.1",
                           "--persist-to", str(work), "--show-interactive-dev-session=false"],
                          cwd=HERE, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{worker_port}"
    try:
        for _ in range(120):
            try:
                if call(base, "/health")[0] == 200:
                    break
            except Exception:                                     # noqa: BLE001
                pass
            time.sleep(0.5)
        check("worker starts under wrangler dev", call(base, "/health")[0] == 200)

        pdf = b"%PDF-1.4 fake assignment brief"
        key = hashlib.sha256(pdf).hexdigest()
        s, j = call(base, f"/brief?key={key}")
        check("unknown brief -> 404", s == 404, j)

        body, ct = multipart("Assessment Instruction.pdf", pdf, "application/pdf")
        s, up = call(base, "/upload", "POST", body, {"content-type": ct})
        check("upload streams to the Files API and returns a ticket", s == 200 and "." in up.get("ticket", ""), (s, up))
        check("...and the ticket is not a bare file id", not up.get("ticket", "").startswith("file_1") or "." in up["ticket"])

        s, j = call(base, "/brief", "POST", json.dumps({"key": key, "title": "EFM Block I",
                    "tickets": [{"ticket": up["ticket"], "name": "Assessment Instruction.pdf"}]}).encode(),
                    {"content-type": "application/json"})
        check("brief is written", s == 200 and j.get("summary") == BRIEF["summary"], (s, j))
        check("steps are one line each", j.get("steps") == ["Read the brief", "Collect data from sources"], j.get("steps"))
        m = Fake.messages[-1]
        check("Opus 5.5 is asked", m["body"]["model"] == "claude-opus-5-5", m["body"]["model"])
        check("structured output schema is sent", m["body"]["output_config"]["format"]["type"] == "json_schema")
        check("fallback to another model on refusal is on", m["body"].get("fallbacks") == "default"
              and "server-side-fallback-2026-07-01" in (m["beta"] or ""), (m["body"].get("fallbacks"), m["beta"]))
        doc = m["body"]["messages"][0]["content"][0]
        check("the document is referenced by file id", doc["source"] == {"type": "file", "file_id": "file_1"}, doc)
        time.sleep(1.5)
        check("uploaded file is deleted afterwards", "file_1" in Fake.deleted, Fake.deleted)

        calls = len(Fake.messages)
        s, j = call(base, f"/brief?key={key}")
        check("second student gets it from the store", s == 200 and j.get("summary") == BRIEF["summary"], s)
        s, j = call(base, "/brief", "POST", json.dumps({"key": key, "title": "x", "tickets": [{"ticket": up["ticket"]}]}).encode(),
                    {"content-type": "application/json"})
        check("...and it is never paid for twice", len(Fake.messages) == calls and s == 200, len(Fake.messages) - calls)

        forged = "file_1." + "0" * 64
        s, j = call(base, "/brief", "POST", json.dumps({"key": "a" * 64, "title": "x",
                    "tickets": [{"ticket": forged}]}).encode(), {"content-type": "application/json"})
        check("a forged ticket is refused", s == 403, (s, j))
        s, j = call(base, "/brief", "POST", json.dumps({"key": "a" * 64, "title": "x",
                    "tickets": [{"ticket": "file_1"}]}).encode(), {"content-type": "application/json"})
        check("a bare file id is refused", s == 403, (s, j))

        big, ct = multipart("big.pdf", b"x" * (16 * 1024 * 1024), "application/pdf")
        s, j = call(base, "/upload", "POST", big, {"content-type": ct})
        check("oversized upload refused", s == 413, s)
        body, ct = multipart("a.exe", b"MZ....", "application/octet-stream")
        s, j = call(base, "/upload", "POST", body, {"content-type": ct})
        check("non-PDF/text upload refused", s == 415, (s, j))

        Fake.mode = "reject_fallback"
        body, ct = multipart("b.txt", b"brief two", "text/plain")
        _, up2 = call(base, "/upload", "POST", body, {"content-type": ct})
        s, j = call(base, "/brief", "POST", json.dumps({"key": "b" * 64, "title": "Two",
                    "tickets": [{"ticket": up2["ticket"]}]}).encode(), {"content-type": "application/json"})
        check("if fallbacks are rejected, it retries without them", s == 200
              and "fallbacks" not in Fake.messages[-1]["body"], (s, j))

        Fake.mode = "refuse"
        _, up3 = call(base, "/upload", "POST", *(lambda b, c: (b, {"content-type": c}))(*multipart("c.txt", b"three", "text/plain")))
        s, j = call(base, "/brief", "POST", json.dumps({"key": "c" * 64, "title": "Three",
                    "tickets": [{"ticket": up3["ticket"]}]}).encode(), {"content-type": "application/json"})
        check("a refusal is reported, not stored", s == 422 and call(base, f"/brief?key={'c' * 64}")[0] == 404, (s, j))

        Fake.mode = "ok"
        _, up4 = call(base, "/upload", "POST", *(lambda b, c: (b, {"content-type": c}))(*multipart("d.txt", b"four", "text/plain")))
        s, j = call(base, "/brief", "POST", json.dumps({"key": "d" * 64, "title": "Four",
                    "tickets": [{"ticket": up4["ticket"]}]}).encode(), {"content-type": "application/json"})
        check("per-computer daily cap stops new briefs", s == 429, (s, j))
    finally:
        wr.terminate()
        try:
            wr.wait(timeout=10)
        except subprocess.TimeoutExpired:
            wr.kill()
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(wr.pid)], capture_output=True)
        srv.shutdown()
        log.close()
        (HERE / ".dev.vars").unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    if not all(results):
        print((HERE / ".test-wrangler.log").read_text(encoding="utf-8", errors="replace")[-2500:])
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
