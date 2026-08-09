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
from typing import Any, Dict, List, Optional, Tuple
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
        # run 作用域（E1 收尾）：None = 全局快照（start/end/periodic 在 run 外时）；
        # 具体 run_id 表示快照属于某次正式 run
        self._run_id: Optional[str] = None
        # 采集开关：warmup 阶段关闭，正式 run 开启（warmup 的 KV 快照不进入结果）
        self._collecting = True

    # ---- run 作用域 ----
    def begin_run(self, run_id: str) -> None:
        """进入正式 run 上下文；此后快照标记 run_id。"""
        self._run_id = run_id

    def end_run(self) -> None:
        """离开 run 上下文（恢复全局快照语义）。"""
        self._run_id = None

    def set_collecting(self, collecting: bool) -> None:
        """开关采集（False 用于 warmup 阶段，其 KV 快照不进入结果）。"""
        self._collecting = collecting

    # ---- 单次快照 ----
    def snapshot(self, tag: str = "") -> Optional[Dict[str, Any]]:
        """采集一次 KV 快照并记录；endpoint 不可用或未在采集阶段时降级。

        返回原始快照 dict（含 ts/tag/run_id/data），失败返回 None。
        """
        if not self.enabled or not self._collecting:
            return None
        ts = time.time()
        data = self._fetch()
        if data is None:
            self.failures += 1
            return None
        if self.schema_version is None:
            self.schema_version = data.get("schema_version")
        sample = {"ts": round(ts, 3), "tag": tag, "run_id": self._run_id, "data": data}
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

    # ---- E2.0.5：KV 清洁断言 / slot erase（independent replicate 协议） ----
    def kv_state(self) -> Optional[Dict[str, Any]]:
        """同步读取当前 KV 状态（无采集副作用），供清洁断言使用。

        返回 /metrics/kv 原始 dict；endpoint 不可用时返回 None（last_error 更新）。
        """
        if not self.enabled:
            return None
        return self._fetch()

    def list_slots(self) -> List[Dict[str, Any]]:
        """GET /slots 返回 slot 列表；失败返回空列表。"""
        root = self._kv_url.rsplit("/metrics/kv", 1)[0]
        url = f"{root}/slots"
        try:
            with request.urlopen(url, timeout=3.0) as resp:
                if resp.status != 200:
                    return []
                body = json.loads(resp.read().decode("utf-8"))
                return body if isinstance(body, list) else []
        except Exception:
            return []

    def erase_slot(self, slot_id: int) -> bool:
        """POST /slots/{id}?action=erase 清空指定 slot 的 KV。

        需要 server 启用 slot erase（--slot-save-path）；不支持时返回 False。
        """
        root = self._kv_url.rsplit("/metrics/kv", 1)[0]
        url = f"{root}/slots/{slot_id}?action=erase"
        try:
            req = request.Request(url, method="POST")
            with request.urlopen(req, timeout=3.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def clean_all_slots(self) -> Tuple[int, int]:
        """遍历 /slots 并 erase 所有 slot。

        返回 (尝试清理数, 成功清理数)；/slots 不可用或全部失败时返回 (0, 0)。
        """
        slots = self.list_slots()
        if not slots:
            return (0, 0)
        ok = 0
        for s in slots:
            sid = s.get("id")
            if isinstance(sid, int) and self.erase_slot(sid):
                ok += 1
        return (len(slots), ok)

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
            "runs": self.run_aggregate(),
            "failures": self.failures,
            "last_error": self.last_error,
        }

    def run_aggregate(self) -> Dict[str, Any]:
        """按 run_id 聚合：每个正式 run 的 used_cells first/last/peak 与样本数。

        run_id=None 的样本（start/end/periodic 全局快照）不计入 run 聚合。
        M0（Critical 1）：同时聚合 active_sequences（G-M0-3a 需要 erase 后
        active_sequences==0 的真实检查；E1 兼容增量）。
        """
        runs: Dict[str, Dict[str, Any]] = {}
        for s in self.samples:
            rid = s.get("run_id")
            if not rid:
                continue
            r = runs.setdefault(rid, {"samples": 0, "first_used_cells": None,
                                      "last_used_cells": None, "peak_used_cells": None,
                                      "first_active_sequences": None,
                                      "last_active_sequences": None,
                                      "peak_active_sequences": None,
                                      # v49 任务 3：after_erase 归零观测单独暂存，
                                      # 防止 periodic 迟到样本（stop join 超时后
                                      # daemon 线程仍在写）覆盖 last
                                      "_ae_used": None, "_ae_active": None})
            r["samples"] += 1
            data = s.get("data") if isinstance(s.get("data"), dict) else {}
            tag = s.get("tag")
            v = data.get("used_cells")
            if isinstance(v, (int, float)):
                if r["first_used_cells"] is None:
                    r["first_used_cells"] = v
                if tag == "after_erase":
                    r["_ae_used"] = v
                r["last_used_cells"] = v
                r["peak_used_cells"] = max(r["peak_used_cells"] or 0, v)
            a = data.get("active_sequences")
            if isinstance(a, (int, float)):
                if r["first_active_sequences"] is None:
                    r["first_active_sequences"] = a
                if tag == "after_erase":
                    r["_ae_active"] = a
                r["last_active_sequences"] = a
                r["peak_active_sequences"] = max(r["peak_active_sequences"] or 0, a)
        # v49 任务 3：last 优先 tag=after_erase 样本（周期/迟到样本不得覆盖
        # G-M0-3a 依赖的归零观测）
        for r in runs.values():
            if r["_ae_used"] is not None:
                r["last_used_cells"] = r["_ae_used"]
            if r["_ae_active"] is not None:
                r["last_active_sequences"] = r["_ae_active"]
            r.pop("_ae_used", None)
            r.pop("_ae_active", None)
        return runs

    def _field_vals(self, field: str, run_id: Optional[str] = None) -> List[float]:
        vals = []
        for s in self.samples:
            if run_id is not None and s.get("run_id") != run_id:
                continue
            if isinstance(s.get("data"), dict) and isinstance(s["data"].get(field), (int, float)):
                vals.append(s["data"][field])
        return vals

    def peak(self, field: str, run_id: Optional[str] = None) -> Optional[float]:
        """samples 中某数值字段的峰值（可按 run_id 过滤；无样本或非数值返回 None）。"""
        vals = self._field_vals(field, run_id)
        return max(vals) if vals else None

    def first(self, field: str, run_id: Optional[str] = None) -> Optional[float]:
        vals = self._field_vals(field, run_id)
        return vals[0] if vals else None

    def last(self, field: str, run_id: Optional[str] = None) -> Optional[float]:
        vals = self._field_vals(field, run_id)
        return vals[-1] if vals else None
