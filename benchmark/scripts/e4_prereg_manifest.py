#!/usr/bin/env python3
"""E4：预注册 manifest（内存容量基准；exploratory 标记）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
OUT = os.path.join(BENCH, "results", "e4")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    manifest = {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "phase": "E4",
        "kind": "pre_registered（内存容量基准）",
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "context": "用户指令：暂缓 E3.5 性能门禁（Laptop GPU wall-time 不作正式性能证据），"
                   "转回核心内存管理优化——验证内存容量/显存峰值/KV 使用/slot 复用/淘汰/并发承载/OOM 边界",
        "exploratory": True,
        "wall_time_disclaimer": "所有 GPU wall-time 结果标记为 exploratory（RTX 4060 Laptop boost 抖动，"
                                "不作为 <0.5% median / <1% p95 正式性能证据）",
        "config": {"model": "qwen3-5-4B-Q4_K_M", "ctx": 8192, "kv_unified": True,
                   "cache_ram": 0, "temp": 0, "seed": 42, "lifecycle_policy": "default|lru",
                   "attribution_contract": "E3.5.1 已闭环，本阶段不修改"},
        "scenarios": {
            "A_并发承载": "N=2/4/6/8 session 并发 × 4 轮 → 可承载数/失败率/KV 利用率/显存峰值",
            "B_OOM边界": "6 session × 轮数 4/8/12 → 池满边界 + purge 恢复 + 400/truncation",
            "C_策略对比": "6 session × 8 轮 → default vs lru 的 KV 利用率/失败率（探索性）",
        },
        "metrics": ["sessions_ok", "failures", "http_400", "truncations",
                    "kv_peak_used_cells", "kv_peak_utilization", "capacity_cells",
                    "gpu_mem_peak_mb", "rss_peak_mb", "task_success_all"],
        "gates": {
            "capacity_success": "并发承载场景：无失败（sessions_ok == n_sessions）且 task_success 全过",
            "oom_recovery": "OOM 边界：purge 后恢复（无 400 且 session 完成）或明确记录边界值",
            "exploratory_only": "性能指标仅作探索，不作正式门槛",
        },
        "field_trace_provided": False,
        "e36_blocked": True,  # 不执行 E3.6（lifecycle 性能门禁 + field trace validator 未满足）
    }
    with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("e4 prereg manifest written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
