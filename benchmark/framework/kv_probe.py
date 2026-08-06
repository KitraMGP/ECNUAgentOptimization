"""KVProbe —— llama-server KV 统计快照采集（E1 可观测性）。

与 RSS/GPU sampler 职责分离：KVProbe 只负责 GET /metrics/kv 的采集与记录，
不参与进程采样，也不与 workload driver 逻辑耦合（driver 仅通过可选 hook 触发快照）。

特性：
- endpoint 不可用时优雅降级：不阻塞、记录 failure 计数与 last_error、指标为 null；
- 记录原始快照（时间戳 + 完整 JSON），不保留聚合值（聚合由 report/下游完成）；
- 记录 schema_version，便于兼容检查；
- 支持手动快照（实验开始/结束、请求前后）与低频周期采样（后台线程）。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional
from urllib import request

# endpoint 返回的兼容 schema 版本（E1 契约）
KV_SCHEMA_VERSION = 1


class KVProbe:
    def __init__(self, base_url: str, enabled: bool = True) -> None:
        root = base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        self._kv_url = f"{root}/metrics/kv"
        self.enabled = enabled
        self.samples: List[Dict[str, Any]] = []
        self.failures = 0
        self.last_error: Optional[str] = None
        self.schema_version: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ---- 单次快照 ----
    def snapshot(self, tag: str = "") -> Optional[Dict[str, Any]]:
        """采集一次 KV 快照并记录；endpoint 不可用时降级并记录失败。

        返回原始快照 dict（含 tag/ts/data），失败返回 None。
        """
        if not self.enabled:
            return None
        ts = time.time()
        data = self._fetch()
        if data is None:
            self.failures += 1
            return None
        if self.schema_version is None:
            self.schema_version = data.get("schema_version")
        sample = {"ts": round(ts, 3), "tag": tag, "data": data}
        self.samples.append(sample)
        return sample

    def _fetch(self) -> Optional[Dict[str, Any]]:
        try:
            with request.urlopen(self._kv_url, timeout=3.0) as resp:
                if resp.status != 200:
                    self.last_error = f"HTTP {resp.status}"
                    return None
                body = json.loads(resp.read().decode("utf-8"))
                if not isinstance(body, dict):
                    self.last_error = f"unexpected payload type: {type(body).__name__}"
                    return None
                if body.get("error"):
                    self.last_error = f"server error: {body['error']}"
                    return None
                self.last_error = None
                return body
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return None

    # ---- 周期采样（低频后台线程） ----
    def start_periodic(self, interval: float) -> None:
        """以 interval 秒为周期后台采样；interval <= 0 时不启动。"""
        if not self.enabled or interval <= 0 or self._thread is not None:
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.wait(interval):
                self.snapshot(tag="periodic")

        self._thread = threading.Thread(target=_loop, daemon=True, name="kv-probe")
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._stop_event.set()
            self._thread.join(timeout=2.0)
            self._thread = None

    # ---- 汇总 ----
    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "schema_version": self.schema_version,
            "samples": self.samples,
            "failures": self.failures,
            "last_error": self.last_error,
        }

    def peak(self, field: str) -> Optional[float]:
        """samples 中某数值字段的峰值（无样本或非数值返回 None）。"""
        vals = [s["data"].get(field) for s in self.samples
                if isinstance(s.get("data"), dict)
                and isinstance(s["data"].get(field), (int, float))]
        return max(vals) if vals else None

    def first(self, field: str) -> Optional[float]:
        for s in self.samples:
            if isinstance(s.get("data"), dict) and isinstance(s["data"].get(field), (int, float)):
                return s["data"][field]
        return None

    def last(self, field: str) -> Optional[float]:
        for s in reversed(self.samples):
            if isinstance(s.get("data"), dict) and isinstance(s["data"].get(field), (int, float)):
                return s["data"][field]
        return None
