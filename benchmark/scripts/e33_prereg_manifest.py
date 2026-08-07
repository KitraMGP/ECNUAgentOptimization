#!/usr/bin/env python3
"""E3.3：预注册 manifest（正式实验前冻结；实验后由 e33_summarize.py 补结果文件 hash）。

冻结内容（不得在正式实验开始后修改）：
- workload 参数（WL_PARAMS：multi_turn 16 轮 / long_life 10 轮 secret=9527 / branch 3 / bp {}）
- 压力 prompt 目标 6300 tokens（构造参数，非扫描）
- 配对顺序 D L | L D 交替
- 预注册门槛（正确性 8 / 收益 6 / 非回归 7 / 证据强度）
- STOP 条件
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
OUT = os.path.join(BENCH, "results", "e33")

sys.path.insert(0, BENCH)
from scripts.e33_existing_workload_integration import WL_PARAMS  # noqa: E402


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    manifest = {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "phase": "E3.3",
        "kind": "pre_registered（正式实验前冻结）",
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "wl_params_frozen": WL_PARAMS,
        "pressure_prompt_frozen": "PRESSURE_TEXT 重复 + END STATE 标记，目标 6300 tokens（构造参数，不扫描）",
        "params": {"ctx": 8192, "cache_ram": 0, "seed": 42, "temp": 0,
                   "routing_policy": "default（固定，不使用 prefix-branch）",
                   "lifecycle_policy": "default|lru（E3.2 实现 049872f59）"},
        "paired_order": "D L | L D | D L | L D | D L（每块内交替）",
        "matrix": {
            "A_multi_turn_p2_pressure": "5 对",
            "B_long_life_p2_pressure": "5 对",
            "C_long_life_p4_integration_pressure": "5 对",
            "D_branch_p2_baseline": "3 对（正确性对照）",
            "E_branch_pressure_p2_baseline": "3 对（A2 边界回归）",
            "F_non_unified_smoke": "1×2（lru 惰性）",
            "G_parallel1_smoke": "1×2（无 idle 竞争）",
            "H_RAM_smoke": "multi_turn pressure cache-ram 8192 1×2",
        },
        "pre_registered_thresholds": {
            "correctness": [
                "1. request failure=0",
                "2. evaluator pass rate 不低于 default",
                "3. contamination=0",
                "4. task_success 不低于 default",
                "5. context shift/truncation 不增加",
                "6. active sequence 被清理次数=0",
                "7. server-per-replicate 断言全部通过",
                "8. default 行为与 E3.2 基线一致",
            ],
            "benefit": [
                "1. ≥4/5 paired 关键回访 latency 不劣于 default",
                "2. paired median 回访 latency 改善 ≥5% 或 prompt_processed 降 ≥10%",
                "3. wall time 不增超 3%",
                "4. pressure recovery 成功率不低于 default",
                "5. victim 选择符合冻结 LRU 规则",
                "6. lifecycle selection overhead <2%",
            ],
            "non_regression": [
                "unified parallel=1", "non-unified", "无压力普通 multi_turn",
                "branch 正确性/污染", "branch_pressure 正确性", "RAM default smoke",
                "default lifecycle policy",
            ],
            "evidence": [
                "每个主要 workload ≥5 对",
                "pressure <3/5 → opportunity_not_observed（不判收益）",
                "全部 workload <3/5 → 不得 PROMOTE_A1A4_DEFAULT",
                "无 pressure replicate 计入正确性与 wall 非回归，不计入收益分母",
                "只报告 paired median/方向一致性/样本覆盖率，不宣称显著性",
            ],
        },
        "stop_conditions": [
            "server 启动失败（可重试并记录）",
            "clean 断言失败（replicate 标 invalid 排除）",
            "出现 contamination/active 清理/稳定变慢 → 倾向 REJECT",
        ],
    }
    with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("pre-registered manifest written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
