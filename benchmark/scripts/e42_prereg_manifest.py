#!/usr/bin/env python3
"""E4.2：预注册 manifest（对照场景/判定规则/停止条件）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
OUT = os.path.join(BENCH, "results", "e42")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    manifest = {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "phase": "E4.2",
        "kind": "pre_registered",
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "config": {"model": "qwen3-5-4B-Q4_K_M", "ctx": 8192, "parallel": 4,
                   "kv_unified": True, "cache_ram": 0, "temp": 0, "seed": 42,
                   "lifecycle_policy": "default|lru", "attribution_contract": "E3.5.1 不修改"},
        "wall_time_disclaimer": "wall-time 仅 exploratory",
        "scenarios": {
            "A_active_only_control": "与场景 C 相同 active 请求，无 S4；所有 active 必须完成"
                                     "（若自身达 ctx 上限则校准生成预算后重冻结）",
            "B_late_arrival": "active 进入 decode 后提交 S4；理论需求超剩余 KV；无 idle victim；"
                              "验证 active 继续完成，S4 被拒绝/排队/延迟准入",
            "C_simultaneous": "全部同时到达超容量；记录确定 admission 次序；admitted 后不得因后续"
                              "准入发生容量失败",
            "D_recovery": "S4 被拒/背压后等待 active 完成；重试 S4 应成功且不读错误 KV",
            "E_ctx_limit": "单请求达自身 ctx 上限（池有空余）；记录既定错误并与共享池饱和区分",
        },
        "churn_reclamation": {"cycles": 12, "checkpoints_per_cycle": 3,
                              "gates": ["used ≤ capacity", "无负计数/悬挂", "相同逻辑状态无单调增长",
                                        "清空后回到空闲基线"]},
        "causality_questions": [
            "1. active 无 S4 时是否全部完成",
            "2. active 加入 S4 后是否才失败",
            "3. 失败是单请求 ctx 上限还是共享 KV 不足",
            "4. S4 是否在 active 失败前已 admitted",
            "5. active 失败释放的 cells 是否被 S4 使用",
        ],
        "gates": {
            "PASS": ["active-only 全完成", "late-arrival 后到 S4 不使 active 失败",
                     "无 idle victim 时无 active purge/reset/错误 KV 复用",
                     "超容量请求明确拒绝/背压/延迟准入", "simultaneous 符合准入契约",
                     "资源释放后 S4 恢复", "ctx 上限与池压力因果区分",
                     "无错误 2xx/静默截断/串线/crash/hang/死锁",
                     "churn 无持续增长且最终回收", "production 零事件", "回归全过"],
            "REJECT": ["active-only 可完成但 +S4 后 active 失败", "后到请求抢占资源优先完成而 active 容量错误",
                       "active 被 purge/reset/跨 session 错误复用", "容量不足产生错误 2xx/静默截断/crash/hang/死锁",
                       "压力解除后无法恢复/不回收"],
            "HOLD": ["无法区分 ctx 上限与池压力", "无法确定 admission 顺序", "场景未实际超容量"],
        },
        "repair_rule": "确认准入优先级缺陷后实施最小修复：无合法 victim 时不清 active、"
                       "不向 S4 分配使已 admitted 无法继续的 KV、对 S4 队列/背压或明确容量错误、"
                       "资源释放后继续；不改变有 idle victim 时 E4.1 已验证行为；"
                       "不改错误类型/HTTP schema 除非报告兼容性影响",
        "field_trace_provided": False,
        "e36_blocked": True,
    }
    with open(os.path.join(OUT, "prereg_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("e42 prereg manifest written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
