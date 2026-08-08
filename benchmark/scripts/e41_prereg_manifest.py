#!/usr/bin/env python3
"""E4.1：预注册 manifest（压力预算基于 token/cell 预计算，采样前冻结）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
OUT = os.path.join(BENCH, "results", "e41")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    manifest = {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "phase": "E4.1",
        "kind": "pre_registered（压力预算基于 token/cell 预计算）",
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "binary_sha256": "记录于 runner（构建时）",
        "config": {"model": "qwen3-5-4B-Q4_K_M", "ctx": 8192, "parallel": 4,
                   "kv_unified": True, "cache_ram": 0, "temp": 0, "seed": 42,
                   "lifecycle_policy": "default|lru", "attribution_contract": "E3.5.1 不修改"},
        "wall_time_disclaimer": "GPU wall-time 为 exploratory；容量与正确性指标为本阶段门禁依据",
        "pressure_budget": {
            "assumption": "每 token 需 1 个 KV cell（capacity_cells = 8192）；"
                          "不同前缀的 session 不共享 cell（避免 4B 位置复用掩盖）",
            "A_saturation": {
                "sessions": ["S0(active,~2100t)", "S1(idle,~2100t)", "S2(idle,~2100t)", "S3(active,~2100t)", "S4(new,~2000t)"],
                "theory_total_cells": "8400(4 session) + 2000(S4) = 10400 > 8192",
                "expect": "purge 触发；victim ∈ {S1,S2}（idle）；S0/S3 保护；S4 完成",
            },
            "B_victim_contract": {
                "sessions": "3 idle（tick 与 slot id 顺序相反）+ 1 压力",
                "expect": "default 清 slot id 最小；lru 清 tick 最小（tie-break 契约）",
            },
            "C_no_victim": {
                "sessions": "4 全 active + 新需求超池",
                "expect": "既定 schema 合法失败（400 exceeds）或背压；不 crash/hang/串线",
            },
            "D_recovery_reuse": {
                "expect": "被淘汰 session 回访 = 重算（cached=0）或既定失败；"
                          "释放 slot 被新请求复用（generation++）；used_cells 回落",
            },
            "E_churn": {
                "cycles": "≥6 轮 create/grow/idle/evict/recover/cancel/recreate",
                "cumulative_tokens": "> 多个完整池容量（理论累计 > 24576 cells）",
                "expect": "无单调泄漏/悬挂 session/负计数/超 capacity/死锁",
            },
        },
        "scenario_order": {"A": "default,lru × 2 reps", "B": "default,lru × 2 reps",
                           "C": "default,lru × 2 reps", "D": "default,lru × 2 reps",
                           "E": "default,lru × 1 rep"},
        "dual_mode": {
            "correctness": "允许 --lifecycle-trace（核对 generation/slot/session 归属）",
            "production": "关闭 trace 重跑最小饱和场景，验证事件数 0 且行为一致",
        },
        "stop_conditions": ["server 启动失败（重试并记录）", "场景超时（每场景最大运行时间记录）",
                            "出现 active 被清/串线/错误 2xx/crash → 立即记录并判定 REJECT"],
        "gates": {
            "PASS": ["至少一个场景理论需求 > 8192", "可淘汰场景实际 purge 且请求完成",
                     "active 从未被错误淘汰", "victim 符合策略契约", "无 victim 时既定失败",
                     "恢复/重算符合契约", "无跨 session/generation 归属错配",
                     "churn 后资源回收", "结构化解析", "production 零事件", "回归全过"],
            "REJECT": ["错误淘汰 active", "KV 被其他 session/generation 复用", "静默截断/错误成功/crash/死锁/不回收",
                       "victim 违反契约", "默认关闭产生事件"],
            "HOLD": ["未真正触达饱和条件", "telemetry/解析不足", "场景无法完成，且无 correctness 缺陷"],
        },
        "field_trace_provided": False,
        "e36_blocked": True,
    }
    with open(os.path.join(OUT, "prereg_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("e41 prereg manifest written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
