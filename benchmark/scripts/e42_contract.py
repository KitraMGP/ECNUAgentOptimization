#!/usr/bin/env python3
"""E4.2：冻结产品契约（从代码/测试/API 行为确认后写入）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
OUT = os.path.join(BENCH, "results", "e42")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    contract = {
        "admitted_definition": "请求被分配到 slot 并进入 processing（launch_slot_with_task → "
                               "state != SLOT_STATE_IDLE）即视为 admitted；admitted 后应能在自身 "
                               "per-seq ctx 上限内完成生成",
        "state_definitions": {
            "active": "slot 正在 processing（decode/生成中）",
            "idle": "slot 处于 SLOT_STATE_IDLE（无任务，KV 可能保留）",
            "queued": "任务在 queue_tasks 中等待空 slot（未 admitted）",
            "decoding": "batch decode 循环中处理该 slot 的 token",
        },
        "unified_no_victim_expectation": "无 idle victim 且 KV 不足时：新请求应被拒绝/排队/背压，"
                                        "或已接纳 active 返回容量错误——但不得仅因后到准入使原本可完成的 "
                                        "active 失败（见 minimal_contract）",
        "minimal_contract": "新请求不得仅因后到准入而使原本可完成的已接纳 active 请求失败。",
        "single_request_ctx_limit": {
            "http_status": "400（请求 prompt 超 per-seq ctx 上限时）",
            "error": "request (N tokens) exceeds the available context size (M tokens)",
            "code_path": "server-context.cpp 请求校验（task 分配前）",
        },
        "shared_pool_saturation_error": {
            "http_status": "500/400（decode 失败连带）",
            "error": "Context size has been exceeded.（decode ret=1）",
            "code_path": "server-context.cpp update_slots decode ret!=0 分支（4166-4180）："
                         "n_batch==1 && ret==1 → 对所有 processing slots send_error + release；"
                         "代码 TODO 注释明确 'terminate only the largest active slot' 未实现",
        },
        "admission_priority_claim": "当前代码**未承诺** active 优先于新到达请求（无准入优先级机制）；"
                                    "batch decode 失败会连带所有 processing slots——这是 E4.2 待验证/修复点",
        "purge_semantics": "purge 只清 idle slot（is_processing 跳过）；active 不被选为 victim",
    }
    with open(os.path.join(OUT, "contract.json"), "w", encoding="utf-8") as f:
        json.dump(contract, f, ensure_ascii=False, indent=2)
    print("contract.json written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
