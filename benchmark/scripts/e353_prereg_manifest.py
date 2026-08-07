#!/usr/bin/env python3
"""E3.5.3：预注册 manifest（正式采样前冻结；记录 sha256 于报告）。"""
from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
OUT = os.path.join(BENCH, "results", "e353")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    rng = random.Random(20260807)
    # placebo 20 对（10 A/A + 10 B/B）顺序
    placebo_order = []
    for _ in range(10):
        placebo_order.append(("A", "A"))
        placebo_order.append(("B", "B"))
    rng.shuffle(placebo_order)
    # crossover 30 对（15 A→B + 15 B→A）
    crossover_order = [("A", "B")] * 15 + [("B", "A")] * 15
    rng.shuffle(crossover_order)
    manifest = {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "phase": "E3.5.3",
        "kind": "pre_registered（正式采样前冻结）",
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit_candidate": "aba4b26a",
        "llama_commit_baseline": "049872f59",
        "binaries": {
            "A_baseline": os.path.join(ROOT, "llama-baseline", "build-cuda", "bin", "llama-server"),
            "B_candidate": os.path.join(ROOT, "llama-candidate", "build-cuda", "bin", "llama-server"),
        },
        "config": {"model": "qwen3-5-4B-Q4_K_M", "ctx": 8192, "parallel": 4,
                   "kv_unified": True, "cache_ram": 0, "temp": 0, "seed": 42,
                   "lifecycle_policy": "default", "lifecycle_trace": "B 明确关闭（默认）"},
        "batch": "4 clients × 6 requests = 24 并发（E3.5.2 同一批次；no-pressure）",
        "environment": {
            "gpu": "NVIDIA GeForce RTX 4060 Laptop GPU 8188 MiB",
            "clock_lock": "unavailable（无 root 权限，nvidia-smi -lgc 拒绝）——如实记录",
            "power_lock": "unsupported（Laptop 作用域）",
            "stabilization": "预热至连续 5 分钟温度范围 ≤2°C",
            "pair_adjacency": "每对两个 measurement 相邻执行；pair 内温差 ≤2°C、无 throttling",
        },
        "placebo": {"pairs": 20, "composition": "10 A/A + 10 B/B", "order": placebo_order},
        "crossover": {"pairs": 30, "composition": "15 A→B + 15 B→A", "order": crossover_order},
        "statistics": {
            "placebo_delta": "(second - first) / first",
            "crossover_delta": "(B_wall - A_wall) / A_wall",
            "report": ["总体 + A→B + B→A 分层：delta median/raw p95/bootstrap 95% CI",
                       "正/负方向计数", "throughput 与 CPU process-time median",
                       "placebo abs median/p95", "温度/功耗/clock 差"],
        },
        "gates": {
            "environment_qualification": [
                "failure=0", "A/A 与 B/B 默认关闭事件均为 0",
                "combined p95(abs(placebo_delta)) <= 1%",
                "A/A、B/B 各自 abs(median) < 0.25%",
                "pair 内 GPU 温差均 <=2°C", "GPU median clock pair 差 <=1%",
                "throttling count=0",
            ],
            "readiness": [
                "环境资格通过", "wall delta median <0.5%", "wall delta raw p95 <1%",
                "A→B、B→A 两分层均无稳定正向退化", "CPU process-time median <0.5%",
                "throughput median 退化 <0.5%", "failure=0 且行为一致",
                "candidate 默认关闭事件数=0", "attribution 回归全过",
                "field_trace_not_provided",
            ],
            "hold": ["环境资格失败", "p95/CPU/吞吐门槛未过但无一致退化方向",
                     "schedule/温控/runtime 隔离/协议完整性被破坏"],
            "reject": ["wall median >=0.5% 且 bootstrap CI 下界 >=0.5% 且 A→B/B→A 均变慢",
                       "或默认关闭产生事件/行为变化"],
        },
        "field_trace_provided": False,
    }
    with open(os.path.join(OUT, "prereg_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    h = hashlib.sha256(open(os.path.join(OUT, "prereg_manifest.json"), "rb").read()).hexdigest()[:16]
    print(f"prereg manifest written, sha256={h}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
