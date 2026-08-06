"""Mock OpenAI 兼容 server —— 端到端冒烟测试桩（不依赖 GPU / 真实 llama-server）。

返回固定 chat.completions 响应，含 llama-server 风格的字段：
- usage.prompt_tokens_details.cached_tokens
- 顶层 timings（prompt_n / cache_n / predicted_n / prompt_ms / ...）
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Tuple

_COUNTER = {"n": 0}


def _make_body(n: int) -> dict:
    return {
        "id": f"chatcmpl-{n}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "bench",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": f"模拟回复 {n}"},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
            "prompt_tokens_details": {"cached_tokens": 40},
        },
        "timings": {
            "prompt_n": 60, "cache_n": 40, "predicted_n": 10,
            "prompt_ms": 20.0, "predicted_ms": 100.0,
            "prompt_per_second": 3000.0, "predicted_per_second": 100.0,
        },
    }


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/metrics/kv"):
            body = {
                "schema_version": 1,
                "capacity_bytes": 104857600,
                "used_bytes": 20971520,
                "used_bytes_valid": True,
                "capacity_cells": 1024,
                "used_cells": 256,
                "active_sequences": 2,
                "shared_cells": 0,
                "physical_sharing": False,
                "shared_cells_semantics": "multi-sequence cell association (metadata-level, not COW)",
            }
        elif self.path.startswith("/props"):
            body = {
                "model_path": "models/mock-model.gguf",
                "total_slots": 4,
                "build_info": "b3-mockcommit",
            }
        elif self.path.startswith("/slots"):
            body = [{"id": 0, "n_ctx": 512, "is_processing": False, "speculative": False},
                    {"id": 1, "n_ctx": 512, "is_processing": False, "speculative": False}]
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        _COUNTER["n"] += 1
        data = json.dumps(_make_body(_COUNTER["n"])).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class MockOpenAIServer:
    """上下文管理器：启动/关闭 mock server，暴露实际端口。"""

    def __init__(self) -> None:
        self.httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        assert self.httpd is not None
        return self.httpd.server_address[1]

    def __enter__(self) -> "MockOpenAIServer":
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        assert self.httpd is not None
        self.httpd.shutdown()
        self.httpd.server_close()
