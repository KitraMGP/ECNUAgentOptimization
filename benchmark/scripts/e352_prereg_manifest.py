#!/usr/bin/env python3
"""E3.5.2：预注册 manifest（正式测量前冻结统计规则与噪声校正规则）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
OUT = os.path.join(BENCH, "results", "e352")

sys.path.insert(0, BENCH)


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    manifest = {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "phase": "E3.5.2",
        "kind": "pre_registered（正式测量前冻结）",
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit_candidate": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit_baseline": "049872f59（独立 worktree ../llama-baseline）",
        "binaries": {
            "A_baseline": os.path.join(ROOT, "llama-baseline", "build-cuda", "bin", "llama-server"),
            "B_candidate": os.path.join(ROOT, "llama.cpp", "build-cuda", "bin", "llama-server"),
        },
        "config": {"model": "qwen3-5-4B-Q4_K_M", "ctx": 8192, "parallel": 4,
                   "kv_unified": True, "cache_ram": 0, "temp": 0, "seed": 42,
                   "lifecycle_policy": "default", "lifecycle_trace": "B 明确关闭（默认）"},
        "protocol": {
            "batch": "4 client × 6 轮 multi_turn = 24 并发请求（no-pressure）",
            "per_server": ["全新 server", "health", "warmup（1 短请求，不计入）",
                           "固定批次", "SIGTERM"],
            "exclude": "server 启动与模型加载时间（只统计请求批次 wall）",
            "abba_blocks": 30, "abba_order": "A B B A / B A A B 交替",
            "placebo_blocks": 10, "placebo_order": "A A（同 binary 两次配对差）",
        },
        "statistics": {
            "primary": "delta = (mean(B) - mean(A)) / mean(A)（每 ABBA block 内去趋势）",
            "report": ["delta median", "delta p95", "bootstrap 95% CI（1000 次重采样）",
                       "A/A placebo median/p95", "正负方向计数", "吞吐与 CPU process-time 差",
                       "response hash/失败/行为一致性"],
            "strict_gate": ["median < 0.5%", "raw p95 < 1%"],
        },
        "noise_correction_rules": [
            "仅当 raw p95 未达 1% 时启用，且须全部满足：",
            "1. A/A placebo p95 同样超过 1%",
            "2. ABBA delta median < 0.5%",
            "3. ABBA 与 placebo 的 p95 差不超过 0.25 个百分点",
            "4. ABBA 正负方向近似对称，单侧比例不超过 60%",
            "5. CPU process-time median < 0.5%",
            "6. 吞吐无稳定负方向",
            "7. 无失败、响应或行为差异",
        ],
        "stop_conditions": ["server 启动失败（重试并记录）", "批次失败率异常（>0 且不可解释）"],
        "field_trace_provided": False,
    }
    with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("pre-registered manifest written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
