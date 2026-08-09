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
# E2.0.5：可变的 KV 状态（completion 后 used_cells 增长，slot erase 后归 0）
_KV = {"used_cells": 0, "active_sequences": 0, "erase_disabled": False,
       "metrics_fail": False,   # v50：/metrics/kv 端点失败开关（after_erase_missing 生产路径）
       "slots_malformed": False}  # v52：/slots 返回空列表开关（clean_all_slots 返回 (0,0) 真实路径）；erase_failed 为框架防御码（monkeypatch e2e），无真实端点路径
# M0（Critical 2）：可配置 finish_reason（"stop" 默认 / "length" 测试决策截断）
_FINISH = {"reason": "stop"}
# M0 v49（任务 4）：一次性请求失败开关（500 一次后复位）——模拟"请求级异常但
# server 进程健康"（warmup 决策异常路径测试；非进程崩溃）
_ERROR_ONCE = {"on": False}


def _make_body(n: int) -> dict:
    _KV["used_cells"] = 384
    _KV["active_sequences"] = 1
    return {
        "id": f"chatcmpl-{n}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "bench",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": f"模拟回复 {n}"},
            "finish_reason": _FINISH["reason"],
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
            if _KV["metrics_fail"]:
                # v50（任务 1）：/metrics/kv 端点失败（生产路径：_fetch 返回 None、
                # last_error 更新；非 mock 抛异常）→ after_erase 观测缺失
                self.send_response(500)
                self.end_headers()
                return
            body = {
                "schema_version": 1,
                "capacity_bytes": 104857600,
                "used_bytes": _KV["used_cells"] * 32768,
                "used_bytes_valid": True,
                "capacity_cells": 1024,
                "used_cells": _KV["used_cells"],
                "active_sequences": _KV["active_sequences"],
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
            if _KV["slots_malformed"]:
                # v52（任务 4）：slots_malformed = /slots 返回空列表 → list_slots 空 →
                # clean_all_slots 返回 (0,0)（真实可达路径，server 健康）。
                # erase_failed 是框架防御码（clean_all_slots 抛异常场景），
                # 不宣称存在真实"端点返回非 JSON → erase_failed"路径——
                # list_slots 对非 JSON/非 200 一律返回 []（吞错），
                # 非 JSON 端点路径实际不可达；erase_failed 由 monkeypatch e2e 覆盖。
                data = b"[]"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
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
        # slot erase：/slots/{id}?action=erase → KV 归零
        import re
        m = re.match(r"/slots/(\d+)\?action=erase", self.path)
        if m:
            if _KV["erase_disabled"]:
                self.send_response(501)
                self.end_headers()
                return
            _KV["used_cells"] = 0
            _KV["active_sequences"] = 0
            data = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        # M0 v49（任务 4）：一次性 500（请求级失败、进程健康）
        if _ERROR_ONCE["on"]:
            _ERROR_ONCE["on"] = False
            self.send_response(500)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        # 实例级崩溃模拟（M0 测试：crash_at 后返回 500 且计数冻结）
        srv = getattr(self, "server", None)
        if srv is not None and getattr(srv, "crash_at", None) is not None:
            srv.count = getattr(srv, "count", 0) + 1
            if srv.count > srv.crash_at:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "mock crash"}')
                return
        # M0（§4.2）：/apply-template → 渲染后 prompt；/tokenize → token ids
        if self.path.startswith("/apply-template"):
            msgs = payload.get("messages", [])
            rendered = "\n".join(
                f"<{msg.get('role', 'user')}>{msg.get('content', '')}"
                for msg in msgs) + "\n<assistant>"
            data = json.dumps({"prompt": rendered}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/tokenize"):
            content = payload.get("content", "")
            n = 100  # 与 chat usage.prompt_tokens=100 一致（parity 偏差=0）；固定值保证预算不超限
            data = json.dumps({"tokens": list(range(n))}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
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

    def __init__(self, crash_at: int | None = None,
                 finish_reason: str = "stop") -> None:
        self.httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.crash_at = crash_at
        self.finish_reason = finish_reason
        # Critical 8：不设实例 count 属性——崩溃计数唯一挂在 httpd.count
        # （handler 经 self.server 读取；实例属性从未被更新/使用，删除防误导）

    @property
    def port(self) -> int:
        assert self.httpd is not None
        return self.httpd.server_address[1]

    def __enter__(self) -> "MockOpenAIServer":
        # 每个实例独立 KV 状态（避免跨测试累积）
        _KV["used_cells"] = 0
        _KV["active_sequences"] = 0
        _KV["erase_disabled"] = False
        _FINISH["reason"] = self.finish_reason
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        # M0 崩溃模拟：crash_at/count 挂到 httpd（handler 经 self.server 读取）
        self.httpd.crash_at = self.crash_at
        self.httpd.count = 0
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        assert self.httpd is not None
        self.httpd.shutdown()
        self.httpd.server_close()
