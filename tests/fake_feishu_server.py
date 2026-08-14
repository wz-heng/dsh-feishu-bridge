"""A dumb Feishu (Lark) open-platform stand-in for the bridge's OUTBOUND
target: it answers the token endpoint, records every message-send / card-PATCH
the bridge issues, and returns benign success envelopes for everything else.

No product code runs here. Mirrors the fake Feishu server used by the bridge
this design was ported from, but as a Python
``http.server`` so this repo's test suite needs no JS toolchain.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class FakeFeishuServer:
    sent: list[dict] = field(default_factory=list)
    _httpd: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._httpd is not None
        host, port = self._httpd.server_address[:2]
        return f"http://127.0.0.1:{port}"

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # silence default access log
                pass

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                try:
                    return json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    return {}

            def _reply(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                path = self.path.split("?", 1)[0]
                parsed = self._read_json()
                if "tenant_access_token" in path or "app_access_token" in path:
                    self._reply(
                        200,
                        {
                            "code": 0,
                            "msg": "ok",
                            "tenant_access_token": "t-fake",
                            "app_access_token": "a-fake",
                            "expire": 7200,
                        },
                    )
                    return
                if path == "/open-apis/im/v1/messages":
                    outer.sent.append(
                        {
                            "path": path,
                            "method": "POST",
                            "receive_id": parsed.get("receive_id"),
                            "msg_type": parsed.get("msg_type"),
                            "content": parsed.get("content"),
                        }
                    )
                    message_id = f"om_{len(outer.sent)}"
                    self._reply(200, {"code": 0, "msg": "ok", "data": {"message_id": message_id}})
                    return
                self._reply(200, {"code": 0, "msg": "ok", "data": {}})

            def do_PATCH(self) -> None:  # noqa: N802 - http.server API
                path = self.path.split("?", 1)[0]
                parsed = self._read_json()
                if path.startswith("/open-apis/im/v1/messages/"):
                    outer.sent.append({"path": path, "method": "PATCH", "content": parsed.get("content")})
                    self._reply(200, {"code": 0, "msg": "ok", "data": {}})
                    return
                self._reply(200, {"code": 0, "msg": "ok", "data": {}})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def reset(self) -> None:
        self.sent.clear()

    def sent_message_bodies(self) -> list[dict]:
        """Decode ``content`` (a JSON string, per the Feishu wire format) for
        every recorded POST /messages call."""
        return [json.loads(item["content"]) for item in self.sent if item["method"] == "POST"]
