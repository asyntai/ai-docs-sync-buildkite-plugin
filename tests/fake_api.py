#!/usr/bin/env python3
"""A small stand-in for the Asyntai knowledge base API.

It answers the three endpoints the orb uses, keeps the entries in memory, and
writes every request it received to a JSON file when it stops. The test runner
reads that file to check what the orb actually did.
"""

import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

API_KEY = "test-key-2f8a1c"

STATE = {"entries": {}, "calls": []}
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorised(self):
        header = self.headers.get("Authorization", "")
        if header != "Bearer " + API_KEY:
            self._send(401, {"success": False, "error": "Invalid API key"})
            return False
        return True

    def _record(self, method):
        with LOCK:
            STATE["calls"].append({"method": method, "path": self.path})

    def do_GET(self):
        self._record("GET")
        if not self._authorised():
            return
        if not self.path.startswith("/api/v1/knowledge/"):
            self._send(404, {"success": False, "error": "Not found"})
            return

        path = self.path.split("?", 1)[0].rstrip("/")
        if path != "/api/v1/knowledge":
            entry_id = path.rsplit("/", 1)[-1]
            with LOCK:
                entry = STATE["entries"].get(entry_id)
            if not entry:
                self._send(404, {"success": False,
                                 "error": "Knowledge base entry not found"})
                return
            payload = dict(entry)
            payload.update({"success": True, "content_available": True,
                            "chunks_count": 1})
            self._send(200, payload)
            return

        with LOCK:
            entries = [{k: v for k, v in entry.items() if k != "content"}
                       for entry in STATE["entries"].values()]
        self._send(200, {"success": True, "entries": entries})

    def do_POST(self):
        self._record("POST")
        if not self._authorised():
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))

        if self.path.rstrip("/") != "/api/v1/knowledge/text":
            self._send(404, {"success": False, "error": "Not found"})
            return

        title = body.get("title") or ""
        content = body.get("content") or ""
        if not title:
            self._send(400, {"success": False, "error": "title is required"})
            return
        if len(content) < 10:
            self._send(400, {"success": False,
                             "error": "content must be at least 10 characters"})
            return

        entry_id = str(uuid.uuid4())
        with LOCK:
            STATE["entries"][entry_id] = {
                "id": entry_id,
                "type": "text",
                "title": title,
                "description": content[:80],
                "content": content,
                "created_at": "2026-09-17T10:00:00Z",
            }
        self._send(200, {"success": True, "id": entry_id, "title": title,
                         "chunks_created": max(1, len(content) // 500)})

    def do_DELETE(self):
        self._record("DELETE")
        if not self._authorised():
            return
        entry_id = self.path.rstrip("/").rsplit("/", 1)[-1]
        with LOCK:
            if entry_id not in STATE["entries"]:
                self._send(404, {"success": False,
                                 "error": "Knowledge base entry not found"})
                return
            del STATE["entries"][entry_id]
        self._send(200, {"success": True,
                         "message": "Knowledge base entry deleted"})


def main():
    port = int(sys.argv[1])
    dump_path = sys.argv[2]
    server = HTTPServer(("127.0.0.1", port), Handler)

    def dump():
        with LOCK:
            payload = {"entries": list(STATE["entries"].values()),
                       "calls": STATE["calls"]}
        with open(dump_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

    # The runner asks for a dump by touching a file named <dump>.flush
    def watcher():
        import time
        flag = dump_path + ".flush"
        stop = dump_path + ".stop"
        while True:
            if os.path.exists(flag):
                dump()
                os.remove(flag)
            if os.path.exists(stop):
                dump()
                os.remove(stop)
                server.shutdown()
                return
            time.sleep(0.05)

    threading.Thread(target=watcher, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
