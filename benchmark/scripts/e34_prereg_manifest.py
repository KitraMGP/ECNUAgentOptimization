#!/usr/bin/env python3
"""E3.4：预注册 manifest（正式实验前冻结）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BENCH = os.path.join(ROOT, "benchmark")
OUT = os.path.join(BENCH, "results", "e34")

sys.path.insert(0, BENCH)
from scripts.e34_multi_session_workload_gate import WL_PARAMS  # noqa: E402


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    manifest = {
        "ts": time.strftime("%Y%m%d_%H%M%S"),
        "phase": "E3.4",
        "kind": "pre_registered（正式实验前冻结）",
        "root_commit": subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"]).decode().strip(),
        "llama_commit": subprocess.check_output(["git", "-C", os.path.join(ROOT, "llama.cpp"), "rev-parse", "HEAD"]).decode().strip(),
        "wl_params_frozen": WL_PARAMS,
        "pressure_prompt_frozen": "PRESSURE_TEXT 重复 + END STATE 标记，目标 6300 tokens（构造参数）",
        "params": {"ctx": 8192, "parallel": 4, "cache_ram": 0, "seed": 42, "temp": 0,
                   "routing_policy": "default（固定，不使用 prefix-branch）",
                   "lifecycle_policy": "default|lru（E3.2 实现 049872f59）",
                   "sessions": 4, "session_threads": "真实并发 client（每 session 独立 Driver）",
                   "victim_forcing": "禁止（不使用 id_slot/branch 标识强制 victim；重放全部自动路由）"},
        "paired_order": "D L | L D | D L | L D | D L（写入 manifest 冻结）",
        "matrix": {
            "1_multi_session_multi_turn_pressure": "5 对（主要证据）",
            "2_multi_session_long_life_pressure": "5 对（主要证据）",
            "3_multi_session_multi_turn_no_pressure": "3 对（成本对照）",
            "4_multi_session_long_life_no_pressure": "3 对（成本对照）",
            "5_branch_p2_regression": "复用 E3.3（3×2 baseline，不重跑）",
            "6_branch_pressure_p2_regression": "复用 E3.3（3×2 baseline，不重跑）",
            "7_non_unified_smoke": "multi_session multi_turn no_pressure 1×2（parallel=2 non-unified）",
            "8_parallel1_smoke": "multi_session multi_turn no_pressure 1×2（parallel=1 unified）",
        },
        "pressure_opportunity_rules": [
            "1. unified cell pool 进入真实 free-space/retry/purge 路径",
            "2. ≥2 个合格 idle candidates",
            "3. ≥1 个 active sequence",
            "4. selected victim 非 active",
            "5. purge 后 active 请求成功完成",
            "6. 之后存在可验证的热点 session 回访",
            "7. 回访 evaluator 通过",
            "8. lifecycle event 证明 default/lru 实际选择",
            "仅 used_cells 接近 capacity（无 retry/purge）→ near_pressure（不作为收益证据）",
        ],
        "pre_registered_thresholds": {
            "correctness": ["request failure=0", "evaluator 不低于 default", "task_success 不低于 default",
                            "contamination=0", "truncation 不增", "active 清理=0", "clean 断言全过",
                            "default victim 与 E3.2 first-eligible 一致", "lifecycle stats 默认关",
                            "branch/branch_pressure 无 A2 回归"],
            "optional_benefit": ["pressure coverage ≥4/5（任一 workload）", "lru ≥4/5 热点回访不劣",
                                 "paired median 热点回访 latency 改善 ≥5% 或 processed 降 ≥10%",
                                 "wall ≤3%", "pressure recovery 不低于 default", "victim 选择 5/5 符合规则"],
            "default_benefit": ["两个 workload 家族 coverage ≥4/5", "两个家族都满足收益门槛",
                                "各自 ≥4/5 不劣", "无压力 wall ≤3%", "branch/bp/non-unified/p1 无回归",
                                "诊断/回退/运维完整", "不依赖 synthetic adapter 顺序/branch 标识"],
            "evidence": ["每 workload ≥5 对", "pressure<3/5 → opportunity_not_observed",
                         "near_pressure 不作为收益证据", "no-pressure 样本计入正确性/成本，不计入收益分母",
                         "只报告 paired median/方向一致性/覆盖率"],
        },
        "stop_conditions": ["server 启动失败（重试并记录）", "clean 断言失败（replicate 标 invalid）",
                            "并发协议无法建立（≤4 session 或请求串行）→ 不得高于 HOLD_A1A4",
                            "出现 contamination/active 清理/稳定变慢 → 倾向 REJECT"],
    }
    with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("pre-registered manifest written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
